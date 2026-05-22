"""Verify the colleague's placed-heap aliasing hypothesis against the
existing baseline sn2_nouevr capture.

Specifically check:
  1. Are there TWO LightScatteringCS dispatches (one per eye)?
  2. Are there RIGHT-eye UWE Fog dispatches we missed?
  3. Do the 3D fog volumes 28116/30076 (input to UWE Fog) have
     aliasing barriers from a placed heap?
  4. Which events WRITE to 28116/30076?
  5. Are LightScatteringCS UAV outputs (28367/28368 in my earlier
     finding) aliased into 28116/30076 via barriers?
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_aliasing_verify"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "verify.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


cap, controller = _lib.open_capture(CAPTURE)
try:
    # =================================================================
    # CHECK 1: How many LightScatteringCS dispatches exist?
    # =================================================================
    log("=== CHECK 1: LightScatteringCS dispatch count ===")
    ls_events = []
    for a in _lib.walk_actions(controller):
        flags = int(a.flags)
        if not (flags & int(rd.ActionFlags.Dispatch)):
            continue
        eid = int(a.eventId)
        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()
            refl = pipe.GetShaderReflection(rd.ShaderStage.Compute)
        except Exception:
            continue
        if refl and len(refl.rawBytes) > 0:
            entry = str(refl.entryPoint)
            if entry == "LightScatteringCS":
                # Get view CB offset
                d3d12 = controller.GetD3D12PipelineState()
                view_off = None
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
                # UAVs
                uavs = []
                try:
                    for u in pipe.GetReadWriteResources(rd.ShaderStage.Compute, False):
                        d = u.descriptor
                        if d and d.resource:
                            uavs.append({
                                "u": int(u.access.index),
                                "resource": res_id(d.resource),
                            })
                except Exception:
                    pass
                ls_events.append({
                    "eventId": eid,
                    "viewOffset": view_off,
                    "uavs": uavs,
                    "dispatch": [int(v) for v in a.dispatchDimension],
                })
                log(f"  eid={eid}  viewOff={view_off}  dim={ls_events[-1]['dispatch']}  UAVs={uavs}")
    log(f"\n  total LightScatteringCS dispatches: {len(ls_events)}")
    if len(ls_events) == 1:
        log("  → Only ONE LightScatteringCS in baseline. Colleague claims there should be TWO.")
    elif len(ls_events) == 2:
        log("  → TWO LightScatteringCS dispatches — matches colleague's claim.")
    else:
        log(f"  → {len(ls_events)} dispatches — unusual.")

    # =================================================================
    # CHECK 2: UWE Fog dispatches per-eye?
    # =================================================================
    log("\n=== CHECK 2: UWE Fog dispatch eye classification ===")
    uwe_entries = ("UWEFogReconstructCS", "UWEFogDenoiseCS", "UWEFogResolveCS",
                   "UWEFogImportanceDilateCS", "UWEFogHistoryUpdateConfidenceCS",
                   "UWEFogBuildDepthStandardDeviationCS")
    uwe_events = []
    for a in _lib.walk_actions(controller):
        flags = int(a.flags)
        if not (flags & int(rd.ActionFlags.Dispatch)):
            continue
        eid = int(a.eventId)
        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()
            refl = pipe.GetShaderReflection(rd.ShaderStage.Compute)
        except Exception:
            continue
        if refl and len(refl.rawBytes) > 0:
            entry = str(refl.entryPoint)
            if entry in uwe_entries:
                d3d12 = controller.GetD3D12PipelineState()
                view_off = None
                view_res = None
                for p in d3d12.rootSignature.parameters:
                    try:
                        d = p.descriptor
                    except Exception:
                        continue
                    if d is None or getattr(d, "resource", None) is None:
                        continue
                    if int(d.byteSize) == 10076:
                        view_off = int(d.byteOffset)
                        view_res = res_id(d.resource)
                        break
                uwe_events.append({
                    "eventId": eid, "entry": entry,
                    "viewRes": view_res, "viewOffset": view_off,
                })
    log(f"  total UWE Fog dispatches: {len(uwe_events)}")
    from collections import Counter
    by_entry_eye = Counter()
    for e in uwe_events:
        by_entry_eye[(e["entry"], e["viewOffset"])] += 1
    log(f"  per-entry per-View-offset counts:")
    for (entry, off), cnt in sorted(by_entry_eye.items()):
        log(f"    {entry:50s} viewOff={off}: {cnt}")
    # Identify distinct view offsets
    offsets = sorted(set(e["viewOffset"] for e in uwe_events if e["viewOffset"] is not None))
    log(f"  distinct view offsets in UWE Fog: {offsets}")
    if len(offsets) == 1:
        log("  → SINGLE view offset = LEFT-eye-only UWE Fog (matches my earlier finding)")
    elif len(offsets) == 2:
        log("  → TWO view offsets = both eyes run UWE Fog (matches colleague's claim)")

    # =================================================================
    # CHECK 3: Aliasing barriers on the input fog 3D textures
    # =================================================================
    log("\n=== CHECK 3: Aliasing barriers on 28116, 30076 (UWE Fog SRV inputs) ===")
    log("  (these are the 54x31x64 R11G11B10F 3D textures read by UWEFogReconstruct at t3/t4)")
    for rid_str in ("ResourceId::28116", "ResourceId::30076",
                    "ResourceId::28367", "ResourceId::28368"):
        target = None
        for r in controller.GetResources():
            if res_id(r.resourceId) == rid_str:
                target = r.resourceId; break
        if target is None:
            log(f"  {rid_str}: not found"); continue
        usage = controller.GetUsage(target)
        log(f"\n  {rid_str}: {len(usage)} usages")
        # Filter to writers + barriers
        for u in usage:
            kind = str(u.usage).split(".")[-1]
            log(f"    eid {int(u.eventId)} usage={kind}")

    # =================================================================
    # CHECK 4: Is there an aliasing/PlacedResource relationship between
    # 28367 (LightScatteringCS u0 output) and 28116 (UWE Fog t3 input)?
    # =================================================================
    log("\n=== CHECK 4: D3D12 placed-heap / aliasing hints ===")
    sdfile = controller.GetStructuredFile()
    try:
        nchunks = sdfile.chunks.size()
    except Exception:
        nchunks = len(sdfile.chunks)
    log(f"  scanning {nchunks} chunks for ResourceBarrier with Aliasing flag...")
    aliasing_chunks = 0
    for i in range(nchunks):
        chunk = sdfile.chunks[i]
        cname = str(chunk.name)
        if "ResourceBarrier" in cname:
            # Inspect for aliasing
            for j in range(chunk.NumChildren()):
                c = chunk.GetChild(j)
                cn = str(c.name)
                if "Aliasing" in cn or "pAliasing" in cn or "Type" in cn:
                    # Heuristic: if Type child has int value, check if it's
                    # D3D12_RESOURCE_BARRIER_TYPE_ALIASING (1)
                    try:
                        tv = c.AsInt()
                        if tv == 1:  # ALIASING
                            aliasing_chunks += 1
                    except Exception:
                        pass
    log(f"  total aliasing barriers found: {aliasing_chunks}")
    if aliasing_chunks > 0:
        log("  → ALIASING IS USED in this capture. Colleague's hypothesis is plausible here too.")
    else:
        log("  → No aliasing barriers found. Either the placed-heap pattern doesn't apply")
        log("     to THIS scene, or my detection heuristic missed them.")

    # =================================================================
    # Save state
    # =================================================================
    out = {
        "lightScatteringCS_events": ls_events,
        "uweFog_events": uwe_events,
        "aliasingBarrierCount": aliasing_chunks,
    }
    with open(os.path.join(OUT_DIR, "verify.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    log(f"\n  wrote verify.json")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
