"""Constant buffer tools (§13 of the roadmap).

  - dump_cbv: dump raw bytes of a CBV at a specific event
  - decode_cbv: decode bytes as float4 rows (and float16x16 matrices)
  - diff_cbv:   compare two CBVs by float index, summarising matrix/vector deltas

Usage:
    python -m util.automation.cbv_tools dump <capture.rdc> --event <eid> --slot <root_param_or_name>
    python -m util.automation.cbv_tools diff <capture.rdc> --eventA <a> --eventB <b> --slot 0
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
from typing import List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _resolve_cbv(controller, event_id: int, slot) -> Optional[dict]:
    """Find the CBV bound at `slot` at the given event.

    `slot` is either an int (root parameter index for D3D12) or a string that
    matches the constant block name in the reflection.
    """
    controller.SetFrameEvent(int(event_id), True)
    d3d12 = None
    try:
        d3d12 = controller.GetD3D12PipelineState()
    except Exception:
        pass

    if isinstance(slot, int) and d3d12 is not None:
        if not (0 <= slot < len(d3d12.rootSignature.parameters)):
            return None
        p = d3d12.rootSignature.parameters[slot]
        if len(p.tableRanges) > 0:
            # Root table — pick the first CBV range
            for r in p.tableRanges:
                if _lib.descriptor_category_name(r.category) == "ConstantBlock":
                    return {
                        "kind": "RootTable",
                        "heap": _lib.resource_id_str(p.heap),
                        "heapByteOffset": int(p.heapByteOffset) + int(r.tableByteOffset),
                    }
        elif len(p.constants) > 0:
            return {"kind": "RootConstants", "bytes": bytes(p.constants)}
        else:
            return {
                "kind": "RootDescriptor",
                "resource": _lib.resource_id_str(p.descriptor.resource),
                "byteOffset": int(p.descriptor.byteOffset),
                "byteSize": int(p.descriptor.byteSize),
            }
        return None

    if isinstance(slot, str):
        pipe = controller.GetPipelineState()
        for stage_enum in (
            rd.ShaderStage.Vertex,
            rd.ShaderStage.Pixel,
            rd.ShaderStage.Compute,
        ):
            try:
                cbs = pipe.GetConstantBlocks(stage_enum, False)
            except Exception:
                continue
            refl = pipe.GetShaderReflection(stage_enum)
            if refl is None:
                continue
            for used in cbs:
                idx = int(used.access.index)
                if idx == 0xFFFF or idx >= len(refl.constantBlocks):
                    continue
                if str(refl.constantBlocks[idx].name) == slot:
                    return {
                        "kind": "Named",
                        "resource": _lib.resource_id_str(used.descriptor.resource),
                        "byteOffset": int(used.descriptor.byteOffset),
                        "byteSize": int(used.descriptor.byteSize),
                    }
    return None


def dump(capture: str, event_id: int, slot) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        descr = _resolve_cbv(controller, event_id, slot)
        if descr is None:
            return {"error": "CBV not found", "eventId": event_id, "slot": slot}
        if descr["kind"] == "RootConstants":
            data = descr["bytes"]
        else:
            rid_str = descr.get("resource")
            if rid_str is None:
                return {"error": "CBV has no backing resource", "descriptor": descr}
            rid = _find_resource_id(controller, rid_str)
            data = bytes(controller.GetBufferData(rid, descr.get("byteOffset", 0), descr.get("byteSize", 0)))
        floats = _to_floats(data)
        return {
            "eventId": event_id,
            "slot": slot,
            "descriptor": {k: v for k, v in descr.items() if k != "bytes"},
            "byteSize": len(data),
            "hex": data[:1024].hex(),
            "float4Rows": _format_float4_rows(floats),
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def diff(capture: str, event_a: int, event_b: int, slot) -> dict:
    a = dump(capture, event_a, slot)
    b = dump(capture, event_b, slot)
    if "error" in a or "error" in b:
        return {"a": a, "b": b}
    fa = _to_floats(bytes.fromhex(a["hex"]))
    fb = _to_floats(bytes.fromhex(b["hex"]))
    deltas = []
    n = min(len(fa), len(fb))
    for i in range(n):
        if abs(fa[i] - fb[i]) > 1e-7:
            deltas.append({"index": i, "a": fa[i], "b": fb[i], "delta": fb[i] - fa[i]})
    return {
        "eventA": event_a,
        "eventB": event_b,
        "slot": slot,
        "floatCount": n,
        "deltas": deltas[:512],
    }


def _find_resource_id(controller, rid_str):
    for r in controller.GetResources():
        if str(r.resourceId) == rid_str:
            return r.resourceId
    raise ValueError(f"resource id {rid_str} not found")


def _to_floats(data: bytes) -> List[float]:
    n = len(data) // 4
    if n == 0:
        return []
    return list(struct.unpack(f"<{n}f", data[: n * 4]))


def _format_float4_rows(floats):
    out = []
    for i in range(0, len(floats), 4):
        row = floats[i : i + 4]
        if len(row) < 4:
            row += [0.0] * (4 - len(row))
        out.append({"row": i // 4, "v": row})
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Constant buffer tools.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("dump")
    pd.add_argument("capture")
    pd.add_argument("--event", "-e", type=int, required=True)
    pd.add_argument("--slot", required=True, help="Integer root parameter index or named constant block")

    pdif = sub.add_parser("diff")
    pdif.add_argument("capture")
    pdif.add_argument("--eventA", "-a", type=int, required=True)
    pdif.add_argument("--eventB", "-b", type=int, required=True)
    pdif.add_argument("--slot", required=True)

    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        slot = int(args.slot) if args.slot.lstrip("-").isdigit() else args.slot
        if args.cmd == "dump":
            out = dump(args.capture, args.event, slot)
        else:
            out = diff(args.capture, args.eventA, args.eventB, slot)
    finally:
        rd.ShutdownReplay()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
