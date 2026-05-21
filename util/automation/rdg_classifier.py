"""Unreal Engine RDG / pass awareness (§12 of the roadmap).

Walks every action and classifies it into an Unreal-engine pass family using
regex patterns on the action name and on its marker ancestors. Patterns are
configurable so the tool doesn't hardcode game-specific assumptions.

Output is a JSONL of {eventId, classes, markerStack}.

Usage:
    python -m util.automation.rdg_classifier <capture.rdc> [--config <classes.json>] [--out <file>]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


DEFAULT_CLASSES = {
    "VolumetricFog": [r"VolumetricFog", r"VBufferA", r"VBufferB", r"IntegratedLightScattering",
                       r"ReconstructVolumetricRenderTarget"],
    "SingleLayerWater": [r"SingleLayerWater"],
    "VirtualShadowMap": [r"VirtualShadowMap"],
    "Nanite": [r"Nanite"],
    "BasePass": [r"BasePass"],
    "ShadowDepth": [r"ShadowDepth"],
    "Lumen": [r"Lumen"],
    "TSR_TAA": [r"TemporalSuperResolution", r"TAA", r"TemporalAA"],
    "PostProcessing": [r"PostProcess"],
    "Composite_SBS": [r"CopyRectPS", r"CombineSlices", r"SideBySide"],
    "Translucency": [r"Translucenc"],
    "GBuffer": [r"GBuffer"],
}


def classify(capture: str, classes_cfg=None) -> dict:
    classes_cfg = classes_cfg or DEFAULT_CLASSES
    compiled = {k: [re.compile(p, re.IGNORECASE) for p in pats] for k, pats in classes_cfg.items()}

    cap, controller = _lib.open_capture(capture)
    try:
        structured = controller.GetStructuredFile()
        rows = []
        # Walk with marker stack: ActionDescription doesn't carry the stack but
        # we rebuild it from PushMarker/PopMarker actions encountered in order.
        marker_stack = []
        for a in _lib.walk_actions(controller):
            flags = _lib.action_flag_names(a.flags)
            name = str(a.GetName(structured))
            if "PushMarker" in flags:
                marker_stack.append(name.rstrip("()"))
                continue
            if "PopMarker" in flags:
                if marker_stack:
                    marker_stack.pop()
                continue
            joined = " | ".join(marker_stack) + " | " + name
            matches = sorted(
                k for k, ps in compiled.items() if any(p.search(joined) for p in ps)
            )
            if not matches and "Drawcall" not in flags and "Dispatch" not in flags:
                continue
            rows.append(
                {
                    "eventId": int(a.eventId),
                    "name": name,
                    "flags": flags,
                    "classes": matches,
                    "markerStack": list(marker_stack),
                }
            )

        # Per-class summary
        counts = {}
        for r in rows:
            for k in r["classes"]:
                counts[k] = counts.get(k, 0) + 1
        return {"capture": os.path.abspath(capture), "classes": list(classes_cfg.keys()), "counts": counts, "events": rows}
    finally:
        controller.Shutdown()
        cap.Shutdown()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Unreal Engine RDG / pass classifier.")
    p.add_argument("capture")
    p.add_argument("--config", help="JSON of {className: [regex,...]} (overrides defaults)")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    cfg = None
    if args.config:
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        out = classify(args.capture, cfg)
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
