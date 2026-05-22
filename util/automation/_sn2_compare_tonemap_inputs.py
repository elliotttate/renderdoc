"""Trace tonemapper input chains:
  LEFT  t1=141653  t2=141333
  RIGHT t1=141394  t2=141370

Find what writes each, classify by eye, surface the asymmetric writers.
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "compare_tonemap_inputs.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"

# Tonemap inputs
PAIRS = [
    ("LEFT_t1", "ResourceId::141653"),
    ("RIGHT_t1", "ResourceId::141394"),
    ("LEFT_t2", "ResourceId::141333"),
    ("RIGHT_t2", "ResourceId::141370"),
]


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


cap, controller = _lib.open_capture(CAPTURE)
try:
    textures_by_id = {res_id(t.resourceId): t for t in controller.GetTextures()}

    results = {}
    for label, target_rid_str in PAIRS:
        log(f"\n=== {label}: {target_rid_str} ===")
        target = None
        for r in controller.GetResources():
            if res_id(r.resourceId) == target_rid_str:
                target = r.resourceId; break
        if target is None:
            log("  not found"); continue
        info = textures_by_id.get(target_rid_str)
        if info:
            log(f"  shape: {int(info.width)}x{int(info.height)}x{int(info.depth)} {info.format.Name()}")

        usage = controller.GetUsage(target)
        writers = []
        for u in usage:
            kind = str(u.usage).split(".")[-1]
            if "Discard" in kind or "Barrier" in kind:
                continue
            if "RWResource" in kind or "ColorTarget" in kind or "ColourTarget" in kind \
               or "CopyDst" in kind:
                writers.append((int(u.eventId), kind))
        log(f"  {len(writers)} writers")

        writer_details = []
        for eid, kind in writers:
            try:
                controller.SetFrameEvent(eid, True)
                pipe = controller.GetPipelineState()
                refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                entry = str(refl.entryPoint) if refl and len(refl.rawBytes) > 0 else None
                h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))[:16] if refl and len(refl.rawBytes) > 0 else None
                d3d12 = controller.GetD3D12PipelineState()
                vp_x = None; vp_w = None
                if len(d3d12.rasterizer.viewports) > 0:
                    vp = d3d12.rasterizer.viewports[0]
                    vp_x = float(vp.x); vp_w = float(vp.width)
                pso = res_id(d3d12.pipelineResourceId)
                # First few SRVs
                srvs = []
                try:
                    for u2 in pipe.GetReadOnlyResources(rd.ShaderStage.Pixel, False):
                        if u2.descriptor and u2.descriptor.resource:
                            srvs.append((int(u2.access.index), res_id(u2.descriptor.resource)))
                except Exception:
                    pass
                srvs.sort()
            except Exception:
                entry = None; h = None; vp_x = vp_w = None; pso = None; srvs = []
            writer_details.append({"eventId": eid, "usage": kind, "entry": entry,
                                     "psHash": h, "vp_x": vp_x, "vp_w": vp_w,
                                     "pso": pso, "first_srvs": srvs[:6]})
        # Sort by eid
        writer_details.sort(key=lambda w: w["eventId"])
        for w in writer_details:
            log(f"    eid {w['eventId']:5}  PS={w['entry'] or '?':30s} "
                f"vp=({w['vp_x']},{w['vp_w']})  PSO={w['pso']}  hash={w['psHash']}")
            for reg, rid in w["first_srvs"][:5]:
                log(f"      t{reg} = {rid}")
        results[label] = {"resource": target_rid_str, "writers": writer_details}

    # =================================================================
    # Compare LEFT vs RIGHT chains
    # =================================================================
    log("\n\n=== ASYMMETRY ANALYSIS ===")
    for slot in ("t1", "t2"):
        l_key = f"LEFT_{slot}"
        r_key = f"RIGHT_{slot}"
        if l_key not in results or r_key not in results:
            continue
        l_writers = results[l_key]["writers"]
        r_writers = results[r_key]["writers"]
        log(f"\n  Tonemapper {slot}:")
        log(f"    LEFT  {results[l_key]['resource']}: {len(l_writers)} writers")
        log(f"    RIGHT {results[r_key]['resource']}: {len(r_writers)} writers")
        if len(l_writers) != len(r_writers):
            log(f"    ⚠ DIFFERENT writer count!")
        # Diff PS hashes
        l_hashes = {w["psHash"] for w in l_writers if w["psHash"]}
        r_hashes = {w["psHash"] for w in r_writers if w["psHash"]}
        only_l = l_hashes - r_hashes
        only_r = r_hashes - l_hashes
        common = l_hashes & r_hashes
        log(f"    LEFT-only PS hashes:  {len(only_l)}")
        for h in sorted(only_l):
            e = next((w["entry"] for w in l_writers if w["psHash"] == h), "?")
            eid = next((w["eventId"] for w in l_writers if w["psHash"] == h), None)
            log(f"      {h} ({e}) @ eid {eid}")
        log(f"    RIGHT-only PS hashes: {len(only_r)}")
        for h in sorted(only_r):
            e = next((w["entry"] for w in r_writers if w["psHash"] == h), "?")
            eid = next((w["eventId"] for w in r_writers if w["psHash"] == h), None)
            log(f"      {h} ({e}) @ eid {eid}")
        log(f"    Common: {len(common)}")

    with open(os.path.join(OUT_DIR, "compare_tonemap_inputs.json"), "w") as f:
        json.dump(results, f, indent=2, default=str)
    log("\nwrote compare_tonemap_inputs.json")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
