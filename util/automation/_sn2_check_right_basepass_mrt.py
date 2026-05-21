"""For every basepass draw (PS entry name contains 'MainPS'), enumerate
every render-target binding (OMSetRenderTargets state). Answers the question:
"Does any right-eye basepass draw bind ResourceId::30085 (the SLW auxiliary
target) at MRT slot 6?"

Three possible outcomes:
  1. NO right-eye draws bind slot 6 → MRT-setup level bug (engine doesn't
     bind the SLW aux target on right eye)
  2. Right-eye draws DO bind slot 6 to 30085 but to a different resource ID
     → per-eye allocation (engine is making a separate right-eye target
     that just isn't being read by the composite)
  3. Right-eye draws bind slot 6 to 30085 (same as left) → my eye classifier
     was wrong; right-eye DOES write to 30085, classifier miscounted

Also separately checks Water-material draws specifically: any draw whose
PS hash matches one of the 3 water MainPS variants (1ea961e4, 30782963,
9ecbcc2e) — does it appear on both eyes?
"""

import json
import os
import sys
import time
import traceback

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_target"
LOG = os.path.join(OUT_DIR, "right_basepass_mrt.log")
JSON_OUT = os.path.join(OUT_DIR, "right_basepass_mrt.json")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

log("import _lib...")
from util.automation import _lib, eye_classifier_temporal  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
TARGET_AUX_RT = "ResourceId::30085"   # SLW GBuffer auxiliary
WATER_MATERIAL_PS_HASH_PREFIXES = ("1ea961e4", "30782963", "9ecbcc2e")

