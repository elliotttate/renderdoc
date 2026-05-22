"""Verify: do right-eye UWE Fog dispatches exist? What's their state?

Inspect specific events known from dead_dispatch_shaders.json:
  - 24726 (UWEFogDenoiseCS paired-right of 21377)
  - 24743 (UWEFogImportanceDilateCS paired-right of 21359)
  - Plus search for ALL events running UWE Fog / LightScatteringCS
    shaders by iterating events 20000-26000 with bisect-style sampling.

Also check: does this capture use placed-heap aliasing?
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_aliasing_verify"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "right_eye_fog.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"

# Known right-eye UWE Fog candidate events
EVENTS_TO_INSPECT = [
    20206,  # known LightScatteringCS (left)
    21348,  # known UWEFogReconstructCS (left)
    21359,  # UWEFogImportanceDilateCS (left)
    21370,  # UWEFogDenoiseCS read1 (left)
    21377,  # UWEFogDenoiseCS write (left)
    21390,  # UWEFogResolveCS (left)
    24726,  # UWEFogDenoiseCS dead-right pair
    24743,  # UWEFogImportanceDilateCS dead-right pair
]


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


cap, controller = _lib.open_capture(CAPTURE)
try:
    log(f"=== Per-event inspection ===")
    rows = []
    for eid in EVENTS_TO_INSPECT:
        try:
            controller.SetFrameEvent(eid, True)
            d3d12 = controller.GetD3D12PipelineState()
            pipe = controller.GetPipelineState()
            entry = None
            for st in (rd.ShaderStage.Compute, rd.ShaderStage.Pixel):
                try:
                    refl = pipe.GetShaderReflection(st)
                except Exception:
                    refl = None
                if refl and len(refl.rawBytes) > 0:
                    entry = str(refl.entryPoint)
                    break
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
            # Get action info
            dim = None
            for a in _lib.walk_actions(controller):
                if int(a.eventId) == eid:
                    try:
                        dim = [int(v) for v in a.dispatchDimension]
                    except Exception:
                        pass
                    break
            # UAV bindings
            uavs = []
            try:
                for u in pipe.GetReadWriteResources(rd.ShaderStage.Compute, False):
                    d = u.descriptor
                    if d and d.resource:
                        uavs.append((int(u.access.index), res_id(d.resource)))
            except Exception:
                pass
            log(f"  eid={eid} entry={entry} viewOff={view_off} dim={dim} UAVs={uavs}")
            rows.append({
                "eventId": eid, "entry": entry, "viewOffset": view_off,
                "dispatchDim": dim, "uavs": uavs,
            })
        except Exception as e:
            log(f"  eid={eid} ERROR {e}")

    # =================================================================
    # Search for ALL LightScatteringCS dispatches (not just one)
    # =================================================================
    log("\n=== Searching all events 19000-26000 for LightScatteringCS ===")
    ls_found = []
    for a in _lib.walk_actions(controller):
        flags = int(a.flags)
        if not (flags & int(rd.ActionFlags.Dispatch)):
            continue
        eid = int(a.eventId)
        if eid < 19000 or eid > 26500:
            continue
        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()
            refl = pipe.GetShaderReflection(rd.ShaderStage.Compute)
        except Exception:
            continue
        if refl and len(refl.rawBytes) > 0:
            entry = str(refl.entryPoint)
            if "LightScattering" in entry:
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
                uavs = []
                try:
                    for u in pipe.GetReadWriteResources(rd.ShaderStage.Compute, False):
                        d = u.descriptor
                        if d and d.resource:
                            uavs.append((int(u.access.index), res_id(d.resource)))
                except Exception:
                    pass
                try:
                    dim = [int(v) for v in a.dispatchDimension]
                except Exception:
                    dim = None
                ls_found.append({
                    "eventId": eid, "entry": entry, "viewOffset": view_off,
                    "dim": dim, "uavs": uavs,
                })
                log(f"  eid {eid}: viewOff={view_off}  dim={dim}  UAVs={uavs}")
    log(f"  total LightScatteringCS events: {len(ls_found)}")

    # =================================================================
    # Check for aliasing barriers (chunk scan)
    # =================================================================
    log("\n=== Aliasing barriers scan ===")
    sdfile = controller.GetStructuredFile()
    try:
        nchunks = sdfile.chunks.size()
    except Exception:
        nchunks = len(sdfile.chunks)
    log(f"  scanning {nchunks} chunks...")
    aliasing_count = 0
    for i in range(nchunks):
        chunk = sdfile.chunks[i]
        cname = str(chunk.name)
        if "ResourceBarrier" not in cname:
            continue
        # Walk children for Aliasing
        try:
            barrier_arr = None
            for j in range(chunk.NumChildren()):
                c = chunk.GetChild(j)
                if str(c.name) in ("pBarriers", "Barriers"):
                    barrier_arr = c
                    break
            if barrier_arr is None:
                continue
            for k in range(barrier_arr.NumChildren()):
                b = barrier_arr.GetChild(k)
                # Each barrier has a Type field
                for m in range(b.NumChildren()):
                    bc = b.GetChild(m)
                    if str(bc.name) == "Type":
                        try:
                            tv = int(bc.AsInt())
                        except Exception:
                            continue
                        if tv == 1:  # D3D12_RESOURCE_BARRIER_TYPE_ALIASING
                            aliasing_count += 1
                        break
        except Exception:
            pass
    log(f"  total aliasing barriers in capture: {aliasing_count}")
    if aliasing_count > 0:
        log("  → ALIASING IS USED. Colleague's hypothesis applies.")
    else:
        log("  → No aliasing barriers detected.")

    with open(os.path.join(OUT_DIR, "right_eye_fog.json"), "w") as f:
        json.dump({"specificEvents": rows, "lightScatteringFound": ls_found,
                   "aliasingCount": aliasing_count}, f, indent=2, default=str)
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
