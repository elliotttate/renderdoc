"""Quick exploration of the live capture — what shader entries exist?"""

import os, sys, time
sys.path.insert(0, r"E:\Github\renderdoc")
from util.automation import _lib  # type: ignore
import renderdoc as rd

LOG = r"E:\tmp_dir\sn2_live_compare\live_explore.log"


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"
log("open_capture...")
cap, controller = _lib.open_capture(CAPTURE)
try:
    log("opened")
    from collections import Counter
    entry_counts = Counter()
    total = 0
    sampled = 0
    for a in _lib.walk_actions(controller):
        total += 1
        flags = int(a.flags)
        if not (flags & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
            continue
        eid = int(a.eventId)
        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()
        except Exception as e:
            log(f"SetFrameEvent({eid}) failed: {e}")
            break
        for st in (rd.ShaderStage.Compute, rd.ShaderStage.Pixel):
            try:
                refl = pipe.GetShaderReflection(st)
            except Exception:
                refl = None
            if refl and len(refl.rawBytes) > 0:
                entry_counts[str(refl.entryPoint)] += 1
                break
        sampled += 1
        if sampled % 200 == 0:
            log(f"  sampled {sampled} actions...")
    log(f"\ndone — total actions iterated: {total}, sampled: {sampled}")
    log(f"unique entries: {len(entry_counts)}")
    # Most common entries
    log("\nTop 30 entries by count:")
    for entry, cnt in entry_counts.most_common(30):
        log(f"  {cnt:5}  {entry}")
    # UWE/fog-specific
    log("\nUWE / fog / volumetric entries:")
    for entry, cnt in entry_counts.most_common():
        if any(k in entry for k in ("UWE", "Fog", "Volumetric", "LightScattering", "Material", "Exponential", "SingleLayerWater")):
            log(f"  {cnt:5}  {entry}")
finally:
    controller.Shutdown()
    cap.Shutdown()
log("DONE")
os._exit(0)
