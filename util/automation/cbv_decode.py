"""Decoded constant buffer dump using shader reflection.

Wraps controller.GetCBufferVariableContents(pipeline, shader, stage,
entryPoint, cbufslot, buffer, offset, length) which decodes a CBV blob into
named field/struct/matrix variables according to the shader's reflection.

This is the Nsight equivalent of clicking a constant buffer and seeing
"ViewMatrix = ..., RotateLeft = ..., FogParams.density = ...".

Combined with eye pairing, gives:
    python -m util.automation.cbv_decode pair <cap.rdc> --left 16042 --right 16678 --cbuf cbuffer0

Usage:
    python -m util.automation.cbv_decode dump <cap.rdc> --event 16042 --cbuf cbuffer0
    python -m util.automation.cbv_decode pair <cap.rdc> --left 16042 --right 16678 --cbuf View
"""

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
    from automation import shader_debug
else:
    from . import _lib, shader_debug

import renderdoc as rd  # noqa: E402


def _find_cbv(pipe, cbuf_name: str):
    """Search every stage's constant blocks for one named `cbuf_name`.
    Returns (stage, shader_id, entry, cbufslot, buffer_id, offset, size) or None."""
    for stage_enum in (
        rd.ShaderStage.Pixel,
        rd.ShaderStage.Vertex,
        rd.ShaderStage.Compute,
        rd.ShaderStage.Hull,
        rd.ShaderStage.Domain,
        rd.ShaderStage.Geometry,
        rd.ShaderStage.Amplification,
        rd.ShaderStage.Mesh,
    ):
        refl = pipe.GetShaderReflection(stage_enum)
        if refl is None:
            continue
        for i, cb in enumerate(refl.constantBlocks):
            if str(cb.name) == cbuf_name:
                cbs = pipe.GetConstantBlocks(stage_enum, False)
                for u in cbs:
                    if int(u.access.index) == i:
                        return {
                            "stage": stage_enum,
                            "shaderId": refl.resourceId,
                            "entryPoint": str(refl.entryPoint),
                            "cbufslot": i,
                            "buffer": u.descriptor.resource,
                            "offset": int(u.descriptor.byteOffset),
                            "size": int(u.descriptor.byteSize),
                        }
    return None


def dump(capture: str, event_id: int, cbuf_name: str) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        controller.SetFrameEvent(int(event_id), True)
        pipe = controller.GetPipelineState()
        info = _find_cbv(pipe, cbuf_name)
        if info is None:
            return {"error": f"constant block '{cbuf_name}' not found"}

        d3d12 = controller.GetD3D12PipelineState()
        pso = d3d12.pipelineResourceId if d3d12 else rd.ResourceId()

        vars_ = controller.GetCBufferVariableContents(
            pso, info["shaderId"], info["stage"], info["entryPoint"],
            info["cbufslot"], info["buffer"], info["offset"], info["size"],
        )
        return {
            "eventId": event_id,
            "constantBlock": cbuf_name,
            "stage": _lib.shader_stage_name(info["stage"]),
            "byteSize": info["size"],
            "variables": [shader_debug._shadervar(v) for v in vars_],
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def pair(capture: str, event_left: int, event_right: int, cbuf_name: str) -> dict:
    a = dump(capture, event_left, cbuf_name)
    b = dump(capture, event_right, cbuf_name)
    if "error" in a or "error" in b:
        return {"left": a, "right": b}

    # Walk variables by name and emit per-field deltas
    def index_by_name(vs):
        out = {}
        for v in vs or []:
            out[v["name"]] = v
        return out

    la = index_by_name(a["variables"])
    rb = index_by_name(b["variables"])
    diffs = []
    for k in sorted(set(la.keys()) | set(rb.keys())):
        lv = la.get(k, {}).get("values")
        rv = rb.get(k, {}).get("values")
        if lv != rv:
            diffs.append({"name": k, "left": lv, "right": rv})
    return {
        "left": a, "right": b, "differences": diffs,
        "identical": not bool(diffs),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Decoded constant buffer dump + diff.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("dump")
    pd.add_argument("capture")
    pd.add_argument("--event", "-e", type=int, required=True)
    pd.add_argument("--cbuf", required=True)

    pc = sub.add_parser("pair")
    pc.add_argument("capture")
    pc.add_argument("--left", type=int, required=True)
    pc.add_argument("--right", type=int, required=True)
    pc.add_argument("--cbuf", required=True)

    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        if args.cmd == "dump":
            out = dump(args.capture, args.event, args.cbuf)
        else:
            out = pair(args.capture, args.left, args.right, args.cbuf)
    finally:
        rd.ShutdownReplay()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
