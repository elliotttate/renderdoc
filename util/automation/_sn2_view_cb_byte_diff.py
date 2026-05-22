"""Step 5 + Step 2 from the upstream-of-fog investigation plan.

Step 5: full byte-by-byte diff of the View CB between LEFT and RIGHT
        at a known basepass event. Identify any non-IPD-related deltas.

Step 2: enumerate PS CRCs across the per-eye basepass region, group by
        viewport.x, surface LEFT-only / RIGHT-only / common sets.

Uses the live capture from yesterday. Memory-light: limited to the basepass
event range (11000-22000) to avoid OOM.
"""

import hashlib
import json
import os
import struct
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

# Use the live capture (frame1127) — that's the freshest state the user has
CAPTURE_LIVE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"
# Fall back to baseline if live is unavailable
CAPTURE_BASELINE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
CAPTURE = CAPTURE_LIVE if os.path.isfile(CAPTURE_LIVE) else CAPTURE_BASELINE
log(f"using capture: {CAPTURE}")

# View CB layout (UE5 FViewUniformShaderParameters, ~10076 bytes)
# Field offsets known from prior work / UE5 source:
VIEW_CB_FIELDS = {
    0:    "TranslatedWorldToClip[0][0..3]",
    16:   "TranslatedWorldToClip[1][0..3]",
    32:   "TranslatedWorldToClip[2][0..3]",
    48:   "TranslatedWorldToClip[3][0..3]",
    64:   "RelativeWorldToClip[0]",
    80:   "RelativeWorldToClip[1]",
    96:   "RelativeWorldToClip[2]",
    112:  "RelativeWorldToClip[3]",
    128:  "ClipToWorld[0]",
    192:  "TranslatedWorldToView",
    256:  "ViewToTranslatedWorld",
    320:  "TranslatedWorldToCameraView",
    384:  "CameraViewToTranslatedWorld",
    448:  "ViewToClip",
    512:  "ViewToClipNoAA",
    576:  "ClipToView",
    640:  "ClipToTranslatedWorld",
    704:  "SVPositionToTranslatedWorld",
    768:  "ScreenToWorld",
    832:  "ScreenToTranslatedWorld",
    896:  "MobileMultiviewShadowTransform",
    960:  "ViewTilePosition (LWC)",
    976:  "MatrixTilePosition (LWC)",
    992:  "ViewForward",
    1008: "ViewUp",
    1024: "ViewRight",
    1040: "HMDViewNoRollUp",
    1056: "HMDViewNoRollRight",
    1072: "InvDeviceZToWorldZTransform",
    1088: "ScreenPositionScaleBias",
    1104: "RelativeWorldCameraOrigin",
    1120: "TranslatedWorldCameraOrigin",
    1136: "PreViewTranslation_HighWord",
    1152: "PrevViewToClip",
    # Beyond 1200 — fog, sky, materials params
    2360: "ViewRectMin",
    2368: "ViewRect (x,y,w,h)",
    2384: "BufferSizeAndInvSize",
    2400: "ScreenPositionScaleBias",
    # Fog & sky region typically ~2500-3500
}


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


