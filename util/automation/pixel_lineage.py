"""Pixel writer / composite lineage (§9 of the roadmap).

Given a (x,y) on a render target at a specific event (or final backbuffer
default), runs controller.PixelHistory() and walks each writer's bindings to
expose the upstream texture chain.

Usage:
    python -m util.automation.pixel_lineage <capture.rdc> --x 900 --y 250 [--event <eid>]
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


def _find_backbuffer_action(controller):
    """Find the last Present, and its predecessor draw."""
    last_present = None
    for a in _lib.walk_actions(controller):
        if int(a.flags) & int(rd.ActionFlags.Present):
            last_present = a
    return last_present


def _pick_target(controller, prefer_eid=None):
    """Return (eventId, target_resource, sub_resource_mip, sub_resource_slice)."""
    # If an event ID is given, use the current state's first RT.
    if prefer_eid is not None:
        controller.SetFrameEvent(int(prefer_eid), True)
        d3d12 = None
        try:
            d3d12 = controller.GetD3D12PipelineState()
        except Exception:
            pass
        if d3d12 is not None and len(d3d12.outputMerger.renderTargets) > 0:
            rt = d3d12.outputMerger.renderTargets[0]
            return int(prefer_eid), rt.resource, int(rt.firstMip), int(rt.firstSlice)

    # Otherwise pick the last Present and its predecessor RT.
    present = _find_backbuffer_action(controller)
    if present is None:
        raise RuntimeError("No Present action found in capture")
    eid = int(present.eventId)
    if present.previous is None:
        raise RuntimeError("No predecessor of Present")
    prev = present.previous
    controller.SetFrameEvent(int(prev.eventId), True)
    d3d12 = None
    try:
        d3d12 = controller.GetD3D12PipelineState()
    except Exception:
        pass
    if d3d12 is None or len(d3d12.outputMerger.renderTargets) == 0:
        # Fall back to the last bound output of the predecessor action
        for o in prev.outputs:
            if _lib.resource_id_str(o) is not None:
                return int(prev.eventId), o, 0, 0
        raise RuntimeError("No render target bound at predecessor of Present")
    rt = d3d12.outputMerger.renderTargets[0]
    return int(prev.eventId), rt.resource, int(rt.firstMip), int(rt.firstSlice)


def pixel_lineage(capture_path: str, x: int, y: int, event_id=None) -> dict:
    cap, controller = _lib.open_capture(capture_path)
    try:
        eid, target, mip, slice_ = _pick_target(controller, event_id)
        sub = rd.Subresource(mip, slice_, 0)
        # PixelHistory returns events that wrote pixel (x,y) on `target` up through `eid`.
        history = controller.PixelHistory(target, x, y, sub, rd.CompType.Typeless)

        rows = []
        for h in history:
            row = {
                "eventId": int(h.eventId),
                "fragIndex": int(h.fragIndex) if hasattr(h, "fragIndex") else None,
                "primitiveID": int(h.primitiveID) if hasattr(h, "primitiveID") else None,
                "shaderOut": _color_to_dict(getattr(h, "shaderOut", None)),
                "preMod": _color_to_dict(getattr(h, "preMod", None)),
                "postMod": _color_to_dict(getattr(h, "postMod", None)),
                "passed": bool(h.Passed()) if hasattr(h, "Passed") else None,
            }
            rows.append(row)

        # For the latest writer, snapshot the bindings so callers can chase upstream textures.
        upstream = None
        if rows:
            try:
                upstream = _lib.collect_state_at_event(controller, rows[-1]["eventId"])
            except Exception as exc:
                upstream = {"error": str(exc)}

        return {
            "x": int(x),
            "y": int(y),
            "target": _lib.resource_id_str(target),
            "eventId": eid,
            "history": rows,
            "lastWriterState": upstream,
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def _color_to_dict(mod):
    if mod is None:
        return None
    try:
        c = mod.col
        s = mod.stencil
        d = mod.depth
        return {
            "col": [float(c.floatValue[i]) for i in range(4)] if hasattr(c, "floatValue") else None,
            "depth": float(d) if d is not None else None,
            "stencil": int(s) if s is not None else None,
        }
    except Exception:
        return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Pixel writer lineage via PixelHistory.")
    p.add_argument("capture")
    p.add_argument("--x", type=int, required=True)
    p.add_argument("--y", type=int, required=True)
    p.add_argument("--event", "-e", type=int, default=None, help="Event scope (default: last Present)")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = pixel_lineage(args.capture, args.x, args.y, args.event)
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
