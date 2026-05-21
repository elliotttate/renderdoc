"""Compare eyes end-to-end.

Combines pair_eye_events + event_diff + eye_image_diff (+ optional
shader_debug + geometry_diff) into one report that, for each detected
left/right pair, surfaces every concrete divergence — binding, CBV byte,
RT pixel, shader input — ranked by how impactful the divergence appears.

This is the single command most useful for "right eye looks wrong" triage.

Usage:
    python -m util.automation.compare_eyes <cap.rdc> [--max-pairs 25] [--probe-shader-debug] [--out file]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
    from automation import (
        event_diff as autom_event_diff,
        eye_image_diff,
        geometry_diff,
        pair_eye_events,
        shader_debug,
    )
else:
    from . import (
        _lib,
        event_diff as autom_event_diff,
        eye_image_diff,
        geometry_diff,
        pair_eye_events,
        shader_debug,
    )

import renderdoc as rd  # noqa: E402


def _score(pair_report):
    """Rank pair-reports by 'how interesting' the divergence is."""
    score = 0
    d = pair_report.get("eventDiff", {}).get("differences", {})
    if "shaders" in d:
        score += 50
    if "bindings" in d:
        score += 20 + 5 * min(20, len(d["bindings"]))
    if "cbvBytes" in d:
        score += 30
    if "renderTargets" in d:
        score += 15
    img = pair_report.get("imageDiff", {})
    if img.get("perChannelDiff"):
        for c in img["perChannelDiff"]:
            if c.get("differingPixels", 0) > 0:
                score += min(40, c["differingPixels"] // 1000)
    return score


def compare(capture: str, max_pairs=25, probe_shader_debug=False) -> dict:
    pairs = pair_eye_events.pair_events(capture, {"mode": "auto"})
    out = {
        "capture": os.path.abspath(capture),
        "perEyeCounts": pairs["perEyeCounts"],
        "fullSize": pairs["fullSize"],
        "pairs": [],
    }

    # Limit to the most "interesting" pairs first
    selected = pairs["pairs"][:max_pairs]
    for pair in selected:
        l = pair["left"]
        r = pair["right"]
        ed = autom_event_diff.event_diff(capture, l, r, with_cbv_bytes=True)
        report = {
            "left": l,
            "right": r,
            "signature": pair["signature"],
            "eventDiff": ed,
        }
        # Image diff if RTs are the same resource
        if ed["differences"].get("renderTargets") is None:
            try:
                report["imageDiff"] = eye_image_diff.eye_image_diff(capture, l, r)
            except Exception as exc:
                report["imageDiff"] = {"error": str(exc)}
        # Geometry diff (cheap)
        try:
            report["geometryDiff"] = geometry_diff.compare_geometry(capture, l, r, "VSOut")
        except Exception as exc:
            report["geometryDiff"] = {"error": str(exc)}
        # Optional shader debug compare at the RT center
        if probe_shader_debug:
            try:
                fs = pairs.get("fullSize") or [0, 0]
                x = max(0, fs[0] // 4)
                y = max(0, fs[1] // 2)
                report["shaderDebug"] = shader_debug.compare_pixels(capture, l, r, x, y)
            except Exception as exc:
                report["shaderDebug"] = {"error": str(exc)}
        report["score"] = _score(report)
        out["pairs"].append(report)

    out["pairs"].sort(key=lambda p: p["score"], reverse=True)
    out["topInteresting"] = [{"left": p["left"], "right": p["right"], "score": p["score"]} for p in out["pairs"][:10]]
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="End-to-end left-vs-right eye comparison.")
    p.add_argument("capture")
    p.add_argument("--max-pairs", type=int, default=25)
    p.add_argument("--probe-shader-debug", action="store_true",
                   help="Run shader_debug.compare on each pair at RT center")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = compare(args.capture, args.max_pairs, args.probe_shader_debug)
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
