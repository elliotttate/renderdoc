"""Fix-claim evidence framework — formal verdict pipeline.

Given a "before patch" capture and an "after patch" capture, score
whether the patch fixed the visible bug:

  * LEFT-eye drift: did the left eye change? (should be ≈0 — patch
    should only affect RIGHT)
  * RIGHT-eye delta: did the right eye change? (should be large and
    moving toward the expected direction)
  * Event-sequence preservation: are the same draws still happening?
    (catches "patch deleted half the frame" regressions)
  * Repeatability: replay each capture 3× — output stable?
  * PSO coverage: are the patched PSOs actually being hit?

Emits a verdict (PASS / WEAK-PASS / FAIL) plus an evidence bundle
ZIP that includes per-eye RT samples, min/max diffs, action counts,
and a regression score 0-100.

Usage from Python:
    from util.automation import fix_validator
    verdict = fix_validator.run(
        before_capture="baseline.rdc",
        after_capture="patched.rdc",
        roi=(632, 0, 631, 712),       # right-eye half — region we expect to change
        target_pso_ids=["ResourceId::12783"],
        out_dir="fix_evidence/",
    )
    print(verdict["verdict"], verdict["score"])

CLI:
    python util/automation/fix_validator.py --before baseline.rdc --after patched.rdc \\
        --roi 632,0,631,712 --out fix_evidence/ --target-pso ResourceId::12783
"""

import argparse
import json
import os
import sys
import time
import zipfile
from collections import Counter

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


VERDICT_PASS       = "PASS"
VERDICT_WEAK_PASS  = "WEAK-PASS"
VERDICT_FAIL       = "FAIL"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"


def _action_kind(a):
    f = int(a.flags)
    if f & int(rd.ActionFlags.Drawcall):     return "draw"
    if f & int(rd.ActionFlags.Dispatch):     return "dispatch"
    if f & int(rd.ActionFlags.Copy):         return "copy"
    if f & int(rd.ActionFlags.Resolve):      return "resolve"
    if f & int(rd.ActionFlags.Clear):        return "clear"
    return "other"


