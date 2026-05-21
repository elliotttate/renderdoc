"""Eye-aware event classifier (§8 of the roadmap).

Walks every significant action and classifies it as left/right/unknown using
viewport, scissor, RT dimensions, and stereo slice signals. Tunable through a
JSON config (--config) so we can capture title-specific patterns (e.g. UEVR
side-by-side stereo, double-wide RT, RT slice 0/1).

Usage:
    python -m util.automation.eye_classifier <capture.rdc> [--config <config.json>] [--out <out.jsonl>]

Config keys (all optional):
  mode             "sbs" | "stacked" | "array_slice" | "auto"   (default "auto")
  width            int — full backbuffer width (if absent, inferred from RT)
  height           int — full backbuffer height
  left_viewport_x  int — manual override
  right_viewport_x int — manual override
  swap_eyes        bool — flip left/right
"""

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


def classify_capture(capture_path: str, config=None) -> dict:
    config = config or {}
    mode = config.get("mode", "auto")
    swap = bool(config.get("swap_eyes", False))

    cap, controller = _lib.open_capture(capture_path)
    try:
        textures_by_id = {str(t.resourceId): t for t in controller.GetTextures()}

        # Pass 1: figure out the dominant backbuffer/main RT size if not configured.
        full_w = int(config.get("width", 0))
        full_h = int(config.get("height", 0))
        if full_w == 0 or full_h == 0:
            full_w, full_h = _infer_main_rt(controller, textures_by_id)

        rows = []
        for action in _lib.walk_actions(controller):
            if not (int(action.flags) & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
                continue
            eid = int(action.eventId)
            row = _classify_one(controller, eid, action, textures_by_id, full_w, full_h, mode, swap)
            rows.append(row)

        # Summarise
        per_eye = {"left": 0, "right": 0, "unknown": 0}
        for r in rows:
            per_eye[r["eye"]] = per_eye.get(r["eye"], 0) + 1

        return {
            "capture": os.path.abspath(capture_path),
            "fullSize": [full_w, full_h],
            "mode": mode,
            "swap_eyes": swap,
            "perEyeCounts": per_eye,
            "events": rows,
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def _infer_main_rt(controller, textures_by_id):
    """Find the largest 2D RT used as a color output by any action."""
    best = (0, 0)
    seen = set()
    for action in _lib.walk_actions(controller):
        for o in action.outputs:
            rid = _lib.resource_id_str(o)
            if rid is None or rid in seen:
                continue
            seen.add(rid)
            tex = textures_by_id.get(rid)
            if tex is None:
                continue
            w, h = int(tex.width), int(tex.height)
            if w * h > best[0] * best[1]:
                best = (w, h)
    return best


def _classify_one(controller, eid, action, textures_by_id, full_w, full_h, mode, swap):
    controller.SetFrameEvent(eid, True)
    d3d12 = None
    try:
        d3d12 = controller.GetD3D12PipelineState()
    except Exception:
        pass

    vp = None
    sc = None
    rt_rid = None
    rt_w = full_w
    rt_h = full_h
    rt_slice = None
    if d3d12 is not None:
        if len(d3d12.rasterizer.viewports) > 0:
            v = d3d12.rasterizer.viewports[0]
            vp = (float(v.x), float(v.y), float(v.width), float(v.height))
        if len(d3d12.rasterizer.scissors) > 0:
            s = d3d12.rasterizer.scissors[0]
            sc = (int(s.x), int(s.y), int(s.width), int(s.height))
        if len(d3d12.outputMerger.renderTargets) > 0:
            rt = d3d12.outputMerger.renderTargets[0]
            rt_rid = _lib.resource_id_str(rt.resource)
            rt_slice = int(rt.firstSlice)
            tex = textures_by_id.get(rt_rid)
            if tex is not None:
                rt_w, rt_h = int(tex.width), int(tex.height)

    eye = "unknown"
    reason = ""
    confidence = 0.0

    # Decide effective mode
    eff_mode = mode
    if eff_mode == "auto":
        if rt_w >= 2 * rt_h:
            eff_mode = "sbs"
        elif rt_h >= 2 * rt_w:
            eff_mode = "stacked"
        elif rt_slice is not None and rt_slice in (0, 1):
            eff_mode = "array_slice"

    if eff_mode == "sbs" and vp is not None:
        half = rt_w / 2.0
        if vp[0] + vp[2] / 2.0 < half:
            eye = "left"
        else:
            eye = "right"
        reason = f"sbs viewport x={vp[0]:.0f} w={vp[2]:.0f} of {rt_w}"
        confidence = 0.95
    elif eff_mode == "stacked" and vp is not None:
        half = rt_h / 2.0
        if vp[1] + vp[3] / 2.0 < half:
            eye = "left"
        else:
            eye = "right"
        reason = f"stacked viewport y={vp[1]:.0f} h={vp[3]:.0f} of {rt_h}"
        confidence = 0.9
    elif eff_mode == "array_slice" and rt_slice is not None:
        eye = "left" if rt_slice == 0 else "right"
        reason = f"RT array slice {rt_slice}"
        confidence = 0.85

    if swap and eye in ("left", "right"):
        eye = "right" if eye == "left" else "left"

    return {
        "eventId": eid,
        "eye": eye,
        "reason": reason,
        "confidence": confidence,
        "viewport": vp,
        "scissor": sc,
        "rt": rt_rid,
        "rtSize": [rt_w, rt_h],
        "rtSlice": rt_slice,
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Classify draws/dispatches as left/right eye.")
    p.add_argument("capture")
    p.add_argument("--config", help="JSON config (mode/width/height/swap_eyes)")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    config = {}
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            config = json.load(f)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = classify_capture(args.capture, config)
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
