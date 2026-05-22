"""Focused verification — what's the PS CRC and PS SRV bindings at the
right-eye consumer events 14845 and 14853 in the live no-UEVR capture?

Memory-light: only 2 SetFrameEvent calls.

Answers:
  1. PS CRC + entry point at each event
  2. PS root[0] / shader-stage SRV bindings, especially slots 3 + 5
  3. Verifies 24x24x24 R16G16B16A16_FLOAT TLV is bound at t3 + t5
  4. Comparison to LEFT consumer events 14715, 14723 (same shader?)
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "verify_14845_14853.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"

# Right-eye consumers (from prior analysis)
RIGHT_EVENTS = [14845, 14853]
# Left-eye consumers (to compare PS CRC + bindings)
LEFT_EVENTS = [14715, 14723]


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


cap, controller = _lib.open_capture(CAPTURE)
try:
    textures_by_id = {res_id(t.resourceId): t for t in controller.GetTextures()}

    def inspect(eid, label):
        log(f"\n=== {label} eid {eid} ===")
        controller.SetFrameEvent(eid, True)
        d3d12 = controller.GetD3D12PipelineState()
        pipe = controller.GetPipelineState()

        # PS shader hash + entry
        h = None
        entry = None
        try:
            refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
            if refl and len(refl.rawBytes) > 0:
                h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))
                entry = str(refl.entryPoint)
        except Exception as e:
            log(f"  ps refl error: {e}")
        log(f"  PS CRC (RD MD5): {h}")
        log(f"  PS CRC (first 16): {h[:16] if h else None}")
        log(f"  PS Entry: {entry}")

        # Viewport
        try:
            if len(d3d12.rasterizer.viewports) > 0:
                v = d3d12.rasterizer.viewports[0]
                log(f"  Viewport: x={float(v.x)}, y={float(v.y)}, w={float(v.width)}, h={float(v.height)}")
        except Exception:
            pass

        # PSO + root sig
        try:
            log(f"  PSO: {res_id(d3d12.pipelineResourceId)}")
            log(f"  RootSig: {res_id(d3d12.rootSignature.resourceId)}")
        except Exception:
            pass

        # Full PS SRV bindings t0..t14
        log(f"  PS SRV bindings (registers 0-14):")
        bindings = []
        try:
            for u in pipe.GetReadOnlyResources(rd.ShaderStage.Pixel, False):
                d = u.descriptor
                if d is None or d.resource is None:
                    continue
                reg = int(u.access.index)
                if reg > 14:
                    continue
                rid = res_id(d.resource)
                tex = textures_by_id.get(rid)
                if tex is None:
                    desc = "(not texture)"
                else:
                    desc = f"{int(tex.width)}x{int(tex.height)}x{int(tex.depth)} {tex.format.Name()}"
                    if int(tex.depth) > 1:
                        desc += " [3D]"
                heap = res_id(u.access.descriptorStore)
                off = int(u.access.byteOffset)
                bindings.append({
                    "register": reg, "resource": rid, "desc": desc,
                    "descStore": heap, "descStoreOffset": off,
                })
        except Exception as e:
            log(f"  read-only enum error: {e}")
        bindings.sort(key=lambda b: b["register"])
        for b in bindings:
            star = " ★" if b["register"] in (3, 5) else ""
            log(f"    t{b['register']:2}{star} = {b['resource']}  [{b['desc']}]  heap={b['descStore']}+{b['descStoreOffset']}")

        # Identify any 24x24x24 R16G16B16A16F bindings
        tlv_bindings = [b for b in bindings if "24x24x24" in b["desc"] and "R16G16B16A16" in b["desc"]]
        if tlv_bindings:
            log(f"  ✅ {len(tlv_bindings)} TLV-shape bindings found")
            for b in tlv_bindings:
                log(f"     t{b['register']} = {b['resource']}")
        else:
            log(f"  ❌ No 24x24x24 R16G16B16A16F bindings found")

        return {"eventId": eid, "psHash": h, "psEntry": entry,
                "bindings": bindings, "tlv_bindings": tlv_bindings}

    results = {}
    for eid in RIGHT_EVENTS:
        results[f"R_{eid}"] = inspect(eid, "RIGHT")
    for eid in LEFT_EVENTS:
        results[f"L_{eid}"] = inspect(eid, "LEFT")

    # Cross-eye comparison
    log("\n=== L vs R BINDING COMPARISON ===")
    for slot in (3, 5):
        log(f"\n  Slot t{slot}:")
        for tag in ("L_14715", "L_14723", "R_14845", "R_14853"):
            res = results.get(tag, {})
            b = next((x for x in res.get("bindings", []) if x["register"] == slot), None)
            if b:
                log(f"    {tag}: {b['resource']}  [{b['desc']}]")
            else:
                log(f"    {tag}: (not bound)")

    # PS CRC summary
    log("\n=== PS CRC SUMMARY ===")
    for tag, res in results.items():
        log(f"  {tag}: PS_CRC={res.get('psHash', '?')[:16]}  Entry={res.get('psEntry', '?')}")

    # The critical question: are all 4 events on the same PSO (= same PS CRC)?
    crcs = set(r.get("psHash") for r in results.values() if r.get("psHash"))
    log(f"\n  Unique PS CRCs across all 4 events: {len(crcs)}")
    if len(crcs) == 1:
        log(f"  ✅ All 4 events use the SAME PS CRC = {next(iter(crcs))[:16]}")
        log(f"     UEVR can gate the redirect on this single CRC.")
    else:
        for c in crcs:
            log(f"     CRC: {c}")
        log(f"  ⚠ Multiple CRCs — UEVR redirect gate needs to whitelist all of them.")

    with open(os.path.join(OUT_DIR, "verify_14845_14853.json"), "w") as f:
        json.dump(results, f, indent=2, default=str)
    log(f"\nwrote verify_14845_14853.json")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
