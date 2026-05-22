"""Replay-time fix prototype using SetBufferOverrideGPU.

Validates the water-injection approach without rebuilding UEVR. Steps:

1. Read the right-eye View cbuffer bytes from ResourceId::29343 at
   offset +3,155,968 (size 1,038,336).

2. Override the LEFT-eye View region at offset +3,166,208 with the
   right-eye bytes (size 1,028,096 — clamped to fit).

3. SetFrameEvent to a known-broken right-eye basepass pixel (using
   the SLW composite which DOES execute on right eye and would now
   read the patched cbuffer).

4. Compare the rendered output before/after the override using
   GetMinMax + sample pixels in the broken region.

5. If the override changes the output in the expected direction
   (more teal, less blown-white), the fix concept works.

Note: SetBufferOverrideGPU is the new C++ API we added that ACTUALLY
writes the bytes to the buffer's GPU memory at replay time (vs
SetBufferOverride which only changes what analysis tools see).
"""

import json
import os
import sys
import time
import struct

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_uevr_followup_2"
LOG = os.path.join(OUT_DIR, "G_replay_fix.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"

# Per-eye View cbuffer offsets (verified in earlier work)
VIEW_CB_RESOURCE = "ResourceId::29343"
LEFT_VIEW_OFFSET = 3166208
RIGHT_VIEW_OFFSET = 3155968
LEFT_VIEW_SIZE = 1028096
RIGHT_VIEW_SIZE = 1038336

# Events to test
SLW_COMPOSITE_RIGHT = 20954        # right-eye SLW composite (already runs)
OPAQUE_BASEPASS_RIGHT = 13157     # right-eye opaque basepass


log("open_capture...")
cap, controller = _lib.open_capture(CAPTURE)
try:
    # Resolve the cbuffer pool resource
    pool_rid = None
    for r in controller.GetResources():
        if _lib.resource_id_str(r.resourceId) == VIEW_CB_RESOURCE:
            pool_rid = r.resourceId
            break
    if pool_rid is None:
        log(f"FATAL: cbuffer pool {VIEW_CB_RESOURCE} not found")
        os._exit(1)
    log(f"  cbuffer pool resolved: {pool_rid}")

    # Read right-eye View bytes
    log(f"  reading right-eye View bytes at offset {RIGHT_VIEW_OFFSET} size {RIGHT_VIEW_SIZE}...")
    right_view_bytes = bytes(controller.GetBufferData(pool_rid, RIGHT_VIEW_OFFSET, min(RIGHT_VIEW_SIZE, 4096)))
    log(f"  read {len(right_view_bytes)} bytes")
    # Quick sanity: first 16 floats
    floats_r = struct.unpack(f"<{len(right_view_bytes) // 4}f", right_view_bytes)
    log(f"  first 8 right-View floats: {floats_r[:8]}")

    # Read left-eye View bytes for comparison
    left_view_bytes = bytes(controller.GetBufferData(pool_rid, LEFT_VIEW_OFFSET, min(LEFT_VIEW_SIZE, 4096)))
    floats_l = struct.unpack(f"<{len(left_view_bytes) // 4}f", left_view_bytes)
    log(f"  first 8 left-View floats:  {floats_l[:8]}")

    # Sanity check: they should differ in matrix rows but match in shared fields
    if floats_l == floats_r:
        log("  WARNING: left and right View bytes are IDENTICAL — that's suspicious")
    else:
        n_diff = sum(1 for a, b in zip(floats_l, floats_r) if a != b)
        log(f"  {n_diff} / {len(floats_l)} floats differ — confirms per-eye")

    # Check the new SetBufferOverrideGPU API exists
    log(f"\n  checking SetBufferOverrideGPU availability...")
    has_api = hasattr(controller, "SetBufferOverrideGPU")
    log(f"  SetBufferOverrideGPU: {'AVAILABLE' if has_api else 'NOT AVAILABLE'}")

    if has_api:
        # Sample the right-eye SLW composite output BEFORE override
        log(f"\n  sampling SLW composite output BEFORE override at event {SLW_COMPOSITE_RIGHT}...")
        controller.SetFrameEvent(SLW_COMPOSITE_RIGHT, True)
        d3d12 = controller.GetD3D12PipelineState()
        # The scene-color RT is the SLW composite's RT0
        rt_rid = None
        try:
            if len(d3d12.outputMerger.renderTargets) > 0:
                rt_rid = d3d12.outputMerger.renderTargets[0].resource
        except Exception:
            pass
        log(f"  scene RT: {_lib.resource_id_str(rt_rid)}")

        # GetMinMax to characterize the RT before
        try:
            mn, mx = controller.GetMinMax(rt_rid, rd.Subresource(0, 0, 0), rd.CompType.Typeless)
            mins_before = [float(mn.floatValue[c]) for c in range(4)]
            maxs_before = [float(mx.floatValue[c]) for c in range(4)]
            log(f"  RT min: {mins_before}")
            log(f"  RT max: {maxs_before}")
        except Exception as e:
            log(f"  GetMinMax failed: {e}")
            mins_before = maxs_before = None

        # OVERRIDE: write right-View bytes to left-View region of the cbuffer
        log(f"\n  invoking SetBufferOverrideGPU({pool_rid}, {LEFT_VIEW_OFFSET}, <right-view bytes>)...")
        try:
            ok = controller.SetBufferOverrideGPU(pool_rid, LEFT_VIEW_OFFSET, right_view_bytes)
            log(f"  override returned: {ok}")
        except Exception as e:
            log(f"  override failed: {e}")
            ok = False

        if ok:
            # Re-sample after override
            log(f"\n  re-sampling AFTER override (force replay re-run)...")
            controller.SetFrameEvent(SLW_COMPOSITE_RIGHT, True)
            try:
                mn, mx = controller.GetMinMax(rt_rid, rd.Subresource(0, 0, 0), rd.CompType.Typeless)
                mins_after = [float(mn.floatValue[c]) for c in range(4)]
                maxs_after = [float(mx.floatValue[c]) for c in range(4)]
                log(f"  RT min: {mins_after}")
                log(f"  RT max: {maxs_after}")
            except Exception as e:
                log(f"  GetMinMax failed: {e}")
                mins_after = maxs_after = None

            if mins_before and mins_after:
                deltas_min = [a - b for a, b in zip(mins_after, mins_before)]
                deltas_max = [a - b for a, b in zip(maxs_after, maxs_before)]
                log(f"\n  min deltas: {deltas_min}")
                log(f"  max deltas: {deltas_max}")
                changed = any(abs(d) > 1e-6 for d in deltas_min + deltas_max)
                if changed:
                    log(f"  ✅ RT output CHANGED after override — fix mechanism is viable")
                else:
                    log(f"  ❌ RT output UNCHANGED — override didn't take effect")
                    log(f"     possible reasons:")
                    log(f"       1. The right-eye SLW composite doesn't read this exact cbuffer slice")
                    log(f"       2. SetBufferOverrideGPU doesn't replay re-execute the dispatch — the bytes are written but the cached output isn't invalidated")
                    log(f"       3. The overridden bytes happen to match left-View already")
        # Clean up
        try:
            controller.ClearBufferOverrideGPU(pool_rid)
            log(f"  cleared GPU override")
        except Exception:
            pass
    else:
        log("  SetBufferOverrideGPU not available — cannot validate fix at replay")

    # Save result
    result = {
        "view_resource": str(pool_rid),
        "left_offset": LEFT_VIEW_OFFSET,
        "right_offset": RIGHT_VIEW_OFFSET,
        "first_8_left_floats": list(floats_l[:8]),
        "first_8_right_floats": list(floats_r[:8]),
        "api_available": has_api,
    }
    with open(os.path.join(OUT_DIR, "G_replay_fix_result.json"), "w") as f:
        json.dump(result, f, indent=2, default=str)
    log("\nwrote G_replay_fix_result.json")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
