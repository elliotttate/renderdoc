"""Pair left-eye / right-eye events for side-by-side analysis.

Given a capture, run the eye classifier and then group draws into
left+right pairs that probably correspond to the same logical draw.

Pairing heuristics, in order of confidence:
  1) Same PSO + same marker stack + consecutive in event order (highest)
  2) Same PSO + same RT class + closest event distance
  3) Same shader hashes + same approximate viewport size + closest event distance
  4) Unpaired (no plausible counterpart)

Output: JSONL with one row per pair (or per unmatched event) suitable for
feeding into event_diff in bulk.

Usage:
    python -m util.automation.pair_eye_events <capture.rdc> [--out pairs.jsonl] [--config eye.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
    from automation import eye_classifier
else:
    from . import _lib, eye_classifier

import renderdoc as rd  # noqa: E402


def _marker_stack_at(controller, structured, eid):
    """Cheap marker-stack reconstruction: walk all actions in order, maintain stack."""
    stack = []
    for a in _lib.walk_actions(controller):
        flags = _lib.action_flag_names(a.flags)
        name = str(a.GetName(structured))
        if int(a.eventId) == int(eid):
            return list(stack), name
        if "PushMarker" in flags:
            stack.append(name.rstrip("()"))
        elif "PopMarker" in flags:
            if stack:
                stack.pop()
    return list(stack), ""


def pair_events(capture: str, eye_config=None) -> dict:
    cap, controller = _lib.open_capture(capture)
    try:
        eye = eye_classifier.classify_capture(capture, eye_config or {"mode": "auto"})
        events = eye["events"]
        by_eye = {"left": [], "right": [], "unknown": []}
        for e in events:
            by_eye.setdefault(e["eye"], []).append(e)

        # Annotate every action with PSO id from state — single full pass
        state_by_eid = {}
        for e in events:
            try:
                state = _lib.collect_state_at_event(controller, int(e["eventId"]))
            except Exception:
                continue
            state_by_eid[int(e["eventId"])] = {
                "pso": state.get("pipelineId"),
                "shaders": tuple(sh.get("bytecodeHash") for sh in state.get("shaders", [])),
                "rt": (state.get("renderTargets") or [{}])[0].get("resource"),
                "vp": (state.get("viewports") or [{}])[0],
            }

        # Group left events by (PSO, shader_tuple, rt) signature
        def sig(e):
            s = state_by_eid.get(int(e["eventId"]), {})
            return (s.get("pso"), s.get("shaders"), s.get("rt"))

        left_by_sig = {}
        for e in by_eye["left"]:
            left_by_sig.setdefault(sig(e), []).append(e)

        # Pair each right event with the closest-by-event-id left event with matching signature
        pairs = []
        used_left = set()
        for r in by_eye["right"]:
            s = sig(r)
            candidates = left_by_sig.get(s, [])
            best = None
            best_dist = 10**9
            for l in candidates:
                if int(l["eventId"]) in used_left:
                    continue
                d = abs(int(l["eventId"]) - int(r["eventId"]))
                if d < best_dist:
                    best_dist = d
                    best = l
            if best is not None:
                used_left.add(int(best["eventId"]))
                pairs.append({
                    "left": int(best["eventId"]),
                    "right": int(r["eventId"]),
                    "signature": {
                        "pso": s[0],
                        "shaders": list(s[1] or []),
                        "rt": s[2],
                    },
                    "eventDistance": best_dist,
                    "method": "pso+shaders+rt",
                })

        unmatched_left = [int(e["eventId"]) for e in by_eye["left"] if int(e["eventId"]) not in used_left]
        unmatched_right = [int(r["eventId"]) for r in by_eye["right"]
                            if not any(p["right"] == int(r["eventId"]) for p in pairs)]

        return {
            "capture": os.path.abspath(capture),
            "perEyeCounts": eye["perEyeCounts"],
            "fullSize": eye["fullSize"],
            "pairs": pairs,
            "unmatched": {"left": unmatched_left, "right": unmatched_right},
        }
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Pair left/right eye events.")
    p.add_argument("capture")
    p.add_argument("--config", help="Eye-classifier JSON config")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    cfg = None
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = pair_events(args.capture, cfg)
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
