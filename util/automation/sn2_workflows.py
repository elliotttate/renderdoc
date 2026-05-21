"""Runnable SN2 debugging workflows (§23 of the roadmap).

Each subcommand is a thin orchestrator on top of the existing modules. Treat
this as a recipe book — the underlying tools all work on any D3D12 capture,
SN2 is just the original motivation.

Available recipes:
  pso-events       "Is pso3069 / shader X still active?"
  override-fired   "Did the right-eye override fire?"
  first-bad-input  "What is the first bad input?"
  t9-vs-t5         "Is t9 bad or is t5 math bad?"
  view-cbv-diff    "Did View CBVs diverge correctly?"

Usage example:
    python -m util.automation.sn2_workflows pso-events <cap.rdc> --shader-hash 166dba88
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
        cbv_tools,
        eye_classifier,
        pixel_lineage as autom_pixel,
        replay_probe,
        resource_lineage as autom_reslineage,
    )
else:
    from . import (
        _lib,
        cbv_tools,
        eye_classifier,
        pixel_lineage as autom_pixel,
        replay_probe,
        resource_lineage as autom_reslineage,
    )

import renderdoc as rd  # noqa: E402


def pso_events(capture: str, shader_hash: str, stage: str = "Pixel") -> dict:
    """Find every action whose given stage uses the shader bytecode hash."""
    cap, controller = _lib.open_capture(capture)
    try:
        hits = []
        for a in _lib.walk_actions(controller):
            if not (int(a.flags) & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
                continue
            try:
                state = _lib.collect_state_at_event(controller, int(a.eventId))
            except Exception:
                continue
            for sh in state.get("shaders", []):
                if sh.get("stage") == stage and sh.get("bytecodeHash", "").lower().startswith(shader_hash.lower()):
                    hits.append(
                        {
                            "eventId": int(a.eventId),
                            "name": str(a.GetName(controller.GetStructuredFile())),
                            "pipelineId": state.get("pipelineId"),
                            "viewport": state.get("viewports", [{}])[0],
                            "rt": (state.get("renderTargets") or [{}])[0].get("resource"),
                        }
                    )
                    break
        eye = eye_classifier.classify_capture(capture, {"mode": "auto"})
        eye_by_event = {e["eventId"]: e for e in eye["events"]}
        for h in hits:
            h["eye"] = eye_by_event.get(h["eventId"], {}).get("eye", "unknown")
        return {"shaderHash": shader_hash, "stage": stage, "hits": hits, "count": len(hits)}
    finally:
        controller.Shutdown()
        cap.Shutdown()


def override_fired(capture: str, original_hash: str, replacement_hash: str, stage="Pixel") -> dict:
    """Compare bound shader hash against expected replacement, per eye."""
    cap, controller = _lib.open_capture(capture)
    try:
        eye = eye_classifier.classify_capture(capture, {"mode": "auto"})
        eye_by_event = {e["eventId"]: e for e in eye["events"]}
        rows = []
        for a in _lib.walk_actions(controller):
            if not (int(a.flags) & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
                continue
            try:
                state = _lib.collect_state_at_event(controller, int(a.eventId))
            except Exception:
                continue
            for sh in state.get("shaders", []):
                if sh.get("stage") != stage:
                    continue
                bh = (sh.get("bytecodeHash") or "").lower()
                e = eye_by_event.get(int(a.eventId), {}).get("eye", "unknown")
                if bh.startswith(original_hash.lower()):
                    rows.append({"eventId": int(a.eventId), "eye": e, "fired": False, "bound": bh})
                elif bh.startswith(replacement_hash.lower()):
                    rows.append({"eventId": int(a.eventId), "eye": e, "fired": True, "bound": bh})
        summary = {
            "right_total": sum(1 for r in rows if r["eye"] == "right"),
            "right_fired": sum(1 for r in rows if r["eye"] == "right" and r["fired"]),
            "left_total": sum(1 for r in rows if r["eye"] == "left"),
            "left_fired": sum(1 for r in rows if r["eye"] == "left" and r["fired"]),
        }
        return {"original": original_hash, "replacement": replacement_hash, "summary": summary, "rows": rows}
    finally:
        controller.Shutdown()
        cap.Shutdown()


def first_bad_input(capture: str, x: int, y: int) -> dict:
    """Walk backward through the pixel writers and compare each sampled
    texture against its 'left-eye' counterpart if any can be inferred.

    Heuristic: the upstream comparison just picks the last writer of each
    sampled texture and reports the binding for human review. Full
    left-vs-right pair matching requires per-game heuristics.
    """
    lineage = autom_pixel.pixel_lineage(capture, x, y)
    state = lineage.get("lastWriterState") or {}
    sampled = []
    for b in state.get("bindings", []):
        if b.get("type", "").startswith("ReadOnly") and b.get("resource"):
            sampled.append(b)
    upstream = []
    for b in sampled:
        lin = autom_reslineage.lineage(capture, b["resource"], before_eid=lineage["history"][-1]["eventId"])
        upstream.append({"binding": b, "lastWriterBefore": lin["lastWriterBefore"]})
    return {"pixelLineage": lineage, "sampledTextures": sampled, "upstreamWriters": upstream}


def t9_vs_t5(capture: str, event_id: int, t9_swap_with: str = None) -> dict:
    """Swap t9 with a sibling texture and resample the ROI to see whether t9
    is the culprit. If `t9_swap_with` is given, swap to that resource id;
    otherwise just sample the baseline (no mutation).
    """
    out = {}
    with replay_probe.ProbeSession(capture) as s:
        out["before"] = s.sample_roi(int(event_id), 0, 0, 256, 256)
        if t9_swap_with:
            # Identify t9 resource at the event
            state = _lib.collect_state_at_event(s.controller, int(event_id))
            t9 = None
            for b in state.get("bindings", []):
                if b.get("stage") == "Pixel" and b.get("register") == 9 and b.get("type", "").startswith("Read"):
                    t9 = b.get("resource")
                    break
            if t9 is None:
                return {"error": "t9 binding not found at event"}
            s.swap_resource(t9, t9_swap_with)
            out["swappedT9"] = {"orig": t9, "repl": t9_swap_with}
            out["after"] = s.sample_roi(int(event_id), 0, 0, 256, 256)
    return out


def view_cbv_diff(capture: str, event_left: int, event_right: int, slot=0) -> dict:
    """Compare a View constant buffer between left/right eye events."""
    a = cbv_tools.dump(capture, event_left, slot)
    b = cbv_tools.dump(capture, event_right, slot)
    if "error" in a or "error" in b:
        return {"left": a, "right": b}
    import struct
    fa = list(struct.unpack(f"<{min(len(a['hex'])//8, 4096)}f", bytes.fromhex(a["hex"])[: min(len(a['hex'])//8, 4096) * 4]))
    fb = list(struct.unpack(f"<{min(len(b['hex'])//8, 4096)}f", bytes.fromhex(b["hex"])[: min(len(b['hex'])//8, 4096) * 4]))
    deltas = []
    n = min(len(fa), len(fb))
    for i in range(n):
        d = fb[i] - fa[i]
        if abs(d) > 1e-7:
            deltas.append({"index": i, "left": fa[i], "right": fb[i], "delta": d})
    return {"eventLeft": event_left, "eventRight": event_right, "slot": slot, "deltas": deltas[:512]}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="SN2-style end-to-end debugging recipes.")
    sub = p.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("pso-events")
    p1.add_argument("capture")
    p1.add_argument("--shader-hash", required=True, help="MD5 prefix (e.g. 166dba88)")
    p1.add_argument("--stage", default="Pixel")

    p2 = sub.add_parser("override-fired")
    p2.add_argument("capture")
    p2.add_argument("--orig", required=True)
    p2.add_argument("--repl", required=True)
    p2.add_argument("--stage", default="Pixel")

    p3 = sub.add_parser("first-bad-input")
    p3.add_argument("capture")
    p3.add_argument("--x", type=int, required=True)
    p3.add_argument("--y", type=int, required=True)

    p4 = sub.add_parser("t9-vs-t5")
    p4.add_argument("capture")
    p4.add_argument("--event", type=int, required=True)
    p4.add_argument("--swap-with", default=None, help="ResourceId to use in place of t9")

    p5 = sub.add_parser("view-cbv-diff")
    p5.add_argument("capture")
    p5.add_argument("--event-left", type=int, required=True)
    p5.add_argument("--event-right", type=int, required=True)
    p5.add_argument("--slot", default="0")

    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        if args.cmd == "pso-events":
            out = pso_events(args.capture, args.shader_hash, args.stage)
        elif args.cmd == "override-fired":
            out = override_fired(args.capture, args.orig, args.repl, args.stage)
        elif args.cmd == "first-bad-input":
            out = first_bad_input(args.capture, args.x, args.y)
        elif args.cmd == "t9-vs-t5":
            out = t9_vs_t5(args.capture, args.event, args.swap_with)
        else:
            slot = int(args.slot) if args.slot.lstrip("-").isdigit() else args.slot
            out = view_cbv_diff(args.capture, args.event_left, args.event_right, slot)
    finally:
        rd.ShutdownReplay()
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
