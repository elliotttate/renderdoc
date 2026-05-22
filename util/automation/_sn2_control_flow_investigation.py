"""Control-flow investigation: the bug is NOT data corruption.
Three rounds of shadow-redirect prove that. So the right-eye basepass
is making a different DECISION per-eye, not sampling different DATA.

Tasks:
  A. Read camera-position fields from View CB at LEFT (eid 14741) and
     RIGHT (eid 14871). Specifically the LWC-tile + relative origin
     fields around offset +1104 / +1120 / +960 / +976.
  B. Enumerate ALL SingleLayerWater* / Water* / Underwater* PS shaders
     in the capture. Are they per-eye-symmetric or LEFT-only?
  C. Pixel-history the cave-opening region on the LEFT and RIGHT halves
     of the backbuffer 140984. Identify the LAST writer's PSO for each.
     Same PSO writing different output = data/CB driven; different PSO
     = material-classification divergence.
"""

import hashlib
import json
import os
import struct
import sys
import time
import zlib

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "control_flow.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def floats_at(data, off, n):
    return list(struct.unpack(f"<{n}f", data[off:off + 4 * n]))


def ints_at(data, off, n):
    return list(struct.unpack(f"<{n}I", data[off:off + 4 * n]))


cap, controller = _lib.open_capture(CAPTURE)
try:
    textures_by_id = {res_id(t.resourceId): t for t in controller.GetTextures()}

    # =================================================================
    # A. Camera position from View CB
    # =================================================================
    log("=== A. Camera position fields (LEFT vs RIGHT) ===")
    def read_view_cb(eid):
        controller.SetFrameEvent(eid, True)
        d3d12 = controller.GetD3D12PipelineState()
        for p in d3d12.rootSignature.parameters:
            try:
                d = p.descriptor
            except Exception:
                continue
            if d is None or getattr(d, "resource", None) is None:
                continue
            vis = str(p.visibility).split(".")[-1]
            reg = int(p.reg)
            if vis == "Pixel" and reg == 1:
                return bytes(controller.GetBufferData(d.resource, int(d.byteOffset), 10240))
        return None

    left_data = read_view_cb(14741)
    right_data = read_view_cb(14871)
    if not left_data or not right_data:
        log("  failed to read View CB");
    else:
        log(f"  LEFT  View CB: {len(left_data)} bytes")
        log(f"  RIGHT View CB: {len(right_data)} bytes")
        # UE5 5.6 FViewUniformShaderParameters known camera-related field offsets:
        camera_fields = [
            (960, "ViewTilePosition (LWC tile, int3)", "int3"),
            (976, "MatrixTilePosition (LWC, int3 for matrix LWC)", "int3"),
            (992, "ViewForward (float3)", "float3"),
            (1008, "ViewUp (float3)", "float3"),
            (1024, "ViewRight (float3)", "float3"),
            (1040, "HMDViewNoRollUp (float3)", "float3"),
            (1056, "HMDViewNoRollRight (float3)", "float3"),
            (1072, "InvDeviceZToWorldZTransform (float4)", "float4"),
            (1088, "ScreenPositionScaleBias (float4)", "float4"),
            (1104, "RelativeWorldCameraOrigin (float3, LWC relative)", "float3"),
            (1120, "TranslatedWorldCameraOrigin (float3, render-space)", "float3"),
            (1136, "PreViewTranslation_HighWord (LWC, float3)", "float3"),
        ]
        for off, name, kind in camera_fields:
            if off + 16 > min(len(left_data), len(right_data)):
                continue
            if kind == "float3":
                lf = floats_at(left_data, off, 4)
                rf = floats_at(right_data, off, 4)
                differ = any(abs(a - b) > 1e-6 for a, b in zip(lf, rf))
            elif kind == "float4":
                lf = floats_at(left_data, off, 4)
                rf = floats_at(right_data, off, 4)
                differ = any(abs(a - b) > 1e-6 for a, b in zip(lf, rf))
            elif kind == "int3":
                lf = ints_at(left_data, off, 4)
                rf = ints_at(right_data, off, 4)
                differ = lf != rf
            else:
                continue
            marker = "★" if differ else " "
            log(f"  {marker} @+{off:4d} {name}")
            log(f"      LEFT:  {lf}")
            log(f"      RIGHT: {rf}")
            if differ and kind in ("float3", "float4"):
                deltas = [a - b for a, b in zip(rf, lf)]
                log(f"      Δ (R-L): {deltas}")

    # =================================================================
    # B. SingleLayerWater / Water / Underwater shaders
    # =================================================================
    log("\n\n=== B. SingleLayerWater / Water / Underwater shader enumeration ===")
    inv = json.load(open(os.path.join(OUT_DIR, "ps_crc_inventory.json")))
    left_map = inv.get("left", {})
    right_map = inv.get("right", {})
    water_shaders = {}
    for key, eids in {**left_map, **right_map}.items():
        h, entry = key.split("|", 1)
        if any(k in entry for k in ("SingleLayerWater", "Underwater", "Water", "SLW")):
            water_shaders[key] = {"left_events": left_map.get(key, []),
                                     "right_events": right_map.get(key, [])}
    log(f"  found {len(water_shaders)} water-related PS shaders:")
    for key, info in water_shaders.items():
        h, entry = key.split("|", 1)
        L = info["left_events"]; R = info["right_events"]
        symm = "✓ symmetric" if L and R else ("LEFT-only" if L else "RIGHT-only")
        log(f"    {h[:16]} ({entry}): {symm}  L={len(L)} R={len(R)}")
        if L: log(f"      first LEFT eid: {L[0]}")
        if R: log(f"      first RIGHT eid: {R[0]}")

    # =================================================================
    # C. Pixel history on cave-opening pixel
    # =================================================================
    log("\n\n=== C. Pixel-history on backbuffer 140984 ===")
    # Target: 140984 is 1264x712. Cave-opening is upper center.
    # LEFT half: x=0..631, RIGHT half: x=632..1262.
    # Pick upper-center of each half.
    LEFT_PX = (316, 200)
    RIGHT_PX = (947, 200)
    log(f"  LEFT pixel:  ({LEFT_PX[0]}, {LEFT_PX[1]})")
    log(f"  RIGHT pixel: ({RIGHT_PX[0]}, {RIGHT_PX[1]})")

    # Find 140984
    bb_id = None
    for r in controller.GetResources():
        if res_id(r.resourceId) == "ResourceId::140984":
            bb_id = r.resourceId; break
    if bb_id is None:
        log("  backbuffer not found");
    else:
        log(f"  backbuffer ResourceId = {res_id(bb_id)}")
        # Get pixel history at the LAST event (the final present-equivalent)
        # We need to pick an event that has all writes already applied.
        # The last writer to 140984 was eid 16505.
        FINAL_EVENT = 16505

        # Run pixel history for each pixel
        for label, (px, py) in (("LEFT", LEFT_PX), ("RIGHT", RIGHT_PX)):
            log(f"\n  -- pixel history for {label} ({px}, {py}) at event {FINAL_EVENT} --")
            try:
                # PixelHistory(target, x, y, view, sub, comptype)
                history = controller.PixelHistory(
                    bb_id, int(px), int(py),
                    rd.Subresource(0, 0, 0),
                    rd.CompType.UNorm,
                )
                # Set frame event first to populate state
                controller.SetFrameEvent(FINAL_EVENT, True)
                log(f"    pixel-history returned {len(history)} events affecting this pixel")
                # Print the last few (most recent writers)
                for ev in history[-12:]:
                    eid = int(ev.eventId)
                    try:
                        controller.SetFrameEvent(eid, True)
                        pipe = controller.GetPipelineState()
                        refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                        entry = str(refl.entryPoint) if refl and len(refl.rawBytes) > 0 else None
                        h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))[:16] if refl and len(refl.rawBytes) > 0 else None
                        d3d12 = controller.GetD3D12PipelineState()
                        pso = res_id(d3d12.pipelineResourceId)
                    except Exception:
                        entry = None; h = None; pso = None
                    # Pre/post pixel values
                    try:
                        pre = [ev.preMod.col.floatValue[i] for i in range(4)]
                        post = [ev.postMod.col.floatValue[i] for i in range(4)]
                    except Exception:
                        pre = post = None
                    log(f"      eid {eid:5} PS={entry or '?':25} hash={h}  PSO={pso}")
                    log(f"        pre={pre}")
                    log(f"        post={post}")
            except Exception as e:
                log(f"    pixel-history failed: {e}")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
