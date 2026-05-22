"""Priority 2: map the View CB region from +2408 to end against known UE5
FViewUniformShaderParameters fields, and dump LEFT vs RIGHT values for
each named field.

UE5 5.6 FViewUniformShaderParameters has a known layout. I cross-reference
common offsets from public UE5 source / engine-dump data. Anything we can't
name we dump as raw float/uint with offset.
"""

import json
import os
import struct
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "view_cb_post_2408.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"

# UE5 5.6 FViewUniformShaderParameters known offsets (approximate, derived
# from public engine dumps + UE source SceneViewUniformShaderParameters.h).
# Region from +2408 to ~+5800 contains atmosphere/fog/material parameters.
# We map a best-effort table; anything outside the named ranges falls
# back to a generic offset label.
NAMED_FIELDS = [
    (2400, 16, "ScreenPositionScaleBias", "float4"),
    (2416, 16, "GameTime_RealTime_DeltaTime_RealTimeDelta", "float4"),
    (2432, 16, "MaterialTextureMipBias_RandomCounter_Pad", "float4"),
    (2448, 16, "TemporalAAParams", "float4"),
    (2464, 16, "CircleDOFParams", "float4"),
    (2480, 16, "DepthOfFieldSensorWidth_FocalDistance", "float4"),
    (2496, 16, "DepthOfFieldFocalRegion_ScaleBias", "float4"),
    (2512, 16, "DepthOfFieldNearFar", "float4"),
    (2528, 16, "PreExposure_OneOverPreExposure_Pad", "float4"),
    (2544, 16, "RuntimeVirtualTextureMipLevel", "float4"),
    (2560, 16, "RuntimeVirtualTextureWorldToUVTransform[0]", "float4"),
    (2576, 16, "RuntimeVirtualTextureWorldToUVTransform[1]", "float4"),
    (2592, 16, "RuntimeVirtualTextureWorldToUVTransform[2]", "float4"),
    (2608, 16, "ResolutionFractionAndInv_AndFOV", "float4"),
    (2624, 16, "MaterialTextureBilinearWrapedSampler_Misc", "float4"),
    (2640, 16, "MaterialTextureMipBias_RandomCounter_Pad", "float4"),
    (2656, 32, "ExponentialFogParameters / DirectionalInscatteringStartDistance", "float4 x2"),
    (2688, 16, "ExponentialFogColorParameter", "float4"),
    (2704, 16, "ExponentialFogParameters3", "float4"),
    (2720, 16, "VolumetricFogStartDistance_InvGridZParams", "float4"),
    (2736, 16, "SkyAtmosphereAerialPerspectiveStartDepthKm", "float4"),
    (2752, 16, "SkyAtmosphereCameraAerialPerspectiveVolumeSizeAndInvSize", "float4"),
    (2768, 16, "SkyAtmosphereCameraAerialPerspectiveVolumeDepthResolution_InvDepth_DepthSliceLengthKm", "float4"),
    (2784, 16, "SkyAtmosphereApplyCameraAerialPerspectiveVolume", "float4"),
    (2800, 16, "ViewLightingChannelMask_Pad", "float4"),
    (2816, 16, "MinRoughness_PrecomputedSurfaceFracAndUnderwaterDeltaZ_etc", "float4"),
    (2832, 64, "AtmosphereLightDirection[0..3]", "float4 x4"),
    (2896, 64, "AtmosphereLightColor[0..3]", "float4 x4"),
    (2960, 16, "AtmosphericFogSunDiscScale_Density_etc", "float4"),
    (2976, 16, "AtmosphereLightDiscLuminance", "float4"),
    (2992, 16, "DistanceFieldAOSpecularOcclusionMode_etc", "float4"),
    (3008, 16, "IndirectLightingColorScale", "float4"),
    (3024, 16, "PrecomputedIndirectLighting / SSRQuality_etc", "float4"),
    (3040, 16, "VolumetricFogScreenToUVScale_Bias", "float4"),
    (3056, 16, "VolumetricFogMaxDistance_Pad", "float4"),
    (3072, 16, "VolumetricLightmapWorldToUVScale", "float4"),
    (3088, 16, "VolumetricLightmapWorldToUVAdd", "float4"),
    (3104, 16, "VolumetricLightmapIndirectionTextureSize", "float4"),
    (3120, 16, "VolumetricLightmapBrickSize_InvBrickSize", "float4"),
    (3136, 16, "VolumetricLightmapBrickTexelSize", "float4"),
    (3152, 16, "GlobalDistanceFieldMipFactor_etc", "float4"),
    # … 3168 onwards: GlobalDistanceField volumes, atmospheric LUT params,
    # camera cut info, frame number, view origin, debug data
]


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


