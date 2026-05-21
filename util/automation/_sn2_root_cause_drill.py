"""Root-cause drill — runs all Tier-1 + Tier-2 questions in one qrenderdoc
session against the SN2 capture.

Stages:
  I.    Detect UE5 stereo-rendering mode from chunk stream
  II.   Find a matched left/right basepass MainPS draw pair
  III.  Dump + diff CB0 (View cbuffer) at the pair
  IV.   Resource lineage for shared UAVs (fog froxel, VSM, HZB)
  V.    Confirm right-eye basepass samples the shared UAVs
  VI.   DebugPixel on the wrong right-eye basepass pixel
"""

import hashlib
import json
import os
import struct
import sys
import time
import traceback

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_root"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

log("import modules...")
from util.automation import _lib, resource_lineage, debug_pixel_pair  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
SHARED_UAVS = ["ResourceId::28115", "ResourceId::30086", "ResourceId::29816",
               "ResourceId::30092", "ResourceId::30095", "ResourceId::27768"]


def write_json(name, payload):
    p = os.path.join(OUT_DIR, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    log(f"  -> {name} ({os.path.getsize(p)} bytes)")


log("open_capture...")
cap, controller = _lib.open_capture(CAPTURE)
try:
    sdfile = controller.GetStructuredFile()
    try:
        n_chunks = sdfile.chunks.size()
    except Exception:
        n_chunks = len(sdfile.chunks)
    log(f"sdfile has {n_chunks} chunks")

    # =============================================================
    # STAGE I — Detect stereo-rendering mode
    # =============================================================
    log("=== Stage I: stereo-rendering mode detection ===")
    isr_signals = {
        "DrawIndexedInstanced_count": 0,
        "DrawIndexedInstanced_inst2_count": 0,
        "RSSetViewports_count": 0,
        "RSSetViewports_multi_count": 0,
        "viewport_count_distribution": {},
    }
    for i in range(n_chunks):
        chunk = sdfile.chunks[i]
        cname = str(chunk.name)
        if "DrawIndexedInstanced" in cname:
            isr_signals["DrawIndexedInstanced_count"] += 1
            for j in range(chunk.NumChildren()):
                c = chunk.GetChild(j)
                if str(c.name) == "InstanceCount":
                    try:
                        ic = int(c.AsInt())
                        if ic == 2:
                            isr_signals["DrawIndexedInstanced_inst2_count"] += 1
                    except Exception:
                        pass
                    break
        if "RSSetViewports" in cname:
            isr_signals["RSSetViewports_count"] += 1
            for j in range(chunk.NumChildren()):
                c = chunk.GetChild(j)
                if str(c.name) == "NumViewports":
                    try:
                        nv = int(c.AsInt())
                        isr_signals["viewport_count_distribution"][nv] = \
                            isr_signals["viewport_count_distribution"].get(nv, 0) + 1
                        if nv > 1:
                            isr_signals["RSSetViewports_multi_count"] += 1
                    except Exception:
                        pass
                    break
    write_json("i_stereo_signals.json", isr_signals)
    log(f"  Draw inst=2: {isr_signals['DrawIndexedInstanced_inst2_count']}/{isr_signals['DrawIndexedInstanced_count']}")
    log(f"  RSSetViewports multi: {isr_signals['RSSetViewports_multi_count']}/{isr_signals['RSSetViewports_count']}")
    log(f"  viewport-count distribution: {isr_signals['viewport_count_distribution']}")
    if isr_signals["DrawIndexedInstanced_inst2_count"] / max(1, isr_signals["DrawIndexedInstanced_count"]) > 0.5:
        log("  VERDICT: Instanced Stereo Rendering (ISR) likely active")
    elif isr_signals["RSSetViewports_multi_count"] > 0:
        log("  VERDICT: Multi-viewport stereo likely active")
    else:
        log("  VERDICT: Classic per-view (mono-slave) stereo — two sequential render passes")

    # =============================================================
    # STAGE II — Find a left/right basepass MainPS pair
    # =============================================================
    log("=== Stage II: find a basepass MainPS draw pair ===")
    # Use the temporal classifier to identify L/R, then find draws targeting
    # the most-frequently-bound scene RT shape.
    from util.automation import eye_classifier_temporal  # type: ignore
    eye = eye_classifier_temporal.classify(CAPTURE)
    by_eid = {int(e["eventId"]): e for e in eye.get("events", [])}
    full_w, full_h = eye.get("fullSize", [0, 0])
    log(f"  eye classification: {eye.get('perEyeCounts')}  full RT {full_w}x{full_h}")
    main_rt = eye.get("main_rt")
    log(f"  main scene RT: {main_rt}")

    # Walk actions, find drawcalls that target the main RT
    basepass_draws = {"left": [], "right": []}
    for a in _lib.walk_actions(controller):
        if not (int(a.flags) & int(rd.ActionFlags.Drawcall)):
            continue
        eid = int(a.eventId)
        outs = list(a.outputs) if a.outputs else []
        if not outs or _lib.resource_id_str(outs[0]) != main_rt:
            continue
        e_eye = by_eid.get(eid, {}).get("eye")
        if e_eye in ("left", "right"):
            basepass_draws[e_eye].append({
                "eventId": eid,
                "numIndices": int(a.numIndices),
                "numInstances": int(a.numInstances),
            })
    log(f"  basepass draws: left={len(basepass_draws['left'])} right={len(basepass_draws['right'])}")
    write_json("ii_basepass_draws.json", basepass_draws)

    if not basepass_draws["left"] or not basepass_draws["right"]:
        log("  FATAL: no basepass draws on one or both eyes — skipping rest")
        os._exit(1)

    # Pick a pair: pick a left draw with many indices (probably scene geometry)
    # and the closest right draw with similar size.
    L_pick = max(basepass_draws["left"], key=lambda d: d["numIndices"])
    R_pick = min(basepass_draws["right"],
                 key=lambda d: abs(d["numIndices"] - L_pick["numIndices"]))
    L = int(L_pick["eventId"])
    R = int(R_pick["eventId"])
    log(f"  picked pair: L={L} (idx={L_pick['numIndices']})  R={R} (idx={R_pick['numIndices']})")

    # =============================================================
    # STAGE III — CB0 dump + diff
    # =============================================================
    log("=== Stage III: cb0 (View cbuffer) diff ===")

    def dump_cb_at_event(eid, stage_enum, slot_idx=0, max_bytes=2048):
        controller.SetFrameEvent(int(eid), True)
        pipe = controller.GetPipelineState()
        try:
            arr = pipe.GetConstantBlocks(stage_enum, False)
        except Exception:
            return None
        if not arr or len(arr) <= slot_idx:
            return None
        used = arr[slot_idx]
        desc = used.descriptor
        if desc is None or desc.resource is None:
            return None
        try:
            data = bytes(controller.GetBufferData(
                desc.resource, int(desc.byteOffset),
                min(int(desc.byteSize) or max_bytes, max_bytes)))
            return data
        except Exception:
            return None

    l_cb = dump_cb_at_event(L, rd.ShaderStage.Pixel, 0)
    r_cb = dump_cb_at_event(R, rd.ShaderStage.Pixel, 0)
    if l_cb is None or r_cb is None:
        log("  could not dump cb0 on one or both eyes")
        cb_report = {"error": "cb0 unavailable", "leftBytes": l_cb is not None,
                     "rightBytes": r_cb is not None}
    else:
        log(f"  left  cb0 size: {len(l_cb)}  md5={hashlib.md5(l_cb).hexdigest()[:16]}")
        log(f"  right cb0 size: {len(r_cb)}  md5={hashlib.md5(r_cb).hexdigest()[:16]}")
        # Diff at float granularity
        n_floats = min(len(l_cb), len(r_cb)) // 4
        diffs = []
        for i in range(n_floats):
            lb = l_cb[i * 4:i * 4 + 4]; rb = r_cb[i * 4:i * 4 + 4]
            if lb != rb:
                try:
                    lf = struct.unpack("<f", lb)[0]
                    rf = struct.unpack("<f", rb)[0]
                except Exception:
                    lf = rf = None
                try:
                    li = struct.unpack("<I", lb)[0]
                    ri = struct.unpack("<I", rb)[0]
                except Exception:
                    li = ri = None
                diffs.append({
                    "byteOffset": i * 4,
                    "float_index": i,
                    "cbReg_index": i // 4,
                    "cbReg_component": i % 4,
                    "left_float": lf, "right_float": rf,
                    "left_uint": li, "right_uint": ri,
                })
        log(f"  diff floats: {len(diffs)} / {n_floats}")
        # Highlight known per-eye View fields by cbReg index
        named = {
            (4, 5, 6, 7): "ViewToClip / Translated WorldToClip matrix rows",
            (44, 45, 46, 47): "ClipToView / Translated ClipToWorld",
            (121, 122): "PreViewTranslation LWC high/low",
            (148,): "ViewRect (TopLeft + size)",
            (151,): "ViewSizeAndInvSize",
            (159,): "TangentToWorld basis",
            (160,): "MaterialCurve constants",
            (165,): "Random seed / temporal",
            (252, 253): "FogLogScale / FogLogOffset",
            (258,): "VolumetricFogScreenToUV scale/bias",
            (320,): "HDR clamp ceiling",
            (321,): "View flags bitfield",
        }
        per_reg = {}
        for d in diffs:
            reg = d["cbReg_index"]
            per_reg.setdefault(reg, []).append(d)
        annotated_regs = []
        for reg in sorted(per_reg.keys()):
            label = ""
            for rng, name in named.items():
                if reg in rng:
                    label = name; break
            annotated_regs.append({
                "cbReg": reg,
                "label": label,
                "diffComponents": [(d["cbReg_component"], d["left_float"], d["right_float"])
                                   for d in per_reg[reg]],
            })
        cb_report = {
            "leftEventId": L, "rightEventId": R,
            "leftBytes": len(l_cb), "rightBytes": len(r_cb),
            "leftMd5": hashlib.md5(l_cb).hexdigest()[:16],
            "rightMd5": hashlib.md5(r_cb).hexdigest()[:16],
            "diffFloatCount": len(diffs),
            "totalFloats": n_floats,
            "byCbReg": annotated_regs,
            "topDiffs": diffs[:40],
        }
        # Surface the high-value labelled regs
        for ar in annotated_regs:
            if ar["label"]:
                log(f"  reg[{ar['cbReg']}] = {ar['label']}: {ar['diffComponents'][:4]}")
    write_json("iii_cb0_diff.json", cb_report)

    # =============================================================
    # STAGE IV — resource lineage for shared UAVs
    # =============================================================
    log("=== Stage IV: resource lineage for shared UAVs ===")
    lineage_results = {}
    for res in SHARED_UAVS:
        try:
            lin = resource_lineage.lineage(CAPTURE, res)
            lineage_results[res] = lin
            log(f"  {res}: usageCount={lin.get('usageCount', 0)} "
                f"writeCount={lin.get('writeCount', 0)} "
                f"readCount={lin.get('readCount', 0)}")
        except Exception as e:
            lineage_results[res] = {"error": str(e)}
            log(f"  {res}: ERROR {e}")
    write_json("iv_shared_uav_lineage.json", lineage_results)

    # =============================================================
    # STAGE V — confirm right-eye basepass samples shared UAVs
    # =============================================================
    log("=== Stage V: right-eye basepass binding inspection ===")
    controller.SetFrameEvent(R, True)
    pipe = controller.GetPipelineState()
    bindings_at_R = []
    for stage_enum in (rd.ShaderStage.Pixel, rd.ShaderStage.Vertex):
        try:
            arr = pipe.GetReadOnlyResources(stage_enum, False)
        except Exception:
            arr = []
        for u in arr:
            desc = u.descriptor
            if desc is None or desc.resource is None:
                continue
            bindings_at_R.append({
                "stage": _lib.shader_stage_name(stage_enum),
                "register": int(u.access.index),
                "resource": _lib.resource_id_str(desc.resource),
            })
    matches = [b for b in bindings_at_R if b["resource"] in SHARED_UAVS]
    log(f"  bindings at right-eye basepass (R={R}): {len(bindings_at_R)} read-only")
    log(f"  of which shared-UAV matches: {len(matches)}")
    for m in matches[:10]:
        log(f"    {m}")
    write_json("v_right_basepass_bindings.json", {
        "rightEventId": R,
        "bindings": bindings_at_R,
        "sharedUavMatches": matches,
    })

    # =============================================================
    # STAGE VI — debug a right-eye basepass pixel and compare to left
    # =============================================================
    log("=== Stage VI: debug right-eye basepass pixel ===")
    # Pick a pixel in the right-eye half of the main RT.
    # full_w/full_h is the inferred main RT size (e.g. 860x484).
    # The screen is sub-divided per eye by the engine; we'll try the
    # center of the right half.
    pixel_x = max(0, full_w - 100)
    pixel_y = max(0, full_h // 2)
    log(f"  trying DebugPixel at ({pixel_x}, {pixel_y})")
    try:
        diff = debug_pixel_pair.diff_traces(CAPTURE, L, R, pixel_x, pixel_y, max_steps=1024)
        write_json("vi_debug_pixel_pair.json", diff)
        if diff.get("error"):
            log(f"  error: {diff['error']}")
        else:
            log(f"  left steps: {diff.get('leftStepCount')}  right steps: {diff.get('rightStepCount')}")
            fd = diff.get("firstDivergence")
            if fd:
                log(f"  first divergence at step {fd.get('step')}: "
                    f"{len(fd.get('divergentRegisters') or {})} registers differ")
            else:
                log("  no register-level divergence — could be that the pixel is identical or untraced")
    except Exception:
        log("  DebugPixel failed:\n" + traceback.format_exc())

finally:
    controller.Shutdown()
    cap.Shutdown()

log("=== DONE ===")
os._exit(0)
