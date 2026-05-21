"""Drill v3 — use the actually-correct eye-split based on MainPS event-id midpoint.

The temporal classifier was wrong: it labeled events 11000-18954 as
"unknown" because those events happen before the first scene-RT clear
it tracks. In reality the basepass for BOTH eyes runs in that range,
and each MainPS shader has its draws perfectly split into "early"
(left eye) and "late" (right eye) halves at the temporal midpoint.

This drill:
  1. Walk candidates targeting the main RT
  2. Pick the largest MainPS hash that has both early+late draws
  3. Pick one event from each half — these are the real L/R basepass pair
  4. CB0 diff, bindings compare, DebugPixel
"""

import hashlib
import json
import os
import struct
import sys
import time
import traceback

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_root_v3"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

log("loading candidates from v2...")
candidates = json.load(open(r"E:\tmp_dir\sn2_invest_root_v2\candidates.json"))
main_ps = [c for c in candidates if c.get("psEntry") == "MainPS" and c.get("psHash")]
log(f"  {len(main_ps)} MainPS candidates")

# Group by hash
from collections import defaultdict
by_hash = defaultdict(list)
for c in main_ps:
    by_hash[c["psHash"]].append(c)

# Best hash: most draws, biggest event-id spread
best_hash = max(by_hash.keys(),
                key=lambda h: (max(c["eventId"] for c in by_hash[h]) -
                               min(c["eventId"] for c in by_hash[h])) * len(by_hash[h]))
log(f"  best hash: {best_hash}  ({len(by_hash[best_hash])} draws)")

lst = sorted(by_hash[best_hash], key=lambda c: c["eventId"])
eids = [c["eventId"] for c in lst]
mid = (eids[0] + eids[-1]) // 2
early = [c for c in lst if c["eventId"] <= mid]
late = [c for c in lst if c["eventId"] > mid]
log(f"  eid range: {eids[0]}..{eids[-1]}  mid={mid}  early={len(early)} late={len(late)}")

# Pick representative events with similar size (numIndices)
L_pick = max(early, key=lambda c: c["numIndices"])
R_pick = max(late, key=lambda c: c["numIndices"])
L = int(L_pick["eventId"]); R = int(R_pick["eventId"])
log(f"  L event {L} (idx {L_pick['numIndices']})  R event {R} (idx {R_pick['numIndices']})")


