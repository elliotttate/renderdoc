"""Full SN2 right-eye investigation, top-to-bottom using the new tools.

Orchestrates the new analysis modules in a single qrenderdoc-embedded
Python run so we don't pay the .rdc-load cost N times.

Outputs to $SN2_INVESTIGATION_OUT (default E:/tmp_dir/sn2_invest_full):

  Stage A — eye classification + matched event pairs
  Stage B — stereo divergence top-N
  Stage C — for each top pair: state snapshots, dispatch dimensions,
            compute writers of bound t5 resource, fix proposal
  Stage D — descriptor heap heatmap (mismatched slots only)
  Stage E — debug pixel pair on the top divergent pair (best-effort)
  Stage F — descriptor copy log + write-history merged timeline

Each stage logs progress and writes its own artefacts; if any stage fails
the rest still runs.
"""

import json
import os
import sys
import time
import traceback

LOG_DIR = os.environ.get("SN2_INVESTIGATION_OUT", r"E:\tmp_dir\sn2_invest_full")
CAPTURE = os.environ.get(
    "SN2_INVESTIGATION_CAPTURE",
    r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc",
)
TOP_N = int(os.environ.get("SN2_TOP_N", "5"))

os.makedirs(LOG_DIR, exist_ok=True)
PROGRESS = os.path.join(LOG_DIR, "_progress.log")


