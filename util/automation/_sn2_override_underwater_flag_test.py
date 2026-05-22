"""Override-test: copy LEFT view-CB bytes [864:884] into RIGHT view-CB
at the same offsets, then re-replay the right-eye basepass and compare
the rendered output before/after.

If the +880 hypothesis is correct (LEFT has bUnderwater=1, RIGHT has 0),
this override should flip RIGHT to render with underwater material.

Uses SetBufferOverrideGPU — the renderdoc-fork C++ API that writes
bytes to GPU memory at replay time.
"""

import hashlib
import json
import os
import struct
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "override_underwater_test.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"

# View CB resource + per-eye offsets (from prior verification)
VIEW_CB_RID = "ResourceId::14261"
LEFT_VIEW_OFFSET = 2883584
RIGHT_VIEW_OFFSET = 2887424

# Region to copy (LEFT-populated, RIGHT-zero block)
REGION_START = 864
REGION_END = 884
REGION_LEN = REGION_END - REGION_START  # 20 bytes

# Right-eye basepass event we'll re-replay
R_BASEPASS_EID = 14845


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


cap, controller = _lib.open_capture(CAPTURE)
try:
    # Resolve View CB resource
    view_cb_resource = None
    for r in controller.GetResources():
        if res_id(r.resourceId) == VIEW_CB_RID:
            view_cb_resource = r.resourceId; break
    if view_cb_resource is None:
        log(f"  {VIEW_CB_RID} not found"); os._exit(1)
    log(f"View CB pool resolved: {view_cb_resource}")

    # =================================================================
    # Step 1: read LEFT and RIGHT bytes at +864..+884
    # =================================================================
    log("\n=== Step 1: Read LEFT and RIGHT +864..+884 bytes ===")
    left_bytes = bytes(controller.GetBufferData(
        view_cb_resource, LEFT_VIEW_OFFSET + REGION_START, REGION_LEN))
    right_bytes_orig = bytes(controller.GetBufferData(
        view_cb_resource, RIGHT_VIEW_OFFSET + REGION_START, REGION_LEN))
    log(f"  LEFT  bytes:  {left_bytes.hex()}")
    log(f"  RIGHT bytes:  {right_bytes_orig.hex()}")
    # Interpret as 5 uint32s
    left_u32 = struct.unpack("<5I", left_bytes)
    right_u32 = struct.unpack("<5I", right_bytes_orig)
    for i in range(5):
        off = REGION_START + i * 4
        flag = " ★" if left_u32[i] != right_u32[i] else ""
        log(f"  @+{off}: LEFT={left_u32[i]:10}  RIGHT={right_u32[i]:10}{flag}")
    if left_bytes == right_bytes_orig:
        log("  ⚠ LEFT and RIGHT bytes already match — nothing to override")

    # =================================================================
    # Step 2: capture baseline RT output at right-eye basepass
    # =================================================================
    log(f"\n=== Step 2: Capture baseline RT output at eid {R_BASEPASS_EID} ===")
    controller.SetFrameEvent(R_BASEPASS_EID, True)
    d3d12 = controller.GetD3D12PipelineState()
    pipe = controller.GetPipelineState()
    rt_rid = None
    try:
        if len(d3d12.outputMerger.renderTargets) > 0:
            rt_rid = d3d12.outputMerger.renderTargets[0].resource
    except Exception:
        pass
    log(f"  Right-eye basepass RT0: {res_id(rt_rid)}")
    if rt_rid is None:
        log("  no RT0 — bailing"); os._exit(1)

    # GetMinMax to characterize the output BEFORE override
    try:
        mn, mx = controller.GetMinMax(rt_rid, rd.Subresource(0, 0, 0), rd.CompType.Typeless)
        mn_before = [float(mn.floatValue[c]) for c in range(4)]
        mx_before = [float(mx.floatValue[c]) for c in range(4)]
        log(f"  BEFORE override — RT min: {mn_before}")
        log(f"  BEFORE override — RT max: {mx_before}")
    except Exception as e:
        log(f"  GetMinMax failed: {e}")
        mn_before = mx_before = None

    # =================================================================
    # Step 3: SetBufferOverrideGPU — write LEFT bytes to RIGHT offset
    # =================================================================
    log(f"\n=== Step 3: SetBufferOverrideGPU(RIGHT+{REGION_START}, LEFT bytes) ===")
    has_api = hasattr(controller, "SetBufferOverrideGPU")
    if not has_api:
        log("  ⚠ SetBufferOverrideGPU not available — abort"); os._exit(2)
    target_offset = RIGHT_VIEW_OFFSET + REGION_START
    log(f"  target: {VIEW_CB_RID} at byte offset {target_offset}, writing {REGION_LEN} bytes")
    try:
        ok = controller.SetBufferOverrideGPU(view_cb_resource, target_offset, left_bytes)
        log(f"  SetBufferOverrideGPU returned: {ok}")
    except Exception as e:
        log(f"  override failed: {e}"); os._exit(3)
    if not ok:
        log("  override returned False — abort"); os._exit(4)

    # =================================================================
    # Step 4: re-replay (SetFrameEvent forces re-execution with new state)
    # =================================================================
    log(f"\n=== Step 4: Re-replay event {R_BASEPASS_EID} with override active ===")
    controller.SetFrameEvent(R_BASEPASS_EID, True)
    # Verify the bytes are now actually changed in the buffer (RD's reads
    # may see the override too)
    after_bytes = bytes(controller.GetBufferData(
        view_cb_resource, RIGHT_VIEW_OFFSET + REGION_START, REGION_LEN))
    log(f"  RIGHT bytes AFTER override read: {after_bytes.hex()}")
    if after_bytes == left_bytes:
        log("  ✅ Override visible in buffer read — bytes were written")
    elif after_bytes == right_bytes_orig:
        log("  ⚠ Override NOT visible in buffer read — original RIGHT bytes still there")
    else:
        log("  ⚠ Buffer contains different bytes — partial / unexpected state")

    # Sample RT output AFTER override
    try:
        mn, mx = controller.GetMinMax(rt_rid, rd.Subresource(0, 0, 0), rd.CompType.Typeless)
        mn_after = [float(mn.floatValue[c]) for c in range(4)]
        mx_after = [float(mx.floatValue[c]) for c in range(4)]
        log(f"  AFTER override — RT min: {mn_after}")
        log(f"  AFTER override — RT max: {mx_after}")
    except Exception as e:
        log(f"  GetMinMax failed: {e}")
        mn_after = mx_after = None

    # =================================================================
    # Step 5: compare
    # =================================================================
    log("\n=== Step 5: Compare ===")
    if mn_before and mn_after:
        deltas_min = [a - b for a, b in zip(mn_after, mn_before)]
        deltas_max = [a - b for a, b in zip(mx_after, mx_before)]
        log(f"  min deltas: {deltas_min}")
        log(f"  max deltas: {deltas_max}")
        changed = any(abs(d) > 1e-4 for d in deltas_min + deltas_max)
        if changed:
            log(f"  ✅ RT OUTPUT CHANGED after override — +880 region likely affects rendering")
        else:
            log(f"  ⚠ RT output UNCHANGED — +880 region does NOT affect this draw's output")
            log(f"     this means the per-eye divergence is elsewhere")

    # Save raw RT data for visual comparison
    try:
        # Save the rendered RT as image for visual inspection
        sd_path = os.path.join(OUT_DIR, "right_basepass_AFTER_override.dds")
        save_data = rd.TextureSave()
        save_data.resourceId = rt_rid
        save_data.destType = rd.FileType.DDS
        ok = controller.SaveTexture(save_data, sd_path)
        log(f"  saved RT to {sd_path}: {ok}")
    except Exception as e:
        log(f"  texture save failed: {e}")

    # Cleanup: clear the override
    try:
        controller.ClearBufferOverrideGPU(view_cb_resource)
        log("  cleared override")
    except Exception:
        pass

    result = {
        "left_bytes": left_bytes.hex(),
        "right_bytes_orig": right_bytes_orig.hex(),
        "left_u32": list(left_u32),
        "right_u32": list(right_u32),
        "rt_min_before": mn_before, "rt_max_before": mx_before,
        "rt_min_after": mn_after, "rt_max_after": mx_after,
    }
    with open(os.path.join(OUT_DIR, "override_underwater_test.json"), "w") as f:
        json.dump(result, f, indent=2, default=str)
    log("\nwrote override_underwater_test.json")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