def write_json(name, payload):
    p = os.path.join(OUT_DIR, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    log(f"  -> {name}  ({os.path.getsize(p)} bytes)")


log("import _lib + open capture...")
from util.automation import _lib, debug_pixel_pair  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
cap, controller = _lib.open_capture(CAPTURE)
try:
    # ----- Stage III: CB0 diff -----
    log("=== Stage III: CB0 diff (View cbuffer) ===")

    def dump_cb(eid, slot=0, max_bytes=4096):
        controller.SetFrameEvent(int(eid), True)
        pipe = controller.GetPipelineState()
        try:
            arr = pipe.GetConstantBlocks(rd.ShaderStage.Pixel, False)
        except Exception:
            return None
        if not arr or len(arr) <= slot:
            return None
        used = arr[slot]; desc = used.descriptor
        if desc is None or desc.resource is None:
            return None
        try:
            return bytes(controller.GetBufferData(
                desc.resource, int(desc.byteOffset),
                min(int(desc.byteSize) or max_bytes, max_bytes)))
        except Exception:
            return None

    l_cb = dump_cb(L); r_cb = dump_cb(R)
    log(f"  L cb0 {'OK' if l_cb else 'FAIL'}, size={len(l_cb) if l_cb else 0}B")
    log(f"  R cb0 {'OK' if r_cb else 'FAIL'}, size={len(r_cb) if r_cb else 0}B")
    if l_cb and r_cb:
        log(f"  L md5={hashlib.md5(l_cb).hexdigest()[:16]}  R md5={hashlib.md5(r_cb).hexdigest()[:16]}")
        n_floats = min(len(l_cb), len(r_cb)) // 4
        diffs = []
        for i in range(n_floats):
            lb = l_cb[i*4:i*4+4]; rb = r_cb[i*4:i*4+4]
            if lb != rb:
                try:
                    lf = struct.unpack("<f", lb)[0]; rf = struct.unpack("<f", rb)[0]
                except Exception:
                    lf = rf = None
                try:
                    li = struct.unpack("<I", lb)[0]; ri = struct.unpack("<I", rb)[0]
                except Exception:
                    li = ri = None
                diffs.append({
                    "byteOffset": i*4, "floatIndex": i,
                    "cbReg": i // 4, "comp": i % 4,
                    "L_float": lf, "R_float": rf,
                    "L_uint": li, "R_uint": ri,
                })
        log(f"  diff floats: {len(diffs)} / {n_floats}")
        # Annotate registers with UE5 View cbuffer field names
        named = {
            **{r: "TranslatedWorldToClip[row]" for r in (4, 5, 6, 7)},
            **{r: "TranslatedWorldToView[row]" for r in (8, 9, 10, 11)},
            **{r: "ViewToTranslatedWorld[row]" for r in (12, 13, 14, 15)},
            **{r: "ClipToTranslatedWorld[row]" for r in (44, 45, 46, 47)},
            72: "PreViewTranslation_HighWord",
            73: "ViewForward",
            84: "GameTime",
            85: "WorldTime",
            121: "PreViewTranslation_HighWord",
            122: "PreViewTranslation_LowWord",
            124: "WorldCameraOrigin",
            148: "ViewRect (TopLeftXY + SizeXY)",
            151: "BufferSizeAndInvSize",
            156: "TonemapperParams_ish",
            157: "DiffuseColorBase",
            158: "SpecularColorBase",
            159: "BasisNormalScale",
            160: "MaterialCurve constants",
            161: "PrimitiveBoundsFlag",
            162: "NormalSignFlags",
            165: "RandomSeed_FrameCounter",
            167: "MaterialIndirectScale",
            204: "PrecomputedLightingFlag",
            252: "FogLogScale",
            253: "FogLogOffset",
            258: "VolumetricFogScreenToUV(scale.xy, bias.xy)",
            269: "RoughnessClampMin",
            320: "HDRClampCeiling",
            321: "ViewFlagsBitfield",
        }
        per_reg = defaultdict(list)
        for d in diffs:
            per_reg[d["cbReg"]].append(d)
        # Print labeled diff regs
        log("  labeled register diffs:")
        for reg in sorted(per_reg.keys()):
            label = named.get(reg, "")
            if not label:
                continue
            comps_s = ', '.join([f".{d['comp']}: L={d['L_float']:.4g} R={d['R_float']:.4g}" for d in per_reg[reg][:4]])
            log(f"    reg[{reg}] {label}: {comps_s}")
        write_json("iii_cb0_diff.json", {
            "leftEventId": L, "rightEventId": R,
            "leftMd5": hashlib.md5(l_cb).hexdigest()[:16],
            "rightMd5": hashlib.md5(r_cb).hexdigest()[:16],
            "diffFloatCount": len(diffs),
            "totalFloats": n_floats,
            "labelledRegs": [{"reg": reg, "label": named.get(reg, ""),
                              "components": [{"comp": d["comp"], "L_float": d["L_float"], "R_float": d["R_float"],
                                              "L_uint": d["L_uint"], "R_uint": d["R_uint"]}
                                             for d in per_reg[reg]]}
                             for reg in sorted(per_reg.keys())],
            "topDiffs": diffs[:80],
        })

    # ----- Stage V: bindings compare -----
    log("=== Stage V: bindings compare ===")

    def collect_bindings(eid):
        controller.SetFrameEvent(int(eid), True)
        pipe = controller.GetPipelineState()
        out = []
        for stage in (rd.ShaderStage.Pixel, rd.ShaderStage.Vertex):
            try:
                arr = pipe.GetReadOnlyResources(stage, False)
            except Exception:
                arr = []
            for u in arr:
                desc = u.descriptor
                if desc is None or desc.resource is None:
                    continue
                out.append({
                    "stage": _lib.shader_stage_name(stage),
                    "register": int(u.access.index),
                    "resource": _lib.resource_id_str(desc.resource),
                    "type": _lib.descriptor_type_name(u.access.type),
                })
        return out

    l_binds = collect_bindings(L); r_binds = collect_bindings(R)
    l_dict = {(b["stage"], b["register"]): b for b in l_binds}
    r_dict = {(b["stage"], b["register"]): b for b in r_binds}
    keys = sorted(set(l_dict.keys()) | set(r_dict.keys()), key=lambda k: (k[0], k[1]))
    same_res = []; diff_res = []
    for k in keys:
        lb = l_dict.get(k); rb = r_dict.get(k)
        if lb and rb and lb["resource"] == rb["resource"]:
            same_res.append({"stage": k[0], "reg": k[1], "resource": lb["resource"]})
        else:
            diff_res.append({"stage": k[0], "reg": k[1],
                              "L": lb["resource"] if lb else "NONE",
                              "R": rb["resource"] if rb else "NONE"})
    log(f"  same-resource bindings: {len(same_res)}")
    log(f"  diff-resource bindings: {len(diff_res)}")
    log(f"  Pixel shader bindings — SHARED:")
    for b in same_res:
        if b["stage"] == "Pixel" and b["reg"] in (0, 1, 5, 8, 9, 14):
            log(f"    t{b['reg']} = {b['resource']}  SHARED")
    log(f"  Pixel shader bindings — DIFFERENT:")
    for b in diff_res:
        if b["stage"] == "Pixel" and b["reg"] in (0, 1, 5, 8, 9, 14):
            log(f"    t{b['reg']}: L={b['L']} R={b['R']}  DIFF")
    write_json("v_bindings_compare.json", {
        "leftEventId": L, "rightEventId": R,
        "sameResource": same_res, "diffResource": diff_res,
    })

    # ----- Stage VI: DebugPixel -----
    log("=== Stage VI: DebugPixel on right-eye basepass pixel ===")
    # The capture is 860x484. For right eye basepass (which renders to the
    # same RT region as left eye in sequential per-view stereo), the same
    # pixel coords work for both. Pick center.
    pixel_x = 430; pixel_y = 240
    log(f"  pixel ({pixel_x}, {pixel_y})")
    try:
        d = debug_pixel_pair.diff_traces(CAPTURE, L, R, pixel_x, pixel_y, max_steps=2048)
        write_json("vi_debug_pixel_pair.json", d)
        log(f"  L steps {d.get('leftStepCount')} R steps {d.get('rightStepCount')}  error: {d.get('error')}")
        fd = d.get("firstDivergence")
        if fd:
            log(f"  first divergence at step {fd.get('step')}: "
                f"{len(fd.get('divergentRegisters') or {})} regs")
            for k, v in list((fd.get("divergentRegisters") or {}).items())[:10]:
                log(f"    {k}: L={v.get('left')} R={v.get('right')}")
        else:
            log("  no register-level divergence detected")
    except Exception:
        log("DebugPixel failed:\n" + traceback.format_exc())

finally:
    controller.Shutdown()
    cap.Shutdown()

log("=== DONE ===")
os._exit(0)