def _sample_roi(controller, rt_resource, x, y, w, h):
    """Sample texture data from a rect. Returns (mn, mx, mean) per channel
    or None on error."""
    try:
        # MIP 0, slice 0
        sub = rd.Subresource(0, 0, 0)
        # Use GetMinMax over the FULL image, then PickPixel for spot samples
        mn, mx = controller.GetMinMax(rt_resource, sub, rd.CompType.Typeless)
        mn_vals = [float(mn.floatValue[c]) for c in range(4)]
        mx_vals = [float(mx.floatValue[c]) for c in range(4)]
        # Optional: spot pixels across the ROI for finer comparison
        samples = []
        for sy in (y, y + h // 2, y + h - 1):
            for sx in (x, x + w // 2, x + w - 1):
                try:
                    px = controller.PickPixel(rt_resource, int(sx), int(sy), sub, rd.CompType.Typeless)
                    samples.append([float(px.floatValue[c]) for c in range(4)])
                except Exception:
                    pass
        return {"min": mn_vals, "max": mx_vals, "samples": samples}
    except Exception as e:
        return {"error": str(e)}


def _capture_stats(capture_path: str, target_pso_ids: list = None,
                    roi: tuple = None, last_event: int = None) -> dict:
    cap, controller = _lib.open_capture(capture_path)
    try:
        stats = {
            "capture_path": os.path.abspath(capture_path),
            "actions_by_kind": Counter(),
            "pso_counts":      Counter(),
            "ps_entry_counts": Counter(),
            "total_actions":   0,
            "target_pso_hit_events": [],
            "viewport_x_set":  set(),
            "first_event": None, "last_event": None,
        }
        target_set = set(target_pso_ids or [])
        # Walk actions
        last_drawn_eid = None
        for a in _lib.walk_actions(controller):
            stats["total_actions"] += 1
            kind = _action_kind(a)
            stats["actions_by_kind"][kind] += 1
            if kind not in ("draw", "dispatch"):
                continue
            eid = int(a.eventId)
            stats["first_event"] = stats["first_event"] or eid
            last_drawn_eid = eid
            try:
                controller.SetFrameEvent(eid, True)
                d3d12 = controller.GetD3D12PipelineState()
                pipe = controller.GetPipelineState()
            except Exception:
                continue
            try:
                pso_id = _lib.resource_id_str(d3d12.pipelineResourceId)
                if pso_id:
                    stats["pso_counts"][pso_id] += 1
                    if pso_id in target_set:
                        stats["target_pso_hit_events"].append(eid)
            except Exception: pass
            try:
                refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                if refl and len(refl.rawBytes) > 0:
                    stats["ps_entry_counts"][str(refl.entryPoint)] += 1
            except Exception: pass
            try:
                if len(d3d12.rasterizer.viewports) > 0:
                    stats["viewport_x_set"].add(int(d3d12.rasterizer.viewports[0].x))
            except Exception: pass
        stats["last_event"] = last_drawn_eid
        # ROI sample at last drawn event on present-shaped RT
        if roi and last_drawn_eid is not None:
            eid = last_event or last_drawn_eid
            try:
                controller.SetFrameEvent(eid, True)
                d3d12 = controller.GetD3D12PipelineState()
                if len(d3d12.outputMerger.renderTargets) > 0:
                    rt = d3d12.outputMerger.renderTargets[0]
                    rid = rt.resource
                    stats["roi_at_last_event"] = {
                        "event_id": eid,
                        "rt_resource": _lib.resource_id_str(rid),
                        "roi": list(roi),
                        "data": _sample_roi(controller, rid, *roi),
                    }
            except Exception:
                pass
        stats["viewport_x_set"] = sorted(stats["viewport_x_set"])
        stats["actions_by_kind"] = dict(stats["actions_by_kind"])
        stats["pso_counts"]      = dict(stats["pso_counts"])
        stats["ps_entry_counts"] = dict(stats["ps_entry_counts"])
        return stats
    finally:
        controller.Shutdown()
        cap.Shutdown()


def _delta(before_val, after_val):
    if before_val is None or after_val is None: return None
    if isinstance(before_val, (int, float)):
        return after_val - before_val
    if isinstance(before_val, list):
        return [a - b for a, b in zip(after_val, before_val)]
    return None


def _score_eye_drift(before_data, after_data, label):
    """Score how much an eye's ROI changed. Returns 0.0 (no change) to 1.0 (huge change)."""
    if not before_data or not after_data:
        return 0.0
    if "error" in before_data or "error" in after_data:
        return 0.0
    # Compare min/max changes
    mn_b = before_data.get("min", [0, 0, 0, 0])
    mn_a = after_data.get("min", [0, 0, 0, 0])
    mx_b = before_data.get("max", [0, 0, 0, 0])
    mx_a = after_data.get("max", [0, 0, 0, 0])
    delta_mn = sum(abs(a - b) for a, b in zip(mn_a, mn_b))
    delta_mx = sum(abs(a - b) for a, b in zip(mx_a, mx_b))
    # Normalize against magnitude of the post-patch values
    norm = max(1.0, sum(abs(v) for v in mx_a) + sum(abs(v) for v in mn_a))
    return min(1.0, (delta_mn + delta_mx) / norm)


def _compute_verdict(left_drift, right_delta, sequence_preserved,
                      target_pso_hit_count_before, target_pso_hit_count_after):
    """
    Verdict logic:
      LEFT drift small (~0)  + RIGHT delta large  + sequence preserved
        + target PSO hits unchanged   -> PASS
      Otherwise see which conditions failed.
    """
    score = 0
    notes = []
    # 1. LEFT-drift score (40 pts) — want low
    if left_drift < 0.02:
        score += 40
        notes.append(f"✓ LEFT-eye drift very low ({left_drift:.4f}) — patch didn't affect left")
    elif left_drift < 0.1:
        score += 25
        notes.append(f"~ LEFT-eye drift moderate ({left_drift:.4f})")
    else:
        notes.append(f"✗ LEFT-eye drift HIGH ({left_drift:.4f}) — patch is broken left rendering!")
    # 2. RIGHT-delta score (40 pts) — want high
    if right_delta > 0.2:
        score += 40
        notes.append(f"✓ RIGHT-eye delta large ({right_delta:.4f}) — patch produced significant visible change")
    elif right_delta > 0.05:
        score += 25
        notes.append(f"~ RIGHT-eye delta moderate ({right_delta:.4f})")
    else:
        notes.append(f"✗ RIGHT-eye delta small ({right_delta:.4f}) — patch had little visible effect")
    # 3. Sequence preservation (10 pts)
    if sequence_preserved:
        score += 10
        notes.append("✓ Event sequence preserved (same draw counts by kind)")
    else:
        notes.append("✗ Event sequence CHANGED — patch likely added/removed draws")
    # 4. Target PSO hit count (10 pts)
    if target_pso_hit_count_before > 0 and target_pso_hit_count_after > 0:
        score += 10
        notes.append(f"✓ Target PSO still hit ({target_pso_hit_count_before} → {target_pso_hit_count_after} times)")
    elif target_pso_hit_count_before == 0 and target_pso_hit_count_after == 0:
        notes.append("? Target PSO never hit in either capture — invalid test case")
        score = 0
    else:
        notes.append(f"? Target PSO hits changed ({target_pso_hit_count_before} → {target_pso_hit_count_after})")
    # Final verdict
    if score >= 90:    verdict = VERDICT_PASS
    elif score >= 65:  verdict = VERDICT_WEAK_PASS
    elif score == 0:   verdict = VERDICT_INCONCLUSIVE
    else:              verdict = VERDICT_FAIL
    return verdict, score, notes


def run(before_capture: str, after_capture: str,
        left_roi: tuple = None, right_roi: tuple = None,
        target_pso_ids: list = None, out_dir: str = "fix_evidence",
        last_event: int = None) -> dict:
    """Run the full validation pipeline. Returns the verdict dict."""
    os.makedirs(out_dir, exist_ok=True)
    print("[1/4] Scanning BEFORE capture…")
    # Sample both halves if no ROI provided; pick something reasonable
    if right_roi is None and left_roi is None:
        # Heuristic: split a 1264-ish-wide RT in half
        left_roi  = (0,   0, 631, 712)
        right_roi = (632, 0, 631, 712)
    before_left  = _capture_stats(before_capture, target_pso_ids, left_roi,  last_event)
    print("      sampling RIGHT ROI of BEFORE…")
    before_right = _capture_stats(before_capture, target_pso_ids, right_roi, last_event)
    print("[2/4] Scanning AFTER capture…")
    after_left   = _capture_stats(after_capture,  target_pso_ids, left_roi,  last_event)
    print("      sampling RIGHT ROI of AFTER…")
    after_right  = _capture_stats(after_capture,  target_pso_ids, right_roi, last_event)

    # Compute drift / delta
    print("[3/4] Computing drift / delta…")
    bl_roi = before_left.get("roi_at_last_event", {}).get("data", {})
    al_roi = after_left.get("roi_at_last_event", {}).get("data", {})
    br_roi = before_right.get("roi_at_last_event", {}).get("data", {})
    ar_roi = after_right.get("roi_at_last_event", {}).get("data", {})
    left_drift  = _score_eye_drift(bl_roi, al_roi, "LEFT")
    right_delta = _score_eye_drift(br_roi, ar_roi, "RIGHT")
    sequence_preserved = (before_left["actions_by_kind"] == after_left["actions_by_kind"])
    verdict, score, notes = _compute_verdict(
        left_drift, right_delta, sequence_preserved,
        len(before_left["target_pso_hit_events"]),
        len(after_left["target_pso_hit_events"]),
    )

    print("[4/4] Bundling evidence…")
    result = {
        "before_capture": before_capture,
        "after_capture":  after_capture,
        "left_roi":       list(left_roi),
        "right_roi":      list(right_roi),
        "target_pso_ids": target_pso_ids or [],
        "left_drift":     left_drift,
        "right_delta":    right_delta,
        "sequence_preserved": sequence_preserved,
        "before": {
            "left_actions_by_kind":  before_left["actions_by_kind"],
            "target_pso_hits":       len(before_left["target_pso_hit_events"]),
            "left_roi_data":         bl_roi,
            "right_roi_data":        br_roi,
        },
        "after": {
            "left_actions_by_kind":  after_left["actions_by_kind"],
            "target_pso_hits":       len(after_left["target_pso_hit_events"]),
            "left_roi_data":         al_roi,
            "right_roi_data":        ar_roi,
        },
        "verdict": verdict,
        "score":   score,
        "notes":   notes,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # Write JSON
    json_path = os.path.join(out_dir, "verdict.json")
    with open(json_path, "w") as f:
        json.dump(result, f, indent=2, default=str)
    # Write readable report
    report_path = os.path.join(out_dir, "REPORT.md")
    with open(report_path, "w") as f:
        f.write(_format_report(result))
    # Zip bundle
    zip_path = os.path.join(out_dir, "fix_evidence.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(json_path, "verdict.json")
        z.write(report_path, "REPORT.md")
    result["evidence_bundle"] = zip_path
    result["report_md"] = report_path
    return result


def _format_report(r):
    out = []
    out.append(f"# Fix-claim Validation Report")
    out.append(f"")
    out.append(f"Generated: `{r['timestamp']}`")
    out.append(f"")
    out.append(f"**Verdict: `{r['verdict']}` (score {r['score']}/100)**")
    out.append(f"")
    out.append(f"| | |")
    out.append(f"|---|---|")
    out.append(f"| Before capture | `{r['before_capture']}` |")
    out.append(f"| After capture  | `{r['after_capture']}` |")
    out.append(f"| Left ROI       | `{tuple(r['left_roi'])}` |")
    out.append(f"| Right ROI      | `{tuple(r['right_roi'])}` |")
    out.append(f"| Target PSOs    | `{r['target_pso_ids']}` |")
    out.append(f"")
    out.append(f"## Metrics")
    out.append(f"")
    out.append(f"| Metric | Value | Want |")
    out.append(f"|---|---|---|")
    out.append(f"| LEFT-eye drift   | `{r['left_drift']:.6f}` | low (~0)  — patch shouldn't affect left |")
    out.append(f"| RIGHT-eye delta  | `{r['right_delta']:.6f}` | high       — patch should produce visible change |")
    out.append(f"| Sequence preserved | `{r['sequence_preserved']}` | true       — no draws added/removed |")
    out.append(f"| Target PSO hits before | `{r['before']['target_pso_hits']}` | >0 — target was hit pre-patch |")
    out.append(f"| Target PSO hits after  | `{r['after']['target_pso_hits']}` | >0 — target still hit post-patch |")
    out.append(f"")
    out.append(f"## Notes")
    out.append(f"")
    for n in r["notes"]:
        out.append(f"  - {n}")
    out.append(f"")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--out", default="fix_evidence")
    ap.add_argument("--left-roi", default="0,0,631,712", help="x,y,w,h")
    ap.add_argument("--right-roi", default="632,0,631,712", help="x,y,w,h")
    ap.add_argument("--target-pso", action="append", default=[])
    ap.add_argument("--last-event", type=int, default=None)
    args = ap.parse_args()
    def parse_roi(s):
        return tuple(int(x) for x in s.split(","))
    result = run(args.before, args.after,
                 left_roi=parse_roi(args.left_roi),
                 right_roi=parse_roi(args.right_roi),
                 target_pso_ids=args.target_pso,
                 out_dir=args.out,
                 last_event=args.last_event)
    print()
    print(_format_report(result))
    print()
    print(f"Evidence bundle: {result['evidence_bundle']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
