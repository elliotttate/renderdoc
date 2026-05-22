"""Live-capture vs baseline comparison.

Walk the new capture, find every UWE fog dispatch + ExponentialPixelMain
draw, classify L/R by View cbuffer offset, and surface:
  - Does right eye execute ANY of these now?
  - L/R count per shader entry
  - All distinct View cbuffer offsets seen (to detect new per-eye buckets)
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_live_compare"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE_NEW = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"
CAPTURE_BASELINE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"

TARGET_ENTRIES = {
    "UWEFogReconstructCS",
    "UWEFogDenoiseCS",
    "UWEFogResolveCS",
    "UWEFogImportanceDilateCS",
    "UWEFogBuildDepthStandardDeviationCS",
    "UWEFogHistoryUpdateConfidenceCS",
    "LightScatteringCS",
    "MaterialSetupCS",
    "ExponentialPixelMain",
    "SingleLayerWaterCompositePS",
    "VirtualShadowMapCompositePS",
}


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


def classify_eye_via_view_cb(d3d12):
    """Return (eye, view_offset). Known offsets from baseline:
       left = 3166208, right = 3155968. New captures may have different
       offsets but the L↔R delta should remain consistent."""
    try:
        for p in d3d12.rootSignature.parameters:
            try:
                d = p.descriptor
            except Exception:
                continue
            if d is None or getattr(d, "resource", None) is None:
                continue
            # Look for View CB by size (10076 bytes is the UE5 FViewUB)
            byte_size = int(d.byteSize) if hasattr(d, "byteSize") else 0
            if byte_size == 10076 or "29343" in res_id(d.resource):
                return res_id(d.resource), int(d.byteOffset), byte_size
    except Exception:
        pass
    return None, None, None


def scan_capture(path, label):
    log(f"\n=== {label}: {path} ===")
    cap, controller = _lib.open_capture(path)
    try:
        sdfile = controller.GetStructuredFile()
        results = {}
        # Walk all draws/dispatches
        offsets_seen = {}  # entry -> set of view offsets

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
            # Try both compute and pixel reflection
            entry = None
            shader_hash = None
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
                    candidate = str(refl.entryPoint)
                    if candidate in TARGET_ENTRIES:
                        entry = candidate
                        shader_hash = _lib.shader_bytecode_hash(bytes(refl.rawBytes))
                        stage = st_name
                        break
            if entry is None:
                continue

            n_processed += 1
            d3d12 = controller.GetPipelineState().GetD3D12() if hasattr(controller.GetPipelineState(), 'GetD3D12') else controller.GetD3D12PipelineState()
            # The above expression is over-engineered — just use direct call
            d3d12 = controller.GetD3D12PipelineState()
            view_res, view_off, view_size = classify_eye_via_view_cb(d3d12)
            offsets_seen.setdefault(entry, set()).add((view_res, view_off))

            entry_results = results.setdefault(entry, {"events": []})
            row = {
                "eventId": eid, "stage": stage, "hash": shader_hash[:16] if shader_hash else None,
                "viewRes": view_res, "viewOffset": view_off, "viewSize": view_size,
            }
            try:
                row["dispatchDimension"] = [int(v) for v in a.dispatchDimension]
            except Exception:
                pass
            try:
                row["numIndices"] = int(a.numIndices)
            except Exception:
                pass
            entry_results["events"].append(row)

        # Classify L/R: assume the lowest offset is left or right (we need
        # to compare across entries to identify the L↔R pattern)
        log(f"  scanned {n_processed} target events")
        log(f"  unique entries hit: {len(results)}")
        # Identify L/R from offset pattern: across all entries, the offsets seen
        # should cluster into 2 buckets per shader (or more if multiple cbuffers
        # match the View shape).
        for entry, info in sorted(results.items()):
            events = info["events"]
            # Cluster by view offset
            from collections import Counter
            off_counts = Counter(e["viewOffset"] for e in events)
            log(f"  {entry}: {len(events)} events, view-offsets: {dict(off_counts)}")
            # Identify the two main offsets if any
            top_offsets = [o for o, _ in off_counts.most_common(2)]
            for e in events[:8]:
                eye = "?"
                if e["viewOffset"] is not None and len(top_offsets) >= 2:
                    if e["viewOffset"] == top_offsets[0]:
                        eye = "A"
                    elif e["viewOffset"] == top_offsets[1]:
                        eye = "B"
                elif e["viewOffset"] == top_offsets[0]:
                    eye = "A_only"
                log(f"    eid {e['eventId']} {e['stage']} hash={e['hash']} "
                    f"offset={e['viewOffset']} dim={e.get('dispatchDimension')} "
                    f"idx={e.get('numIndices')}  eye={eye}")

        return results
    finally:
        controller.Shutdown()
        cap.Shutdown()


# Scan the NEW capture first (the live one with the bug visible)
new_results = scan_capture(CAPTURE_NEW, "NEW capture")
out_new = os.path.join(OUT_DIR, "new_capture_scan.json")
with open(out_new, "w") as f:
    json.dump(new_results, f, indent=2, default=str)
log(f"wrote {out_new}")

# Also baseline for comparison
baseline_results = scan_capture(CAPTURE_BASELINE, "BASELINE")
out_baseline = os.path.join(OUT_DIR, "baseline_scan.json")
with open(out_baseline, "w") as f:
    json.dump(baseline_results, f, indent=2, default=str)
log(f"wrote {out_baseline}")

# Diff: per-entry, new count vs baseline count
log("\n=== DIFF SUMMARY (NEW vs BASELINE) ===")
all_entries = sorted(set(new_results.keys()) | set(baseline_results.keys()))
for entry in all_entries:
    new_n = len(new_results.get(entry, {}).get("events", []))
    base_n = len(baseline_results.get(entry, {}).get("events", []))
    delta = new_n - base_n
    arrow = ""
    if delta > 0: arrow = f"+{delta} (more in NEW)"
    elif delta < 0: arrow = f"{delta} (fewer in NEW)"
    log(f"  {entry}: baseline={base_n}, new={new_n} {arrow}")
    # Look for new offsets (potentially the right-eye offset, if right runs now)
    new_offsets = set(e["viewOffset"] for e in new_results.get(entry, {}).get("events", []))
    base_offsets = set(e["viewOffset"] for e in baseline_results.get(entry, {}).get("events", []))
    only_in_new = new_offsets - base_offsets
    if only_in_new:
        log(f"    OFFSETS only in NEW: {only_in_new}  (potential right-eye instance!)")

log("DONE")
os._exit(0)
