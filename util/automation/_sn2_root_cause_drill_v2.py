"""Better basepass-pair picker + retry Stages III/V/VI.

The first drill picked the biggest-indices left draw but matched it
against a tiny right draw. This time:
  1. Find all draws targeting the main scene RT
  2. Group them by PS-entry-point name ('MainPS', 'DeferredShadingMainPS',
     'BasePassPS', etc.)
  3. For each group, pick a pair where BOTH eyes have a draw of similar
     size (similar numIndices)
  4. Run CB0 diff + bindings + DebugPixel
"""

import hashlib
import json
import os
import struct
import sys
import time
import traceback

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_root_v2"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

log("import modules...")
from util.automation import _lib, debug_pixel_pair, eye_classifier_temporal  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"


def write_json(name, payload):
    p = os.path.join(OUT_DIR, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    log(f"  -> {name} ({os.path.getsize(p)} bytes)")


log("open_capture...")
cap, controller = _lib.open_capture(CAPTURE)
try:
    sdfile = controller.GetStructuredFile()
    log(f"sdfile loaded")

    log("temporal eye classification...")
    eye = eye_classifier_temporal.classify(CAPTURE)
    by_eid = {int(e["eventId"]): e.get("eye", "unknown") for e in eye["events"]}
    log(f"  per-eye: {eye.get('perEyeCounts')}  full {eye.get('fullSize')}  main_rt={eye.get('main_rt')}")
    main_rt = eye.get("main_rt")
    full_w, full_h = eye.get("fullSize", [0, 0])

    # =============================================================
    # Walk all draws targeting the main RT, capture PS entry name
    # =============================================================
    log("walking actions to find draws targeting main RT...")
    candidates = []   # list of (eid, eye, numIndices, psEntry, psHash, psResourceId)
    for a in _lib.walk_actions(controller):
        if not (int(a.flags) & int(rd.ActionFlags.Drawcall)):
            continue
        outs = list(a.outputs) if a.outputs else []
        if not outs or _lib.resource_id_str(outs[0]) != main_rt:
            continue
        eid = int(a.eventId)
        candidates.append({
            "eventId": eid,
            "eye": by_eid.get(eid, "unknown"),
            "numIndices": int(a.numIndices),
            "numInstances": int(a.numInstances),
        })
    log(f"  {len(candidates)} draws target main RT")
    if len(candidates) > 800:
        log("  too many — taking 50 largest")
        candidates = sorted(candidates, key=lambda c: -c["numIndices"])[:50]

    # Now SetFrameEvent on each candidate to get PS entry + hash
    log(f"reading PS reflection for each candidate ({len(candidates)} events)...")
    for c in candidates:
        try:
            controller.SetFrameEvent(c["eventId"], True)
            pipe = controller.GetPipelineState()
            try:
                refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
            except Exception:
                refl = None
            if refl is None or len(refl.rawBytes) == 0:
                c["psEntry"] = None
                c["psHash"] = None
            else:
                c["psEntry"] = str(refl.entryPoint)
                c["psHash"] = _lib.shader_bytecode_hash(bytes(refl.rawBytes))
        except Exception as exc:
            c["error"] = str(exc)
    write_json("candidates.json", candidates)
    log(f"  candidates with PS entry: {sum(1 for c in candidates if c.get('psEntry'))}")

    # Group by PS hash
    from collections import defaultdict
    groups = defaultdict(lambda: {"left": [], "right": [], "entry": "?"})
    for c in candidates:
        if not c.get("psHash"):
            continue
        g = groups[c["psHash"]]
        g["entry"] = c.get("psEntry")
        if c["eye"] in ("left", "right"):
            g[c["eye"]].append(c)
    log(f"  {len(groups)} distinct PS hashes")
    # Surface groups that have both eyes
    bi_eye_groups = []
    for h, g in groups.items():
        if g["left"] and g["right"]:
            bi_eye_groups.append({
                "hash": h, "entry": g["entry"],
                "leftCount": len(g["left"]), "rightCount": len(g["right"]),
                "leftSamples": [c["eventId"] for c in sorted(g["left"], key=lambda x: -x["numIndices"])[:3]],
                "rightSamples": [c["eventId"] for c in sorted(g["right"], key=lambda x: -x["numIndices"])[:3]],
            })
    log(f"  {len(bi_eye_groups)} PS hashes have both-eye draws")
    write_json("bi_eye_groups.json", bi_eye_groups)
    for g in bi_eye_groups[:10]:
        log(f"  hash={g['hash'][:16]}.. entry={g['entry']} L:{g['leftCount']} R:{g['rightCount']}  "
            f"L samples {g['leftSamples']} R samples {g['rightSamples']}")

    if not bi_eye_groups:
        log("FATAL: no PS shader runs on both eyes — can't find a matched basepass pair")
        os._exit(1)

    # Pick best pair: largest group with both eyes, biggest draw on each side
    best_group = max(bi_eye_groups, key=lambda g: min(g["leftCount"], g["rightCount"]) * (g["leftCount"] + g["rightCount"]))
    log(f"BEST group: hash={best_group['hash'][:16]} entry={best_group['entry']}")
    g = groups[best_group["hash"]]
    L_pick = sorted(g["left"], key=lambda c: -c["numIndices"])[0]
    R_pick = sorted(g["right"], key=lambda c: -c["numIndices"])[0]
    L = int(L_pick["eventId"]); R = int(R_pick["eventId"])
    log(f"BEST pair: L={L} (idx={L_pick['numIndices']})  R={R} (idx={R_pick['numIndices']})")

    # =============================================================
    # Stage III — CB0 diff
    # =============================================================
    log("=== Stage III: CB0 diff at BEST pair ===")

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
    if l_cb is None or r_cb is None:
        log(f"  cb0 dump: L={l_cb is not None} R={r_cb is not None}")
        write_json("iii_cb0_diff.json", {"error": "cb0 dump failed"})
    else:
        log(f"  L cb0 {len(l_cb)}B md5={hashlib.md5(l_cb).hexdigest()[:16]}")
        log(f"  R cb0 {len(r_cb)}B md5={hashlib.md5(r_cb).hexdigest()[:16]}")
        n_floats = min(len(l_cb), len(r_cb)) // 4
        diffs = []
        for i in range(n_floats):
            lb = l_cb[i * 4:i * 4 + 4]; rb = r_cb[i * 4:i * 4 + 4]
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
                    "byteOffset": i * 4, "floatIndex": i, "cbReg": i // 4, "comp": i % 4,
                    "left_float": lf, "right_float": rf,
                    "left_uint": li, "right_uint": ri,
                })
        log(f"  diff floats: {len(diffs)} / {n_floats}")

        named = {
            **{r: "TranslatedWorldToClip matrix" for r in (4, 5, 6, 7)},
            **{r: "ClipToTranslatedWorld" for r in (44, 45, 46, 47)},
            121: "PreViewTranslation_HighWord", 122: "PreViewTranslation_LowWord",
            148: "ViewRect", 151: "ViewSizeAndInvSize",
            159: "BasisNormalScale", 160: "MaterialCurve",
            165: "RandomSeedAndFrameCounter",
            252: "FogLogScale", 253: "FogLogOffset",
            258: "VolumetricFogScreenToUVScale",
            320: "HDRClampMax", 321: "ViewFlagsBitfield",
        }
        per_reg = {}
        for d in diffs:
            per_reg.setdefault(d["cbReg"], []).append(d)
        cb_report = {
            "leftEventId": L, "rightEventId": R,
            "leftMd5": hashlib.md5(l_cb).hexdigest()[:16],
            "rightMd5": hashlib.md5(r_cb).hexdigest()[:16],
            "diffFloatCount": len(diffs), "totalFloats": n_floats,
            "regsWithDiffs": [],
        }
        # Annotated registers with diffs
        for reg in sorted(per_reg.keys()):
            label = named.get(reg, "")
            entries = []
            for d in per_reg[reg]:
                entries.append({"comp": d["comp"], "L": d["left_float"], "R": d["right_float"]})
            cb_report["regsWithDiffs"].append({
                "reg": reg, "label": label, "components": entries,
            })
        cb_report["topDiffs"] = diffs[:50]
        write_json("iii_cb0_diff.json", cb_report)
        # Log labeled regs
        log(f"  labeled diff registers:")
        for entry in cb_report["regsWithDiffs"]:
            if entry["label"]:
                comps = entry["components"][:4]
                log(f"    reg[{entry['reg']}] ({entry['label']}): {comps}")

    # =============================================================
    # Stage V — bindings comparison
    # =============================================================
    log("=== Stage V: PS bindings comparison (L vs R basepass) ===")

    def collect_bindings(eid):
        controller.SetFrameEvent(eid, True)
        pipe = controller.GetPipelineState()
        out = []
        for stage in (rd.ShaderStage.Pixel, rd.ShaderStage.Vertex):
            try:
                arr = pipe.GetReadOnlyResources(stage, False)
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
                    "type": _lib.descriptor_type_name(u.access.type),
                })
        return out

    l_binds = collect_bindings(L); r_binds = collect_bindings(R)
    # Build per-(stage, register) dict
    l_dict = {(b["stage"], b["register"]): b for b in l_binds}
    r_dict = {(b["stage"], b["register"]): b for b in r_binds}
    keys = sorted(set(l_dict.keys()) | set(r_dict.keys()), key=lambda k: (k[0], k[1]))
    same_resource = []
    diff_resource = []
    for k in keys:
        lb = l_dict.get(k); rb = r_dict.get(k)
        if lb and rb:
            if lb["resource"] == rb["resource"]:
                same_resource.append({"stage": k[0], "reg": k[1], "resource": lb["resource"]})
            else:
                diff_resource.append({"stage": k[0], "reg": k[1],
                                       "L": lb["resource"], "R": rb["resource"]})
        elif lb:
            diff_resource.append({"stage": k[0], "reg": k[1], "L": lb["resource"], "R": "NONE"})
        else:
            diff_resource.append({"stage": k[0], "reg": k[1], "L": "NONE", "R": rb["resource"]})
    log(f"  same resource bindings: {len(same_resource)}")
    log(f"  diff resource bindings: {len(diff_resource)}")
    log(f"  shared resources at PS register 5 (t5 — fog volume):")
    for b in same_resource:
        if b["stage"] == "Pixel" and b["reg"] == 5:
            log(f"    SHARED PS t5 = {b['resource']}")
    for b in diff_resource:
        if b["stage"] == "Pixel" and b["reg"] == 5:
            log(f"    DIFF    PS t5 L={b.get('L')} R={b.get('R')}")
    write_json("v_bindings_compare.json", {
        "leftEventId": L, "rightEventId": R,
        "sameResource": same_resource, "diffResource": diff_resource,
    })

    # =============================================================
    # Stage VI — DebugPixel on right-eye basepass pixel
    # =============================================================
    log("=== Stage VI: DebugPixel on right-eye basepass pixel ===")
    # Try center of right-eye half
    pixel_x = max(0, full_w * 3 // 4)
    pixel_y = max(0, full_h // 2)
    log(f"  trying pixel ({pixel_x}, {pixel_y})")
    try:
        d = debug_pixel_pair.diff_traces(CAPTURE, L, R, pixel_x, pixel_y, max_steps=1024)
        write_json("vi_debug_pixel_pair.json", d)
        log(f"  L steps {d.get('leftStepCount')} R steps {d.get('rightStepCount')}  "
            f"error: {d.get('error')}")
        fd = d.get("firstDivergence")
        if fd:
            log(f"  first divergence at step {fd.get('step')}")
            divs = fd.get("divergentRegisters") or {}
            for k, v in list(divs.items())[:10]:
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