cap, controller = _lib.open_capture(CAPTURE)
try:
    # Use the existing v2 candidate list to avoid re-walking
    log("loading v2 candidates...")
    candidates = json.load(open(r"E:\tmp_dir\sn2_invest_root_v2\candidates.json"))
    main_ps = [c for c in candidates if c.get("psEntry") == "MainPS" and c.get("psHash")]
    log(f"  {len(main_ps)} MainPS candidates")

    # Split into LEFT (early half by eid) and RIGHT (late half by eid) using
    # the temporal-midpoint trick that worked in v3.
    eids_all = sorted(c["eventId"] for c in main_ps)
    mid = (eids_all[0] + eids_all[-1]) // 2
    log(f"  eid range {eids_all[0]}..{eids_all[-1]}  mid={mid}")
    left_draws = [c for c in main_ps if c["eventId"] <= mid]
    right_draws = [c for c in main_ps if c["eventId"] > mid]
    log(f"  early(LEFT): {len(left_draws)}  late(RIGHT): {len(right_draws)}")

    # For each draw, enumerate every bound render target
    def collect_rtvs(eid):
        controller.SetFrameEvent(int(eid), True)
        d3d12 = controller.GetD3D12PipelineState()
        rtvs = []
        try:
            arr = d3d12.outputMerger.renderTargets
        except Exception:
            return rtvs
        for i, rt in enumerate(arr):
            rid = _lib.resource_id_str(rt.resource)
            if rid is None:
                continue
            rtvs.append({"slot": i, "resource": rid,
                          "firstMip": int(rt.firstMip),
                          "firstSlice": int(rt.firstSlice),
                          "numSlices": int(rt.numSlices)})
        return rtvs

    # Walk all RIGHT-eye basepass draws, collect MRT bindings
    log("\nwalking RIGHT-eye basepass draws...")
    right_mrt_bindings = []
    right_binds_30085 = []
    right_binds_water_hash = []
    for c in right_draws:
        eid = c["eventId"]
        try:
            rtvs = collect_rtvs(eid)
        except Exception as exc:
            rtvs = [{"error": str(exc)}]
        entry = {"eventId": eid, "psHash": c["psHash"][:16], "rtvs": rtvs}
        right_mrt_bindings.append(entry)
        # Track ones that bind 30085 anywhere
        for r in rtvs:
            if r.get("resource") == TARGET_AUX_RT:
                right_binds_30085.append({"eventId": eid, "slot": r["slot"], "psHash": c["psHash"][:16]})
        # Track ones using a water-material PS hash
        if any(c["psHash"].lower().startswith(p) for p in WATER_MATERIAL_PS_HASH_PREFIXES):
            right_binds_water_hash.append({"eventId": eid, "psHash": c["psHash"][:16],
                                           "rtvs": [r.get("resource") for r in rtvs]})

    # Same for LEFT for comparison
    log("walking LEFT-eye basepass draws...")
    left_mrt_bindings = []
    left_binds_30085 = []
    left_binds_water_hash = []
    for c in left_draws:
        eid = c["eventId"]
        try:
            rtvs = collect_rtvs(eid)
        except Exception:
            rtvs = []
        entry = {"eventId": eid, "psHash": c["psHash"][:16], "rtvs": rtvs}
        left_mrt_bindings.append(entry)
        for r in rtvs:
            if r.get("resource") == TARGET_AUX_RT:
                left_binds_30085.append({"eventId": eid, "slot": r["slot"], "psHash": c["psHash"][:16]})
        if any(c["psHash"].lower().startswith(p) for p in WATER_MATERIAL_PS_HASH_PREFIXES):
            left_binds_water_hash.append({"eventId": eid, "psHash": c["psHash"][:16],
                                          "rtvs": [r.get("resource") for r in rtvs]})

    # Histogram of MRT slot-count distribution per eye
    def slot_count_hist(rows):
        from collections import Counter
        h = Counter(len(r["rtvs"]) for r in rows if isinstance(r["rtvs"], list))
        return dict(h)

    out = {
        "leftBasepassDrawCount": len(left_draws),
        "rightBasepassDrawCount": len(right_draws),
        "leftSlotCountHist": slot_count_hist(left_mrt_bindings),
        "rightSlotCountHist": slot_count_hist(right_mrt_bindings),
        "leftBindsAuxRT_30085": left_binds_30085,
        "rightBindsAuxRT_30085": right_binds_30085,
        "leftWaterMaterialDraws": left_binds_water_hash,
        "rightWaterMaterialDraws": right_binds_water_hash,
        "leftBindings": left_mrt_bindings,
        "rightBindings": right_mrt_bindings,
    }
    with open(JSON_OUT, "w") as f:
        json.dump(out, f, indent=2, default=str)
    log(f"\nwrote {JSON_OUT}")

    log("\n=== SUMMARY ===")
    log(f"LEFT basepass draws:  {len(left_draws)}  MRT slot-count hist: {out['leftSlotCountHist']}")
    log(f"RIGHT basepass draws: {len(right_draws)}  MRT slot-count hist: {out['rightSlotCountHist']}")
    log(f"")
    log(f"Draws binding {TARGET_AUX_RT} (SLW aux RT):")
    log(f"  LEFT:  {len(left_binds_30085)} draws")
    for b in left_binds_30085[:5]:
        log(f"    eid {b['eventId']} hash {b['psHash']} slot {b['slot']}")
    log(f"  RIGHT: {len(right_binds_30085)} draws")
    for b in right_binds_30085[:5]:
        log(f"    eid {b['eventId']} hash {b['psHash']} slot {b['slot']}")
    log(f"")
    log(f"Water-material PS-hash draws:")
    log(f"  LEFT:  {len(left_binds_water_hash)} draws")
    log(f"  RIGHT: {len(right_binds_water_hash)} draws")
    for b in right_binds_water_hash[:5]:
        log(f"    eid {b['eventId']} hash {b['psHash']}")

    log("")
    log("=== VERDICT ===")
    if len(right_binds_30085) == 0 and len(left_binds_30085) > 0:
        log("  MRT-LEVEL BUG: right-eye basepass NEVER binds 30085 at any slot.")
        log("  Fix needs to land at MRT setup time on right eye.")
    elif len(right_binds_water_hash) == 0 and len(left_binds_water_hash) > 0:
        log("  MESH-BATCH LEVEL BUG: right-eye basepass binds slot 6 but the")
        log("  mesh-batch list excludes water materials. Water draws never reach the right eye.")
    elif len(right_binds_30085) > 0:
        log("  CLASSIFIER WAS WRONG: right-eye basepass DOES bind 30085 — the dead-right")
        log("  finding was a temporal-classifier miscount near a segment boundary.")
    else:
        log("  Inconclusive — no draws found on either side; check temporal split.")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