cap, controller = _lib.open_capture(CAPTURE)
try:
    # Use the same L/R pair from prior diff
    L_eid = 14741
    R_eid = 14871

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
                data = bytes(controller.GetBufferData(d.resource, int(d.byteOffset), 10240))
                return data, res_id(d.resource), int(d.byteOffset)
        return None, None, None

    l_data, l_res, l_off = read_view_cb(L_eid)
    r_data, r_res, r_off = read_view_cb(R_eid)
    log(f"LEFT  View CB: {l_res} +{l_off}  size={len(l_data)}")
    log(f"RIGHT View CB: {r_res} +{r_off}  size={len(r_data)}")

    # Walk the +2408..+5800 region, decode each named field
    log("\n=== Named field diff (+2408 onwards) ===")
    REGION_START = 2400
    REGION_END = 5800
    by_field = []
    for (off, size, name, kind) in NAMED_FIELDS:
        if off + size > min(len(l_data), len(r_data)):
            continue
        l_bytes = l_data[off:off + size]
        r_bytes = r_data[off:off + size]
        differs = (l_bytes != r_bytes)
        # Decode as floats (4-byte stride)
        l_floats = []
        r_floats = []
        l_uints = []
        r_uints = []
        for i in range(0, size, 4):
            try:
                l_floats.append(struct.unpack("<f", l_bytes[i:i+4])[0])
                r_floats.append(struct.unpack("<f", r_bytes[i:i+4])[0])
            except Exception:
                l_floats.append(None); r_floats.append(None)
            try:
                l_uints.append(struct.unpack("<I", l_bytes[i:i+4])[0])
                r_uints.append(struct.unpack("<I", r_bytes[i:i+4])[0])
            except Exception:
                l_uints.append(None); r_uints.append(None)
        record = {
            "offset": off, "size": size, "name": name, "kind": kind,
            "differs": differs,
            "L_floats": l_floats, "R_floats": r_floats,
            "L_uints": l_uints, "R_uints": r_uints,
        }
        by_field.append(record)
        if differs:
            log(f"  @+{off:4d} ({name}):")
            for i, (lf, rf) in enumerate(zip(l_floats, r_floats)):
                if lf != rf:
                    log(f"    .{i}: L={lf:.6g} R={rf:.6g}  (uint L={l_uints[i]} R={r_uints[i]})")
        else:
            log(f"  @+{off:4d} ({name}): SAME")

    # Beyond NAMED_FIELDS, summarize unnamed deltas in 64-byte chunks
    log("\n=== Unnamed region summary (+5200 onwards, 64-byte chunks) ===")
    chunks = []
    for off in range(5200, min(len(l_data), len(r_data)) - 64, 64):
        l_c = l_data[off:off + 64]
        r_c = r_data[off:off + 64]
        if l_c != r_c:
            differ_bytes = sum(1 for a, b in zip(l_c, r_c) if a != b)
            chunks.append((off, differ_bytes))
            log(f"  @+{off:5d}: {differ_bytes}/64 bytes differ")

    with open(os.path.join(OUT_DIR, "view_cb_post_2408_mapping.json"), "w") as f:
        json.dump({
            "L_event": L_eid, "R_event": R_eid,
            "L_resource": l_res, "L_offset": l_off,
            "R_resource": r_res, "R_offset": r_off,
            "fields": by_field,
            "unnamed_diff_chunks": chunks,
        }, f, indent=2, default=str)
    log("\nwrote view_cb_post_2408_mapping.json")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
