"""End-to-end explainer for a problematic pixel (§24 acceptance criteria).

Given a capture and a right-eye pixel, produces a machine-readable report plus
a short human-readable summary that answers:
  - which event last wrote this pixel
  - which shader hash / PSO
  - is it a left/right/unknown eye event
  - resolve t/u/b/s registers to concrete resources
  - list each upstream resource's last writer
  - whether a UEVR override was active (if uevr_ingest output is present)
  - the visible delta after a magenta probe on the implicated PS

Usage:
    python -m util.automation.explain_pixel <capture.rdc> --x 900 --y 250 [--index <dir>] [--uevr-dir <dir>] [--probe]
"""

import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
    from automation import pixel_lineage, eye_classifier, resource_lineage, replay_probe
else:
    from . import _lib, pixel_lineage, eye_classifier, resource_lineage, replay_probe

import renderdoc as rd  # noqa: E402


def explain(capture: str, x: int, y: int, index_dir=None, uevr_dir=None, probe=False) -> dict:
    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        lineage = pixel_lineage.pixel_lineage(capture, x, y)
        if not lineage["history"]:
            return {"summary": "No pixel writers found", "lineage": lineage}

        last_event = lineage["history"][-1]["eventId"]
        state = lineage.get("lastWriterState") or {}

        eye_class = eye_classifier.classify_capture(capture, {"mode": "auto"})
        eye_for_last = next((e for e in eye_class["events"] if e["eventId"] == last_event), None)

        upstream = []
        for b in state.get("bindings", []):
            if b.get("type", "").startswith("ReadOnly") and b.get("resource"):
                lin = resource_lineage.lineage(capture, b["resource"], before_eid=last_event)
                upstream.append(
                    {
                        "binding": {k: b[k] for k in ("stage", "type", "register", "space", "name", "resource", "format")},
                        "lastWriter": lin["lastWriterBefore"],
                    }
                )

        uevr_match = None
        if uevr_dir:
            from . import uevr_ingest

            ingest_out = uevr_ingest.ingest(index_dir or "", uevr_dir)
            for row in ingest_out["rows"]:
                if row["eventId"] == last_event:
                    uevr_match = row
                    break

        probe_result = None
        if probe:
            try:
                ps_shader = next(
                    (s["shaderId"] for s in state.get("shaders", []) if s["stage"] == "Pixel"),
                    None,
                )
                if ps_shader is None:
                    probe_result = {"skipped": "no pixel shader at last writer"}
                else:
                    with replay_probe.ProbeSession(capture) as session:
                        before = session.sample_roi(last_event, max(0, x - 8), max(0, y - 8), 16, 16)
                        session.force_magenta_ps(ps_shader)
                        after = session.sample_roi(last_event, max(0, x - 8), max(0, y - 8), 16, 16)
                        probe_result = {"before": before, "after": after}
            except Exception as exc:
                probe_result = {"error": str(exc)}

        ps_hash = next((s["bytecodeHash"] for s in state.get("shaders", []) if s["stage"] == "Pixel"), None)

        summary = {
            "x": x,
            "y": y,
            "lastWriterEvent": last_event,
            "lastWriterPSO": state.get("pipelineId"),
            "pixelShaderHash": ps_hash,
            "eye": eye_for_last["eye"] if eye_for_last else "unknown",
            "eyeReason": eye_for_last["reason"] if eye_for_last else "",
            "uevrOverrideExpected": bool(uevr_match and uevr_match.get("override_expected")),
        }
        return {
            "summary": summary,
            "pixelLineage": lineage,
            "lastWriterState": state,
            "eyeClassification": eye_for_last,
            "upstreamLineage": upstream,
            "uevr": uevr_match,
            "magentaProbe": probe_result,
        }
    finally:
        rd.ShutdownReplay()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Explain a problematic pixel end-to-end.")
    p.add_argument("capture")
    p.add_argument("--x", type=int, required=True)
    p.add_argument("--y", type=int, required=True)
    p.add_argument("--index")
    p.add_argument("--uevr-dir")
    p.add_argument("--probe", action="store_true", help="Run replay-time magenta-probe on the implicated PS")
    p.add_argument("--out", "-o")
    args = p.parse_args(argv)

    out = explain(args.capture, args.x, args.y, args.index, args.uevr_dir, args.probe)
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
