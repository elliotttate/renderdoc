"""Shader debug — wrap controller.DebugPixel / DebugVertex / DebugThread.

The single most useful Nsight-style feature for eye-vs-eye divergence: step
through the shader at a specific (x, y) on the right eye, capture the input
register values, then do the same on the left eye, and diff.

Outputs the input register values (texture samples, CBV reads, interpolated
inputs) so callers can pin down which input differs between eyes.

Usage:
    # Debug a specific pixel
    python -m util.automation.shader_debug pixel <cap.rdc> --event 16042 --x 900 --y 250

    # Debug a compute thread
    python -m util.automation.shader_debug thread <cap.rdc> --event 16030 --group 0 0 0 --thread 0 0 0

    # Side-by-side: same uv on left vs right eye
    python -m util.automation.shader_debug compare <cap.rdc> --left 16042 --right 16678 --x 100 --y 50
"""

from __future__ import annotations

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _trace_to_dict(trace):
    """Flatten a ShaderDebugTrace into a JSON-safe dict."""
    if trace is None:
        return {"error": "no trace returned"}
    out = {
        "stage": _lib.shader_stage_name(trace.stage) if hasattr(trace, "stage") else None,
        "inputs": [_shadervar(v) for v in getattr(trace, "inputs", [])],
        "constantBlocks": [_shadervar(v) for v in getattr(trace, "constantBlocks", [])],
        "readOnlyResources": [_shadervar(v) for v in getattr(trace, "readOnlyResources", [])],
        "readWriteResources": [_shadervar(v) for v in getattr(trace, "readWriteResources", [])],
        "samplers": [_shadervar(v) for v in getattr(trace, "samplers", [])],
        "sourceVars": [str(v.name) for v in getattr(trace, "sourceVars", [])],
    }
    return out


def _shadervar(v):
    """Render a ShaderVariable as a JSON-safe dict."""
    out = {"name": str(v.name)}
    try:
        out["type"] = str(v.type).split(".")[-1] if hasattr(v, "type") else None
        out["rows"] = int(v.rows) if hasattr(v, "rows") else None
        out["columns"] = int(v.columns) if hasattr(v, "columns") else None
        # Try float values first
        vals = []
        for r in range(max(1, int(getattr(v, "rows", 1) or 1))):
            for c in range(max(1, int(getattr(v, "columns", 1) or 1))):
                try:
                    vals.append(float(v.value.f32v[r * 4 + c]))
                except Exception:
                    try:
                        vals.append(int(v.value.u32v[r * 4 + c]))
                    except Exception:
                        pass
        if vals:
            out["values"] = vals[:16]
    except Exception:
        pass
    if hasattr(v, "members") and len(v.members) > 0:
        out["members"] = [_shadervar(m) for m in v.members[:16]]
    return out


def debug_pixel(capture: str, event_id: int, x: int, y: int, sample=0, primitive=None, view=None) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        controller.SetFrameEvent(int(event_id), True)
        inputs = rd.DebugPixelInputs()
        inputs.sample = int(sample)
        if primitive is not None:
            inputs.primitive = int(primitive)
        if view is not None:
            inputs.view = int(view)
        trace = controller.DebugPixel(int(x), int(y), inputs)
        out = {"eventId": event_id, "x": x, "y": y, "trace": _trace_to_dict(trace)}
        if trace is not None:
            controller.FreeTrace(trace)
        return out
    finally:
        controller.Shutdown()
        cap.Shutdown()


def debug_thread(capture: str, event_id: int, group, thread) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        controller.SetFrameEvent(int(event_id), True)
        groupid = rd.rdcfixedarray_uint32_t_3() if hasattr(rd, "rdcfixedarray_uint32_t_3") else None
        # Best-effort: pass as tuple — many bindings accept that.
        trace = controller.DebugThread(tuple(group), tuple(thread))
        out = {"eventId": event_id, "group": group, "thread": thread, "trace": _trace_to_dict(trace)}
        if trace is not None:
            controller.FreeTrace(trace)
        return out
    finally:
        controller.Shutdown()
        cap.Shutdown()


def compare_pixels(capture: str, event_left: int, event_right: int, x: int, y: int) -> dict:
    """Same (x, y) on two events, return both traces + a coarse diff."""
    a = debug_pixel(capture, event_left, x, y)
    b = debug_pixel(capture, event_right, x, y)

    # Diff readOnlyResources / constantBlocks / inputs by name
    def index_by_name(arr):
        return {v["name"]: v for v in arr or []}

    diffs = {}
    for section in ("inputs", "constantBlocks", "readOnlyResources", "samplers"):
        left = index_by_name(a["trace"].get(section))
        right = index_by_name(b["trace"].get(section))
        keys = sorted(set(left.keys()) | set(right.keys()))
        for k in keys:
            lv = left.get(k, {})
            rv = right.get(k, {})
            if lv.get("values") != rv.get("values"):
                diffs.setdefault(section, []).append({
                    "name": k,
                    "left": lv.get("values"),
                    "right": rv.get("values"),
                })
    return {
        "left": a,
        "right": b,
        "differences": diffs,
        "identical": not bool(diffs),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Shader debug wrapper.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("pixel")
    pp.add_argument("capture")
    pp.add_argument("--event", "-e", type=int, required=True)
    pp.add_argument("--x", type=int, required=True)
    pp.add_argument("--y", type=int, required=True)
    pp.add_argument("--sample", type=int, default=0)
    pp.add_argument("--primitive", type=int, default=None)
    pp.add_argument("--view", type=int, default=None)

    pt = sub.add_parser("thread")
    pt.add_argument("capture")
    pt.add_argument("--event", "-e", type=int, required=True)
    pt.add_argument("--group", nargs=3, type=int, required=True)
    pt.add_argument("--thread", nargs=3, type=int, required=True)

    pc = sub.add_parser("compare")
    pc.add_argument("capture")
    pc.add_argument("--left", type=int, required=True)
    pc.add_argument("--right", type=int, required=True)
    pc.add_argument("--x", type=int, required=True)
    pc.add_argument("--y", type=int, required=True)

    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        if args.cmd == "pixel":
            out = debug_pixel(args.capture, args.event, args.x, args.y, args.sample,
                              args.primitive, args.view)
        elif args.cmd == "thread":
            out = debug_thread(args.capture, args.event, args.group, args.thread)
        else:
            out = compare_pixels(args.capture, args.left, args.right, args.x, args.y)
    finally:
        rd.ShutdownReplay()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
