"""For each dead-right dispatch pair, capture both L and R UAV bindings,
then run compute_writers on each unique UAV to determine: shared (L wrote, R
reads same resource) vs. per-eye (R has its own UAV that no one writes).
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_LOG = r"E:\tmp_dir\dead_uav.log"
OUT_JSON = r"E:\tmp_dir\sn2_invest_v5\dead_uav_analysis.json"


def log(msg):
    with open(OUT_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(OUT_LOG, "w"): pass

from util.automation import _lib, compute_writers  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
DEAD = json.load(open(r"E:\tmp_dir\sn2_invest_v5\dead_dispatch_shaders.json"))

# Pick interesting dispatches — the fog-volume and shadow ones
INTERESTING_LEFT_EVENTS = [
    19219,  # UWEShadowProjectionDenoiseSpatialCS, 54x61x1
    19120,  # ClearCS, 1024x1x1
    20114,  # MaterialSetupCS, 7x8x16 (3D fog froxel grid!)
    20823,  # VirtualShadowMapProjection, 746x1x1
    21318,  # SimulateMainComputeCS, 22x1x1
    21359,  # UWEFogImportanceDilateCS, 54x31x1
    21377,  # UWEFogDenoiseCS, 108x61x1
    21685,  # ReflectionTileClassificationBuildListsCS, 7x8x1
    21766,  # LumenReflectionDenoiserClearCS, indirect
    21812,  # LumenReflectionDenoiserSpatialCS, indirect
    20997,  # HZBBuildCS, 32x32x1
    18968,  # CullObjectsForShadowCS, 5x1x1
]

# Build {left_event: right_event} map
right_for_left = {}
for r in DEAD:
    right_for_left[int(r["leftEvent"])] = int(r["rightEvent"])


def collect_uavs(controller, eid):
    controller.SetFrameEvent(int(eid), True)
    pipe = controller.GetPipelineState()
    out = []
    for stage in (rd.ShaderStage.Compute, rd.ShaderStage.Pixel):
        try:
            arr = pipe.GetReadWriteResources(stage, False)
        except Exception:
            continue
        for u in arr:
            desc = u.descriptor
            if desc is None or desc.resource is None:
                continue
            out.append({
                "stage": _lib.shader_stage_name(stage),
                "register": int(u.access.index),
                "resource": _lib.resource_id_str(desc.resource),
            })
    return out


cap, controller = _lib.open_capture(CAPTURE)
try:
    rows = []
    uav_resources = set()
    for L in INTERESTING_LEFT_EVENTS:
        R = right_for_left.get(L)
        if R is None:
            log(f"skip L={L}: no right pair")
            continue
        log(f"L={L} R={R}: collecting UAV bindings")
        try:
            l_uavs = collect_uavs(controller, L)
        except Exception as e:
            l_uavs = [{"error": str(e)}]
        try:
            r_uavs = collect_uavs(controller, R)
        except Exception as e:
            r_uavs = [{"error": str(e)}]
        row = {"leftEvent": L, "rightEvent": R, "leftUAVs": l_uavs, "rightUAVs": r_uavs}
        # Compare per-register
        per_reg = {}
        for u in l_uavs:
            if u.get("resource"):
                per_reg.setdefault(u["register"], {})["left"] = u["resource"]
                uav_resources.add(u["resource"])
        for u in r_uavs:
            if u.get("resource"):
                per_reg.setdefault(u["register"], {})["right"] = u["resource"]
                uav_resources.add(u["resource"])
        row["perRegister"] = per_reg
        # Verdict
        diff_regs = [k for k, v in per_reg.items()
                     if v.get("left") and v.get("right") and v["left"] != v["right"]]
        same_regs = [k for k, v in per_reg.items()
                     if v.get("left") and v.get("right") and v["left"] == v["right"]]
        row["sameUAVRegisters"] = same_regs
        row["diffUAVRegisters"] = diff_regs
        rows.append(row)
        log(f"  same regs: {same_regs}  diff regs: {diff_regs}")
finally:
    controller.Shutdown()
    cap.Shutdown()

log(f"\nrunning compute_writers on {len(uav_resources)} unique UAV resources...")
writers = {}
for res in sorted(uav_resources):
    log(f"  {res}...")
    try:
        w = compute_writers.find_writers(CAPTURE, res, eye_classify=False)
        writers[res] = {
            "summary": w.get("summary", {}),
            "writes": w.get("writes", [])[:10],
        }
        s = w.get("summary", {})
        log(f"    {s.get('writeCount', 0)} writers, "
            f"{s.get('deadDispatches', 0)} dead")
    except Exception as e:
        writers[res] = {"error": str(e)}
        log(f"    ERROR {e}")

out = {"pairs": rows, "writersPerUAV": writers}
with open(OUT_JSON, "w") as f:
    json.dump(out, f, indent=2, default=str)
log(f"wrote {OUT_JSON}")
log("DONE")
os._exit(0)
