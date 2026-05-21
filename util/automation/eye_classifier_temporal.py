"""Temporal eye classifier for games that render each eye as a separate pass
to a single-eye-sized RT (no side-by-side).

Many UE5 stereo games — including SN2 vanilla -emulatestereo — render two
sequential render passes per frame, each targeting the same scene-color
RT (not a double-wide one). The viewport-based classifier in
eye_classifier.py can't distinguish them because both eyes' draws share
the same viewport.

This module classifies by chunk ordering instead:

  1. Find a "frame split" event — typically the second ClearRenderTargetView
     of the main scene RT, which marks the start of the second eye's pass.
  2. Events before the split belong to the first eye; events after the
     split belong to the second eye.
  3. Optionally, detect multiple splits per frame (left/right/left/right
     ordering is uncommon but possible).

For SN2 the heuristic is: the scene-color RT is cleared twice per frame.
First clear → start of left-eye pass. Second clear → start of right-eye
pass. Events between are classified accordingly.

Usage::

    python -m util.automation.eye_classifier_temporal <cap.rdc> [--out file]
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
    from automation import eye_classifier as ec  # type: ignore
else:
    from . import _lib
    from . import eye_classifier as ec

import renderdoc as rd  # noqa: E402


def _find_main_rt(controller) -> Optional[Any]:
    """Reuse eye_classifier's RT-shape heuristic."""
    textures_by_id = {str(t.resourceId): t for t in controller.GetTextures()}
    w, h = ec._infer_main_rt(controller, textures_by_id)
    if w == 0:
        return None
    # Return the textureId of the first RT matching that shape (most common
    # scene-color RT)
    counts: Dict[str, int] = {}
    for action in _lib.walk_actions(controller):
        if not (int(action.flags) & int(rd.ActionFlags.Drawcall)):
            continue
        outs = list(action.outputs) if action.outputs else []
        if not outs:
            continue
        rid = _lib.resource_id_str(outs[0])
        if rid is None:
            continue
        tex = textures_by_id.get(rid)
        if tex is None:
            continue
        if int(tex.width) == w and int(tex.height) == h:
            counts[rid] = counts.get(rid, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _find_eye_boundaries(controller, main_rt_str: str) -> List[int]:
    """Walk actions; record event IDs of every Clear that targets ``main_rt_str``."""
    boundaries: List[int] = []
    for action in _lib.walk_actions(controller):
        if not (int(action.flags) & int(rd.ActionFlags.Clear)):
            continue
        # The cleared target is in action.outputs[0] for a typical clear.
        outs = list(action.outputs) if action.outputs else []
        if not outs:
            continue
        rid = _lib.resource_id_str(outs[0])
        if rid == main_rt_str:
            boundaries.append(int(action.eventId))
    return boundaries


def classify(capture_path: str) -> Dict[str, Any]:
    cap, controller = _lib.open_capture(capture_path)
    try:
        main_rt = _find_main_rt(controller)
        if main_rt is None:
            return {"error": "no main scene RT identified"}

        boundaries = _find_eye_boundaries(controller, main_rt)
        if len(boundaries) < 2:
            return {
                "main_rt": main_rt,
                "boundaries": boundaries,
                "error": "fewer than 2 scene-RT clears found — temporal split unavailable",
            }

        # The split point: between the 1st and 2nd clear. Subsequent clears
        # (e.g., 3rd, 4th) are post-process or HUD passes — treat them as
        # belonging to the last eye boundary above them.
        rows: List[Dict[str, Any]] = []
        per_eye = {"left": 0, "right": 0, "unknown": 0}
        for action in _lib.walk_actions(controller):
            if not (int(action.flags) & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
                continue
            eid = int(action.eventId)
            # Find which segment this event falls in
            seg = -1
            for i, b in enumerate(boundaries):
                if eid >= b:
                    seg = i
                else:
                    break
            if seg < 0:
                eye = "unknown"
            else:
                # Even seg = first eye = left; odd seg = right
                eye = "left" if (seg % 2 == 0) else "right"
                # First boundary is bypass setup (clear is at eid=b, draws
                # between b and next clear are this eye's work).
            per_eye[eye] = per_eye.get(eye, 0) + 1
            rows.append({
                "eventId": eid,
                "eye": eye,
                "reason": f"temporal seg={seg} between boundaries {boundaries[:seg + 1][-1:]}..",
                "confidence": 0.8 if eye != "unknown" else 0.0,
            })

        return {
            "capture": capture_path,
            "main_rt": main_rt,
            "boundaries": boundaries,
            "perEyeCounts": per_eye,
            "events": rows,
            "mode": "temporal-clear-boundary",
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Temporal eye classifier (clear-boundary heuristic).")
    p.add_argument("capture")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = classify(args.capture)
    finally:
        rd.ShutdownReplay()

    text = json.dumps(out, indent=2, ensure_ascii=False, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(json.dumps(out.get("perEyeCounts") or out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