cap, controller = _lib.open_capture(CAPTURE)
try:
    sdfile = controller.GetStructuredFile()
    try:
        n_chunks = sdfile.chunks.size()
    except Exception:
        n_chunks = len(sdfile.chunks)
    log(f"capture has {n_chunks} chunks")

    # ----- Step 2: PS CRC inventory across basepass region -----
    log("\n=== STEP 2: PS CRC inventory ===")
    main_rt_shape_counts = {}
    textures_by_id = {res_id(t.resourceId): t for t in controller.GetTextures()}

    # First find the main scene RT shape (most-common draw RT)
    for a in _lib.walk_actions(controller):
        if not (int(a.flags) & int(rd.ActionFlags.Drawcall)):
            continue
        outs = list(a.outputs) if a.outputs else []
        if not outs:
            continue
        rid = res_id(outs[0])
        tex = textures_by_id.get(rid)
        if tex is None:
            continue
        w, h = int(tex.width), int(tex.height)
        if w == h and w >= 1024:
            continue
        main_rt_shape_counts[(w, h)] = main_rt_shape_counts.get((w, h), 0) + 1
    if not main_rt_shape_counts:
        log("  no main RT shape detected"); os._exit(1)
    main_w, main_h = max(main_rt_shape_counts.items(), key=lambda kv: kv[1])[0]
    log(f"  main RT shape: {main_w}x{main_h} (most-common-draw-target)")
    half_w = main_w / 2

    # Walk every draw, classify by viewport.x
    log(f"  walking draws, classifying by viewport.x (LEFT < {half_w}, RIGHT >= {half_w})...")
    ps_by_eye = {"left": {}, "right": {}}
    n_examined = 0
    for a in _lib.walk_actions(controller):
        if not (int(a.flags) & int(rd.ActionFlags.Drawcall)):
            continue
        eid = int(a.eventId)
        # Limit to basepass / lit / post-process bands
        if eid < 10000 or eid > 23000:
            continue
        try:
            controller.SetFrameEvent(eid, True)
            d3d12 = controller.GetD3D12PipelineState()
            pipe = controller.GetPipelineState()
        except Exception:
            continue
        n_examined += 1
        # Get viewport x
        vp_x = None
        try:
            if len(d3d12.rasterizer.viewports) > 0:
                vp_x = float(d3d12.rasterizer.viewports[0].x)
        except Exception:
            pass
        if vp_x is None:
            continue
        # Eye class
        eye = "right" if vp_x >= half_w * 0.5 else "left"
        # PS hash + entry
        try:
            refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
        except Exception:
            refl = None
        if not refl or len(refl.rawBytes) == 0:
            continue
        h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))[:16]
        entry = str(refl.entryPoint)
        key = (h, entry)
        ps_by_eye[eye].setdefault(key, []).append(eid)
        if n_examined % 200 == 0:
            log(f"    examined {n_examined} draws so far...")
    log(f"  examined {n_examined} draws total")
    log(f"  PS shaders fired on LEFT: {len(ps_by_eye['left'])}")
    log(f"  PS shaders fired on RIGHT: {len(ps_by_eye['right'])}")

    left_only = set(ps_by_eye["left"].keys()) - set(ps_by_eye["right"].keys())
    right_only = set(ps_by_eye["right"].keys()) - set(ps_by_eye["left"].keys())
    common = set(ps_by_eye["left"].keys()) & set(ps_by_eye["right"].keys())
    log(f"\n  LEFT-only:  {len(left_only)} shaders")
    for h, entry in sorted(left_only):
        eids = ps_by_eye["left"][(h, entry)]
        log(f"    {h} ({entry}) — {len(eids)} draws, first eid {eids[0]}")
    log(f"\n  RIGHT-only: {len(right_only)} shaders")
    for h, entry in sorted(right_only):
        eids = ps_by_eye["right"][(h, entry)]
        log(f"    {h} ({entry}) — {len(eids)} draws, first eid {eids[0]}")
    log(f"\n  COMMON: {len(common)} shaders")

    with open(os.path.join(OUT_DIR, "ps_crc_inventory.json"), "w") as f:
        json.dump({
            "main_rt": [main_w, main_h],
            "left": {f"{h}|{e}": v for (h, e), v in ps_by_eye["left"].items()},
            "right": {f"{h}|{e}": v for (h, e), v in ps_by_eye["right"].items()},
            "left_only": [f"{h}|{e}" for h, e in left_only],
            "right_only": [f"{h}|{e}" for h, e in right_only],
        }, f, indent=2, default=str)
    log(f"\n  wrote ps_crc_inventory.json")

    # ----- Step 5: View CB byte-by-byte diff -----
    log("\n=== STEP 5: View CB byte-by-byte diff ===")
    # Find one matched L/R pair via common shaders, prefer biggest geometry
    # (likely the main basepass)
    if not common:
        log("  no common L/R shaders — can't pair for View CB diff")
    else:
        # Pick a shader with both L+R draws
        best_shader = max(common, key=lambda k: (len(ps_by_eye['left'][k]) +
                                                  len(ps_by_eye['right'][k])))
        h, entry = best_shader
        L_eid = ps_by_eye["left"][best_shader][0]
        R_eid = ps_by_eye["right"][best_shader][0]
        log(f"  picked shader {h} ({entry}), L event {L_eid}, R event {R_eid}")

        # Read PS b1 CBV bytes at each
        def read_view_cb(eid):
            controller.SetFrameEvent(eid, True)
            d3d12 = controller.GetD3D12PipelineState()
            try:
                for p in d3d12.rootSignature.parameters:
                    try:
                        d = p.descriptor
                    except Exception:
                        continue
                    if d is None or getattr(d, "resource", None) is None:
                        continue
                    # The View CB byte-size field is the cbuffer-binding size
                    # not the actual struct. We identify the View CB by name
                    # or by being at b1. Match by visibility+register here.
                    vis = str(p.visibility).split(".")[-1] if hasattr(p, "visibility") else None
                    reg = int(p.reg) if hasattr(p, "reg") else None
                    if vis == "Pixel" and reg == 1:
                        # Read up to 10240 bytes from this CBV
                        data = bytes(controller.GetBufferData(d.resource, int(d.byteOffset), 10240))
                        return data, res_id(d.resource), int(d.byteOffset)
            except Exception as e:
                log(f"  view CB read failed for eid {eid}: {e}")
            return None, None, None

        l_data, l_res, l_off = read_view_cb(L_eid)
        r_data, r_res, r_off = read_view_cb(R_eid)
        if l_data is None or r_data is None:
            log("  failed to read View CB on one or both eyes")
        else:
            log(f"  LEFT View CB:  {l_res} +{l_off}  ({len(l_data)} bytes md5={hashlib.md5(l_data).hexdigest()[:16]})")
            log(f"  RIGHT View CB: {r_res} +{r_off}  ({len(r_data)} bytes md5={hashlib.md5(r_data).hexdigest()[:16]})")
            # Byte-level diff
            n = min(len(l_data), len(r_data))
            diffs = []  # list of (byte_offset, L_float, R_float, L_uint, R_uint)
            for i in range(0, n - 3, 4):  # walk in float-sized strides
                lb = l_data[i:i+4]; rb = r_data[i:i+4]
                if lb != rb:
                    try:
                        lf = struct.unpack("<f", lb)[0]; rf = struct.unpack("<f", rb)[0]
                    except Exception:
                        lf = rf = None
                    try:
                        li = struct.unpack("<I", lb)[0]; ri = struct.unpack("<I", rb)[0]
                    except Exception:
                        li = ri = None
                    diffs.append({"offset": i, "L_float": lf, "R_float": rf,
                                  "L_uint": li, "R_uint": ri})
            log(f"  byte deltas: {len(diffs)} float-positions differ (of {n // 4} examined)")

            # Annotate with known field names
            annotated = []
            for d in diffs:
                off = d["offset"]
                # Find nearest known field
                field_name = None
                field_base = None
                for k in sorted(VIEW_CB_FIELDS.keys(), reverse=True):
                    if off >= k:
                        field_name = VIEW_CB_FIELDS[k]
                        field_base = k
                        break
                annotated.append({**d, "field": field_name, "field_byte": (off - field_base) if field_base else None})
            # Group by field
            from collections import defaultdict
            by_field = defaultdict(list)
            for d in annotated:
                by_field[d["field"] or "<unknown>"].append(d)
            log(f"\n  diffs by field:")
            for field, items in sorted(by_field.items(), key=lambda kv: kv[1][0]["offset"]):
                if len(items) <= 4:
                    detail = ", ".join(f"{x['L_float']:.4g}→{x['R_float']:.4g}" for x in items)
                else:
                    detail = f"{len(items)} components"
                log(f"    @+{items[0]['offset']:5d} {field}: {detail}")

            with open(os.path.join(OUT_DIR, "view_cb_diff.json"), "w") as f:
                json.dump({"L_event": L_eid, "R_event": R_eid,
                            "L_resource": l_res, "L_offset": l_off,
                            "R_resource": r_res, "R_offset": r_off,
                            "diff_count": len(diffs), "diffs": annotated[:200]},
                          f, indent=2, default=str)
            log(f"\n  wrote view_cb_diff.json (truncated to first 200 diffs)")

            # Flag non-IPD-related deltas: matrices are expected to differ
            # (per-eye), but fog params / scene flags should match.
            log(f"\n  === SUSPICIOUS FIELDS (non-matrix, non-IPD) ===")
            for field, items in sorted(by_field.items(), key=lambda kv: kv[1][0]["offset"]):
                if not field: continue
                # Matrix fields and view-vector fields are expected to differ
                if any(k in field for k in ("ToClip", "ToWorld", "ToView", "Translation",
                                              "CameraOrigin", "ViewForward", "ViewUp", "ViewRight",
                                              "HMD", "TilePosition", "Prev")):
                    continue
                # Anything else is suspicious
                log(f"    {field}: {len(items)} bytes differ")
                for it in items[:6]:
                    log(f"      @+{it['offset']:5d}: L={it['L_float']} R={it['R_float']}  (uint L={it['L_uint']} R={it['R_uint']})")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
