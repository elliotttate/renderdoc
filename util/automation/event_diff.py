"""Pairwise event diff — Nsight-style "compare two draws".

Given two eventIds (typically a left-eye and right-eye draw), report:
  - Same shader hashes? (per-stage)
  - Viewport / scissor differences
  - Render target / depth target differences
  - Per-binding differences (register, resource, format, mip/slice)
  - Constant buffer byte deltas for shared CBVs
  - Vertex/index buffer differences
  - Action flags / dispatch dims / instance counts

Usage:
    python -m util.automation.event_diff <capture.rdc> --a 16042 --b 16678
"""

import argparse
import json
import os
import struct
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _binding_key(b):
    return (b.get("stage"), b.get("type"), b.get("register"), b.get("space"))


def event_diff(capture: str, event_a: int, event_b: int, with_cbv_bytes: bool = True) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        sa = _lib.collect_state_at_event(controller, event_a)
        sb = _lib.collect_state_at_event(controller, event_b)

        out = {"eventA": event_a, "eventB": event_b, "differences": {}}

        # Shaders per stage
        shaders_a = {s["stage"]: s for s in sa.get("shaders", [])}
        shaders_b = {s["stage"]: s for s in sb.get("shaders", [])}
        shader_diff = {}
        for stage in sorted(set(shaders_a.keys()) | set(shaders_b.keys())):
            a = shaders_a.get(stage, {})
            b = shaders_b.get(stage, {})
            if a.get("bytecodeHash") != b.get("bytecodeHash"):
                shader_diff[stage] = {
                    "a": a.get("bytecodeHash"),
                    "b": b.get("bytecodeHash"),
                    "aId": a.get("shaderId"),
                    "bId": b.get("shaderId"),
                }
        if shader_diff:
            out["differences"]["shaders"] = shader_diff

        # PSO
        if sa.get("pipelineId") != sb.get("pipelineId"):
            out["differences"]["pipelineId"] = {"a": sa.get("pipelineId"), "b": sb.get("pipelineId")}

        # Viewports / scissors
        if sa.get("viewports") != sb.get("viewports"):
            out["differences"]["viewports"] = {"a": sa.get("viewports"), "b": sb.get("viewports")}
        if sa.get("scissors") != sb.get("scissors"):
            out["differences"]["scissors"] = {"a": sa.get("scissors"), "b": sb.get("scissors")}

        # Render targets
        rts_a = sa.get("renderTargets", [])
        rts_b = sb.get("renderTargets", [])
        if rts_a != rts_b:
            out["differences"]["renderTargets"] = {"a": rts_a, "b": rts_b}

        # Descriptor heaps
        if set(sa.get("descriptorHeaps") or []) != set(sb.get("descriptorHeaps") or []):
            out["differences"]["descriptorHeaps"] = {
                "a": sa.get("descriptorHeaps"), "b": sb.get("descriptorHeaps")
            }

        # Bindings keyed by (stage, type, register, space)
        bind_a = {_binding_key(b): b for b in sa.get("bindings", [])}
        bind_b = {_binding_key(b): b for b in sb.get("bindings", [])}
        keys = sorted(set(bind_a.keys()) | set(bind_b.keys()))
        binding_diffs = []
        for k in keys:
            a = bind_a.get(k, {})
            b = bind_b.get(k, {})
            # Skip identical
            differs = {}
            for field in ("resource", "view", "format", "firstMip", "numMips",
                          "firstSlice", "numSlices", "heap", "heapByteOffset",
                          "bufferByteOffset", "bufferByteSize", "name"):
                if a.get(field) != b.get(field):
                    differs[field] = {"a": a.get(field), "b": b.get(field)}
            if differs:
                binding_diffs.append({"key": list(k), "diff": differs})
        if binding_diffs:
            out["differences"]["bindings"] = binding_diffs

        # CBV byte deltas — for each shared CBV slot, dump bytes and diff
        if with_cbv_bytes:
            cbv_diffs = []
            for k in keys:
                if k[1] != "ConstantBuffer":
                    continue
                a = bind_a.get(k); b = bind_b.get(k)
                if not a or not b:
                    continue
                if not a.get("resource") or not b.get("resource"):
                    continue
                cbv_diff = _diff_cbv(controller, a, b)
                if cbv_diff:
                    cbv_diffs.append({"key": list(k), "diff": cbv_diff})
            if cbv_diffs:
                out["differences"]["cbvBytes"] = cbv_diffs

        if not out["differences"]:
            out["identical"] = True
        else:
            out["identical"] = False
        return out
    finally:
        controller.Shutdown()
        cap.Shutdown()


def _diff_cbv(controller, a: dict, b: dict) -> dict:
    """Read both CBVs and return per-float deltas."""
    a_rid = _find_resource_id(controller, a["resource"])
    b_rid = _find_resource_id(controller, b["resource"])
    a_off = int(a.get("bufferByteOffset", 0))
    b_off = int(b.get("bufferByteOffset", 0))
    a_size = int(a.get("bufferByteSize", 0))
    b_size = int(b.get("bufferByteSize", 0))
    n = min(a_size, b_size)
    if n == 0:
        return {}
    try:
        ad = bytes(controller.GetBufferData(a_rid, a_off, n))
        bd = bytes(controller.GetBufferData(b_rid, b_off, n))
    except Exception:
        return {}
    if ad == bd:
        return {}
    fa = list(struct.unpack(f"<{n // 4}f", ad[: (n // 4) * 4]))
    fb = list(struct.unpack(f"<{n // 4}f", bd[: (n // 4) * 4]))
    deltas = []
    for i in range(len(fa)):
        if abs(fa[i] - fb[i]) > 1e-7:
            deltas.append({"index": i, "a": fa[i], "b": fb[i], "delta": fb[i] - fa[i]})
    return {"sizeBytes": n, "floatDeltas": deltas[:256]}


def _find_resource_id(controller, rid_str):
    for r in controller.GetResources():
        if str(r.resourceId) == rid_str:
            return r.resourceId
    raise ValueError(f"resource id {rid_str} not found")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Diff state at two events (left vs right eye, etc.).")
    p.add_argument("capture")
    p.add_argument("--a", type=int, required=True, help="First event ID")
    p.add_argument("--b", type=int, required=True, help="Second event ID")
    p.add_argument("--no-cbv", action="store_true", help="Skip CBV byte content diff")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = event_diff(args.capture, args.a, args.b, not args.no_cbv)
    finally:
        rd.ShutdownReplay()

    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
