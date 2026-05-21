"""Skip the slow walks; just run the divergence-dependent stages.

Reuses Stage A artefacts from a prior run (in V4_DIR) and only runs
Stage B / C / C2 / E / G against the existing pairs file. The slow
Stage D (heap heatmap, 13+ min) is reused too if it exists.
"""

import json
import os
import sys
import time
import traceback

V4_DIR = os.environ.get("SN2_V4_DIR", r"E:\tmp_dir\sn2_invest_v4")
OUT_DIR = os.environ.get("SN2_INVESTIGATION_OUT", r"E:\tmp_dir\sn2_invest_v5")
CAPTURE = os.environ.get(
    "SN2_INVESTIGATION_CAPTURE",
    r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc",
)
TOP_N = int(os.environ.get("SN2_TOP_N", "5"))

os.makedirs(OUT_DIR, exist_ok=True)
PROGRESS = os.path.join(OUT_DIR, "_progress.log")


def log(msg):
    line = f"[sn2-fu {time.strftime('%H:%M:%S')}] {msg}"
    with open(PROGRESS, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
    print(line, flush=True)


with open(PROGRESS, "w", encoding="utf-8") as f:
    pass

log(f"capture:  {CAPTURE}")
log(f"v4 dir:   {V4_DIR}")
log(f"out:      {OUT_DIR}")
log(f"top-N:    {TOP_N}")

sys.path.insert(0, r"E:\Github\renderdoc")

try:
    from util.automation import (
        _lib,
        eye_classifier,
        eye_classifier_temporal,
        pair_eye_events,
        stereo_divergence,
        compute_writers,
        fix_proposal,
        debug_pixel_pair,
    )
    import renderdoc as rd
except Exception:
    log(traceback.format_exc())
    os._exit(3)


# Patch eye_classifier to use the temporal classifier so any downstream
# module call hits the fast path.
def _temporal_classify(capture_path, config=None):
    return eye_classifier_temporal.classify(capture_path)


eye_classifier.classify_capture = _temporal_classify
log("PATCHED eye_classifier.classify_capture -> temporal classifier")


# Also patch pair_eye_events.pair_events to just LOAD the v4 artefact if
# present, instead of re-walking 596 events for 5 minutes.
v4_pairs_path = os.path.join(V4_DIR, "a_event_pairs.json")
cached_pairs = None
if os.path.isfile(v4_pairs_path):
    with open(v4_pairs_path, "r", encoding="utf-8") as f:
        cached_pairs = json.load(f)

_orig_pair = pair_eye_events.pair_events


def _cached_pair(capture, eye_config=None):
    if cached_pairs is not None:
        log("PAIR CACHE HIT — reusing v4 a_event_pairs.json")
        return cached_pairs
    return _orig_pair(capture, eye_config)


pair_eye_events.pair_events = _cached_pair


def write_json(name, payload):
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    log(f"  -> {name}  ({os.path.getsize(path)} bytes)")


# Stage B
top_pairs = []
try:
    log(f"=== Stage B: stereo divergence top-{TOP_N} ===")
    t0 = time.time()
    div = stereo_divergence.rank(CAPTURE, top_n=TOP_N)
    write_json("b_stereo_divergence.json", div)
    top_pairs = div.get("top", [])
    log(f"  pairs scored: {div.get('scoredPairs', 0)}")
    for i, p in enumerate(top_pairs):
        log(f"  #{i + 1}: L={p['leftEventId']} R={p['rightEventId']}  score={p['score']:.2f}")
        bd = p.get("breakdown", {})
        if "rightDispatchDead" in bd:
            log(f"      ⚠️ RIGHT DISPATCH DEAD: {bd.get('dispatchDimensionDelta')}")
        for k in ("rtResourceDelta", "bindingDeltas", "shaderHashDeltas",
                  "dispatchDimensionDelta", "cb0HashDelta"):
            if k in bd:
                log(f"      {k}: {str(bd[k])[:300]}")
    log(f"  duration: {time.time() - t0:.1f}s")
except Exception:
    log("Stage B failed:\n" + traceback.format_exc())


# Stage C
try:
    log("=== Stage C: deep-dive on top pairs ===")
    cap, controller = _lib.open_capture(CAPTURE)
    try:
        for i, p in enumerate(top_pairs[:TOP_N]):
            L = int(p["leftEventId"])
            R = int(p["rightEventId"])
            log(f"--- pair #{i + 1}: L={L} R={R} ---")
            try:
                state_l = _lib.collect_state_at_event(controller, L)
                state_r = _lib.collect_state_at_event(controller, R)
                write_json(f"c_pair{i + 1}_state_left.json", state_l)
                write_json(f"c_pair{i + 1}_state_right.json", state_r)
                # Surface PS t5
                for side, st in (("left", state_l), ("right", state_r)):
                    for b in st.get("bindings", []):
                        if b.get("stage") == "Pixel" and b.get("register") == 5 and \
                                (b.get("type") or "").startswith("Read"):
                            log(f"  {side} PS t5 -> {b.get('resource')} "
                                f"(heap {b.get('heap')} +{b.get('heapByteOffset')})")
                            break
            except Exception:
                log(f"  state failed:\n{traceback.format_exc()}")
    finally:
        controller.Shutdown()
        cap.Shutdown()
except Exception:
    log("Stage C failed:\n" + traceback.format_exc())


# Stage C2
try:
    if top_pairs:
        R = int(top_pairs[0]["rightEventId"])
        cap, controller = _lib.open_capture(CAPTURE)
        t5_resource = None
        try:
            state = _lib.collect_state_at_event(controller, R)
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
            w = compute_writers.find_writers(CAPTURE, t5_resource, eye_classify=True)
            write_json("c2_compute_writers_t5.json", w)
            log(f"  writeCount: {w.get('summary', {}).get('writeCount')}")
            log(f"  deadDispatches: {w.get('summary', {}).get('deadDispatches')}")
            log(f"  perEyeWrites: {w.get('summary', {}).get('perEyeWrites')}")
            log(f"  duration: {time.time() - t0:.1f}s")
except Exception:
    log("Stage C2 failed:\n" + traceback.format_exc())


# Stage E
try:
    if top_pairs:
        L = int(top_pairs[0]["leftEventId"])
        R = int(top_pairs[0]["rightEventId"])
        log(f"=== Stage E: fix proposal for L={L} R={R} ===")
        prop = fix_proposal.propose(CAPTURE, L, R)
        write_json("e_fix_proposal_top.json", prop)
        log(f"  proposalCount: {prop.get('proposalCount')}")
        for pr in prop.get("proposals", []):
            log(f"  - {pr.get('kind')}: {pr.get('suggestion', '')[:300]}")
except Exception:
    log("Stage E failed:\n" + traceback.format_exc())


# Stage G — debug pixel pair
try:
    if top_pairs:
        L = int(top_pairs[0]["leftEventId"])
        R = int(top_pairs[0]["rightEventId"])
        x, y = 900, 250
        log(f"=== Stage G: debug pixel pair L={L} R={R} @ ({x},{y}) ===")
        t0 = time.time()
        d = debug_pixel_pair.diff_traces(CAPTURE, L, R, x, y, max_steps=1024)
        write_json("g_debug_pixel_pair.json", d)
        if d.get("firstDivergence"):
            fd = d["firstDivergence"]
            log(f"  firstDivergence at step {fd.get('step')}: "
                f"{len(fd.get('divergentRegisters') or {})} registers differ")
        else:
            log(f"  no register-level divergence in {d.get('leftStepCount')} steps "
                f"(error: {d.get('error')})")
        log(f"  duration: {time.time() - t0:.1f}s")
except Exception:
    log("Stage G failed:\n" + traceback.format_exc())


log("=== DONE ===")
os._exit(0)