def log(msg):
    line = f"[sn2-full {time.strftime('%H:%M:%S')}] {msg}"
    with open(PROGRESS, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
    try:
        print(line, flush=True)
    except Exception:
        pass


with open(PROGRESS, "w", encoding="utf-8") as f:
    pass

log(f"capture: {CAPTURE}")
log(f"out:     {LOG_DIR}")
log(f"top-N:   {TOP_N}")

if not os.path.isfile(CAPTURE):
    log("FATAL: capture not found")
    os._exit(2)

sys.path.insert(0, r"E:\Github\renderdoc")

try:
    from util.automation import (
        _lib,
        eye_classifier,
        eye_classifier_temporal,
        pair_eye_events,
        stereo_divergence,
        compute_writers,
        descriptor_heap_heatmap,
        fix_proposal,
        debug_pixel_pair,
        d3d12_copy_descriptors,
    )
    import renderdoc as rd
except Exception:
    log(traceback.format_exc())
    os._exit(3)


# For SN2-style captures (single-eye-sized scene RT rendered twice),
# monkey-patch the eye_classifier and pair_eye_events modules to use the
# temporal classifier instead of the viewport-based one. This drops
# the 13-min per-action SetFrameEvent walk and produces sane per-eye counts.
def _temporal_classify(capture_path, config=None):
    return eye_classifier_temporal.classify(capture_path)


# Override the eye classification used by pair_eye_events and
# stereo_divergence — both call eye_classifier.classify_capture().
eye_classifier.classify_capture = _temporal_classify
log("PATCHED eye_classifier.classify_capture -> temporal classifier")


def write_json(name, payload):
    path = os.path.join(LOG_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    log(f"  -> {name}  ({os.path.getsize(path)} bytes)")


# -------------------------------------------------------------------------
# Stage A: eye classification + matched event pairs
# -------------------------------------------------------------------------
eye_data = None
pairs_data = None
try:
    log("=== Stage A: eye classification ===")
    t0 = time.time()
    eye_data = eye_classifier.classify_capture(CAPTURE, {"mode": "auto"})
    write_json("a_eye_classification.json", eye_data)
    per_eye = eye_data.get("perEyeCounts", {})
    log(f"  per-eye counts: {per_eye}")
    log(f"  duration: {time.time() - t0:.1f}s")

    log("=== Stage A: pairing events ===")
    t0 = time.time()
    pairs_data = pair_eye_events.pair_events(CAPTURE)
    write_json("a_event_pairs.json", pairs_data)
    log(f"  pair count: {len(pairs_data.get('pairs', []))}")
    log(f"  duration: {time.time() - t0:.1f}s")
except Exception:
    log("Stage A failed:\n" + traceback.format_exc())


# -------------------------------------------------------------------------
# Stage B: stereo divergence top-N
# -------------------------------------------------------------------------
top_pairs = []
try:
    log(f"=== Stage B: stereo divergence top-{TOP_N} ===")
    t0 = time.time()
    div_data = stereo_divergence.rank(CAPTURE, top_n=TOP_N)
    write_json("b_stereo_divergence.json", div_data)
    top_pairs = div_data.get("top", [])
    log(f"  pairs scored: {div_data.get('scoredPairs', 0)}")
    for i, p in enumerate(top_pairs[:TOP_N]):
        log(f"  #{i + 1}: L={p['leftEventId']} R={p['rightEventId']}  score={p['score']:.2f}")
        bd = p.get("breakdown", {})
        if "rightDispatchDead" in bd:
            log(f"      RIGHT DISPATCH DEAD (X=0): {bd.get('dispatchDimensionDelta')}")
        for k in ("rtResourceDelta", "bindingDeltas", "shaderHashDeltas",
                  "dispatchDimensionDelta", "cb0HashDelta"):
            if k in bd:
                log(f"      {k}: {str(bd[k])[:200]}")
    log(f"  duration: {time.time() - t0:.1f}s")
except Exception:
    log("Stage B failed:\n" + traceback.format_exc())


# -------------------------------------------------------------------------
# Stage C: per-pair deep dive
# -------------------------------------------------------------------------
try:
    log("=== Stage C: deep-dive on top divergent pairs ===")
    cap, controller = _lib.open_capture(CAPTURE)
    try:
        for i, p in enumerate(top_pairs[:TOP_N]):
            left_eid = int(p["leftEventId"])
            right_eid = int(p["rightEventId"])
            log(f"--- pair #{i + 1}: L={left_eid} R={right_eid} ---")
            try:
                state_l = _lib.collect_state_at_event(controller, left_eid)
                state_r = _lib.collect_state_at_event(controller, right_eid)
                write_json(f"c_pair{i + 1}_state_left.json", state_l)
                write_json(f"c_pair{i + 1}_state_right.json", state_r)
            except Exception:
                log(f"  state snapshot failed:\n{traceback.format_exc()}")

            # find PS t5 resource on each side
            for side, state in (("left", state_l), ("right", state_r)):
                for b in state.get("bindings", []):
                    if b.get("stage") == "Pixel" and b.get("register") == 5 and \
                            (b.get("type") or "").startswith("Read"):
                        log(f"  {side} PS t5 -> {b.get('resource')}  (heap {b.get('heap')} +{b.get('heapByteOffset')})")
                        break
    finally:
        controller.Shutdown()
        cap.Shutdown()
except Exception:
    log("Stage C state-dump failed:\n" + traceback.format_exc())


# -------------------------------------------------------------------------
# Stage C2: compute writers for the right-eye PS t5 resource of top pair
# -------------------------------------------------------------------------
try:
    if top_pairs:
        right_eid = int(top_pairs[0]["rightEventId"])
        cap, controller = _lib.open_capture(CAPTURE)
        t5_resource = None
        try:
            state = _lib.collect_state_at_event(controller, right_eid)
            for b in state.get("bindings", []):
                if b.get("stage") == "Pixel" and b.get("register") == 5 and \
                        (b.get("type") or "").startswith("Read"):
                    t5_resource = b.get("resource")
                    break
        finally:
            controller.Shutdown()
            cap.Shutdown()

        if t5_resource:
            log(f"=== Stage C2: compute writers for right-eye t5 ({t5_resource}) ===")
            t0 = time.time()
            writers = compute_writers.find_writers(CAPTURE, t5_resource, eye_classify=True)
            write_json("c2_compute_writers_t5.json", writers)
            log(f"  writeCount: {writers.get('summary', {}).get('writeCount')}")
            log(f"  deadDispatches: {writers.get('summary', {}).get('deadDispatches')}")
            log(f"  perEyeWrites: {writers.get('summary', {}).get('perEyeWrites')}")
            log(f"  duration: {time.time() - t0:.1f}s")
except Exception:
    log("Stage C2 failed:\n" + traceback.format_exc())


# -------------------------------------------------------------------------
# Stage D: descriptor heap heatmap (mismatch-only summary)
# -------------------------------------------------------------------------
try:
    log("=== Stage D: descriptor heap heatmap ===")
    t0 = time.time()
    heat = descriptor_heap_heatmap.build_heatmap(CAPTURE)
    # Keep only mismatched rows in the output to save disk
    mismatch_rows = [r for r in heat.get("rows", []) if r.get("mismatch")]
    heat_compact = {"summary": heat.get("summary", {}), "mismatchRows": mismatch_rows}
    write_json("d_descriptor_heap_heatmap_mismatch.json", heat_compact)
    log(f"  total slots: {heat.get('summary', {}).get('totalSlots')}")
    log(f"  mismatches:  {heat.get('summary', {}).get('mismatches')}")
    log(f"  duration: {time.time() - t0:.1f}s")
except Exception:
    log("Stage D failed:\n" + traceback.format_exc())


# -------------------------------------------------------------------------
# Stage E: fix proposal for top pair
# -------------------------------------------------------------------------
try:
    if top_pairs:
        L = int(top_pairs[0]["leftEventId"])
        R = int(top_pairs[0]["rightEventId"])
        log(f"=== Stage E: fix proposal for L={L} R={R} ===")
        prop = fix_proposal.propose(CAPTURE, L, R)
        write_json("e_fix_proposal_top.json", prop)
        log(f"  proposalCount: {prop.get('proposalCount')}")
        for pr in prop.get("proposals", []):
            log(f"  - {pr.get('kind')}: {pr.get('suggestion', '')[:200]}")
except Exception:
    log("Stage E failed:\n" + traceback.format_exc())


# -------------------------------------------------------------------------
# Stage F: descriptor copy log
# -------------------------------------------------------------------------
try:
    log("=== Stage F: descriptor copy log (raw chunks) ===")
    t0 = time.time()
    copy_log = d3d12_copy_descriptors.extract(CAPTURE)
    write_json("f_descriptor_copy_log.json", copy_log)
    log(f"  total chunks: {copy_log.get('total', 0)}")
    log(f"  summary: {copy_log.get('summary', {})}")
    log(f"  duration: {time.time() - t0:.1f}s")
except Exception:
    log("Stage F failed:\n" + traceback.format_exc())


# -------------------------------------------------------------------------
# Stage G: debug pixel pair for top pair (best effort)
# -------------------------------------------------------------------------
try:
    if top_pairs:
        L = int(top_pairs[0]["leftEventId"])
        R = int(top_pairs[0]["rightEventId"])
        # Sample pixel: middle of right-eye half. SN2 SBS is 1280x720 typically.
        x, y = 900, 250
        log(f"=== Stage G: debug pixel pair L={L} R={R} @ ({x},{y}) ===")
        t0 = time.time()
        diff = debug_pixel_pair.diff_traces(CAPTURE, L, R, x, y, max_steps=1024)
        write_json("g_debug_pixel_pair.json", diff)
        if diff.get("firstDivergence"):
            fd = diff["firstDivergence"]
            log(f"  firstDivergence at step {fd.get('step')}: "
                f"{len(fd.get('divergentRegisters') or {})} registers differ")
        else:
            log(f"  no register-level divergence in {diff.get('leftStepCount')} steps "
                f"(error: {diff.get('error')})")
        log(f"  duration: {time.time() - t0:.1f}s")
except Exception:
    log("Stage G failed:\n" + traceback.format_exc())

log("=== DONE ===")
os._exit(0)
