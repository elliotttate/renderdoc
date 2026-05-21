"""Identify the two shared-single-writer resources that drive the SN2
water bug: 29993 (writer event 7679) and 30085 (writer event 19730).

For each:
  - Resource type / format / dimensions
  - The shader running at the writer event + its entry-point name
  - The shader's bound UAV slot + register
  - Any nearby compute calls that might be related (e.g., setup passes
    that compute the dispatch dims)
"""

import json
import os
import sys
import time
import traceback

sys.path.insert(0, r"E:\Github\renderdoc")

OUT = r"E:\tmp_dir\sn2_invest_target\water_textures_identity.json"
LOG = r"E:\tmp_dir\sn2_invest_target\identify_log.log"


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

log("import _lib...")
from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
TARGETS = [
    ("ResourceId::29993", 7679),
    ("ResourceId::30085", 19730),
]

cap, controller = _lib.open_capture(CAPTURE)
try:
    # Resolve resource details
    resource_details = {}
    for r in controller.GetResources():
        rid_s = _lib.resource_id_str(r.resourceId)
        if rid_s in (t[0] for t in TARGETS):
            resource_details[rid_s] = {
                "name": str(r.name),
                "type": str(r.type).split(".")[-1],
                "flags": str(r.flags),
            }
    # For textures, get width/height/format
    for t in controller.GetTextures():
        rid_s = _lib.resource_id_str(t.resourceId)
        if rid_s in resource_details:
            resource_details[rid_s].update({
                "width": int(t.width), "height": int(t.height),
                "depth": int(t.depth),
                "mips": int(t.mips), "arraySize": int(t.arraysize),
                "format": str(t.format.Name()),
            })
    for b in controller.GetBuffers():
        rid_s = _lib.resource_id_str(b.resourceId)
        if rid_s in resource_details:
            resource_details[rid_s].update({
                "length": int(b.length),
            })

    log("Resource details:")
    for rid, det in resource_details.items():
        log(f"  {rid}: {det}")

    # For each writer event, get the shader and bindings
    writer_info = {}
    for rid_s, writer_eid in TARGETS:
        log(f"\n=== {rid_s} writer at event {writer_eid} ===")
        try:
            controller.SetFrameEvent(writer_eid, True)
            pipe = controller.GetPipelineState()
            entry = {"eventId": writer_eid, "resource": rid_s}
            # Try compute first
            try:
                refl_cs = pipe.GetShaderReflection(rd.ShaderStage.Compute)
            except Exception:
                refl_cs = None
            if refl_cs and len(refl_cs.rawBytes) > 0:
                entry["shaderStage"] = "Compute"
                entry["entryPoint"] = str(refl_cs.entryPoint)
                entry["bytecodeHash"] = _lib.shader_bytecode_hash(bytes(refl_cs.rawBytes))
                entry["bytecodeSize"] = len(refl_cs.rawBytes)
            else:
                # Try pixel
                try:
                    refl_ps = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                except Exception:
                    refl_ps = None
                if refl_ps and len(refl_ps.rawBytes) > 0:
                    entry["shaderStage"] = "Pixel"
                    entry["entryPoint"] = str(refl_ps.entryPoint)
                    entry["bytecodeHash"] = _lib.shader_bytecode_hash(bytes(refl_ps.rawBytes))
                    entry["bytecodeSize"] = len(refl_ps.rawBytes)
            # Get the action's dispatch dims if any
            for a in _lib.walk_actions(controller):
                if int(a.eventId) == writer_eid:
                    try:
                        entry["dispatchDimension"] = [int(v) for v in a.dispatchDimension]
                    except Exception:
                        pass
                    try:
                        entry["numIndices"] = int(a.numIndices)
                    except Exception:
                        pass
                    sdfile = controller.GetStructuredFile()
                    entry["actionName"] = str(a.GetName(sdfile))
                    break
            # All UAVs bound on compute / RTVs for raster
            uavs = []
            try:
                arr = pipe.GetReadWriteResources(rd.ShaderStage.Compute, False)
                for u in arr:
                    desc = u.descriptor
                    if desc is None or desc.resource is None:
                        continue
                    uavs.append({
                        "register": int(u.access.index),
                        "resource": _lib.resource_id_str(desc.resource),
                    })
            except Exception:
                pass
            entry["uavs"] = uavs
            # Look at SRV bindings too
            srvs = []
            try:
                arr = pipe.GetReadOnlyResources(rd.ShaderStage.Compute, False)
                for u in arr:
                    desc = u.descriptor
                    if desc is None or desc.resource is None:
                        continue
                    srvs.append({
                        "register": int(u.access.index),
                        "resource": _lib.resource_id_str(desc.resource),
                    })
            except Exception:
                pass
            entry["srvs_first10"] = srvs[:10]

            log(f"  shaderStage: {entry.get('shaderStage')}")
            log(f"  entryPoint: {entry.get('entryPoint')}")
            log(f"  bytecodeHash: {entry.get('bytecodeHash')}")
            log(f"  actionName: {entry.get('actionName')}")
            log(f"  dispatchDim: {entry.get('dispatchDimension')}")
            log(f"  UAVs: {uavs}")
            log(f"  SRVs (first 10): {srvs[:10]}")
            writer_info[rid_s] = entry
        except Exception:
            log(f"  ERROR: {traceback.format_exc()}")
            writer_info[rid_s] = {"error": traceback.format_exc()}

    out = {
        "resources": resource_details,
        "writers": writer_info,
    }
    with open(OUT, "w") as f:
        json.dump(out, f, indent=2, default=str)
    log(f"\nwrote {OUT}")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
