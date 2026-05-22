"""Scan ONLY the new live capture — UWE fog entries, with eye classification."""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_live_compare"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "live_only.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

log("import _lib...")
from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"

TARGET_ENTRIES = {
    "UWEFogReconstructCS", "UWEFogDenoiseCS", "UWEFogResolveCS",
    "UWEFogImportanceDilateCS", "UWEFogBuildDepthStandardDeviationCS",
    "UWEFogHistoryUpdateConfidenceCS", "LightScatteringCS",
    "MaterialSetupCS", "ExponentialPixelMain",
    "SingleLayerWaterCompositePS", "VirtualShadowMapCompositePS",
}


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


log("open_capture...")
cap, controller = _lib.open_capture(CAPTURE)
log("opened.")
try:
    n_actions = 0
    for a in _lib.walk_actions(controller):
        n_actions += 1
    log(f"total actions: {n_actions}")

    results = {}
    n_processed = 0
    for a in _lib.walk_actions(controller):
        flags = int(a.flags)
        if not (flags & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
            continue
        eid = int(a.eventId)
        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()
        except Exception:
            continue
        entry = None
        stage = None
        for st_enum, st_name in (
            (rd.ShaderStage.Compute, "Compute"),
            (rd.ShaderStage.Pixel, "Pixel"),
        ):
            try:
                refl = pipe.GetShaderReflection(st_enum)
            except Exception:
                refl = None
            if refl and len(refl.rawBytes) > 0:
                cand = str(refl.entryPoint)
                if cand in TARGET_ENTRIES:
                    entry = cand
                    stage = st_name
                    break
        if entry is None:
            continue
        n_processed += 1
        d3d12 = controller.GetD3D12PipelineState()
        view_off = None
        try:
            for p in d3d12.rootSignature.parameters:
                try:
                    d = p.descriptor
                except Exception:
                    continue
                if d is None or getattr(d, "resource", None) is None:
                    continue
                if int(d.byteSize) == 10076:
                    view_off = int(d.byteOffset)
                    break
        except Exception:
            pass
        try:
            dispatch_dim = [int(v) for v in a.dispatchDimension]
        except Exception:
            dispatch_dim = None
        results.setdefault(entry, []).append({
            "eventId": eid, "stage": stage,
            "viewOffset": view_off,
            "dispatchDim": dispatch_dim,
        })

    log(f"\nfound {n_processed} target events")
    log("\n=== PER-SHADER SUMMARY ===")
    from collections import Counter
    for entry in sorted(results.keys()):
        events = results[entry]
        off_counts = Counter(e["viewOffset"] for e in events)
        log(f"\n  {entry}: {len(events)} events")
        log(f"    view offsets: {dict(off_counts)}")
        for e in events[:10]:
            log(f"    eid {e['eventId']} {e['stage']} viewOff={e['viewOffset']} dim={e['dispatchDim']}")

    out_json = os.path.join(OUT_DIR, "live_scan.json")
    with open(out_json, "w") as f:
        json.dump(results, f, indent=2, default=str)
    log(f"\nwrote {out_json}")

    # Quick eye-classification heuristic: cluster offsets into 2 groups
    # (left + right) and report per-shader L/R counts
    log("\n=== LIVE CAPTURE EYE CLASSIFICATION ===")
    all_offsets = set()
    for events in results.values():
        for e in events:
            if e["viewOffset"] is not None:
                all_offsets.add(e["viewOffset"])
    log(f"distinct view offsets seen: {sorted(all_offsets)}")

    # For UE5 stereo, expect exactly 2 dominant offsets
    if len(all_offsets) == 2:
        a, b = sorted(all_offsets)
        log(f"two distinct offsets: A={a}, B={b}  (Δ={b-a})  → likely L+R")
        for entry, events in sorted(results.items()):
            a_count = sum(1 for e in events if e["viewOffset"] == a)
            b_count = sum(1 for e in events if e["viewOffset"] == b)
            log(f"  {entry}: A={a_count}, B={b_count}")
    elif len(all_offsets) == 1:
        log(f"only 1 view offset — LEFT-ONLY pattern (same as baseline)")
        log(f"VERDICT: Hypothesis B confirmed for THIS live scene — right eye doesn't run these")
    else:
        log(f"{len(all_offsets)} distinct offsets — unusual pattern, manual inspection needed")
finally:
    controller.Shutdown()
    cap.Shutdown()
log("DONE")
os._exit(0)
