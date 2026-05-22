"""Stage C only — other shared-UAV consumers analysis."""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_uevr_followup"
LOG = os.path.join(OUT_DIR, "C_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
SHARED_UAVS = {
    "ResourceId::29816": "HZB",
    "ResourceId::27768": "Lumen reflection tiles",
    "ResourceId::30092": "UWE fog denoise out 1",
    "ResourceId::30095": "UWE fog denoise out 2",
    "ResourceId::30086": "VSM (already patched)",
    "ResourceId::28115": "MaterialSetupCS fog froxel (intermediate)",
}

log("open_capture...")
cap, controller = _lib.open_capture(CAPTURE)
try:
    consumers = {}
    for res, label in SHARED_UAVS.items():
        target = None
        for r in controller.GetResources():
            if _lib.resource_id_str(r.resourceId) == res:
                target = r.resourceId
                break
        if target is None:
            log(f"  {res} ({label}): NOT FOUND in capture")
            consumers[res] = {"label": label, "error": "not_found"}
            continue
        usage = controller.GetUsage(target)
        # Classify usages
        ps_consumers = []
        cs_consumers = []
        rt_writers = []
        cs_writers = []
        copy_writers = []
        for u in usage:
            kind = str(u.usage).split(".")[-1]
            eid = int(u.eventId)
            if "PS_Resource" in kind:
                ps_consumers.append({"eventId": eid, "usage": kind})
            elif "CS_Resource" in kind and "RWResource" not in kind:
                cs_consumers.append({"eventId": eid, "usage": kind})
            elif "RWResource" in kind:
                cs_writers.append({"eventId": eid, "usage": kind})
            elif "ColorTarget" in kind or "ColourTarget" in kind:
                rt_writers.append({"eventId": eid, "usage": kind})
            elif "CopyDst" in kind:
                copy_writers.append({"eventId": eid, "usage": kind})
        consumers[res] = {
            "label": label,
            "totalUsages": len(usage),
            "psConsumers": ps_consumers,
            "csConsumers": cs_consumers,
            "csWriters": cs_writers,
            "rtWriters": rt_writers,
            "copyWriters": copy_writers,
        }
        log(f"\n  {res} ({label}):")
        log(f"    total usages: {len(usage)}")
        log(f"    PS readers: {len(ps_consumers)}")
        log(f"    CS readers: {len(cs_consumers)}")
        log(f"    CS UAV writers: {len(cs_writers)}")
        log(f"    RTV writers: {len(rt_writers)}")
        log(f"    CopyDst writers: {len(copy_writers)}")
        if ps_consumers[:3]:
            log(f"    PS consumer events: {[c['eventId'] for c in ps_consumers[:5]]}")
        if cs_writers[:3]:
            log(f"    CS writer events: {[c['eventId'] for c in cs_writers[:5]]}")
    with open(os.path.join(OUT_DIR, "C_other_shared_consumers.json"), "w") as f:
        json.dump(consumers, f, indent=2, default=str)
    log("\nwrote C_other_shared_consumers.json")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
