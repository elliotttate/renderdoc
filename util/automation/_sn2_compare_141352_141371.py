"""Trace the LEFT-eye pre-composite (141352) vs RIGHT-eye pre-composite (141371)
producer chains. Find the asymmetric pipelines.

For each:
  - Full write history
  - Last writers (the final tonemap output)
  - The chain of inputs feeding each
  - Compare per-eye PS CRCs that contribute to each
"""

import hashlib
import json
import os
import sys
import time
import zlib

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "compare_141352_141371.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"

TARGETS = ["ResourceId::141352", "ResourceId::141371"]


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def crc32c(data):
    table = []
    for i in range(256):
        v = i
        for _ in range(8):
            v = (v >> 1) ^ (0x82F63B78 if (v & 1) else 0)
        table.append(v)
    crc = 0xFFFFFFFF
    for byte in data:
        crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


cap, controller = _lib.open_capture(CAPTURE)
try:
    textures_by_id = {res_id(t.resourceId): t for t in controller.GetTextures()}

    chains = {}
    for target_rid_str in TARGETS:
        log(f"\n=== {target_rid_str} ===")
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
        readers = []
        for u in usage:
            kind = str(u.usage).split(".")[-1]
            if "Discard" in kind or "Barrier" in kind:
                continue
            eid = int(u.eventId)
            if "RWResource" in kind or "ColorTarget" in kind or "ColourTarget" in kind:
                writers.append((eid, kind))
            elif "Resource" in kind or "CopySrc" in kind:
                readers.append((eid, kind))

        log(f"  {len(writers)} writers, {len(readers)} readers")
        writer_details = []
        for eid, kind in writers:
            try:
                controller.SetFrameEvent(eid, True)
                pipe = controller.GetPipelineState()
                refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                if refl and len(refl.rawBytes) > 0:
                    h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))
                    entry = str(refl.entryPoint)
                    crc_zlib = zlib.crc32(bytes(refl.rawBytes)) & 0xFFFFFFFF
                    crc_cast = crc32c(bytes(refl.rawBytes))
                else:
                    h = None; entry = None; crc_zlib = crc_cast = None
                d3d12 = controller.GetD3D12PipelineState()
                vp_x = None; vp_w = None
                if len(d3d12.rasterizer.viewports) > 0:
                    vp = d3d12.rasterizer.viewports[0]
                    vp_x = float(vp.x); vp_w = float(vp.width)
                pso = res_id(d3d12.pipelineResourceId)
                # First few SRVs
                srvs = []
                try:
                    arr = pipe.GetReadOnlyResources(rd.ShaderStage.Pixel, False)
                    for u2 in arr:
                        if not u2.descriptor or not u2.descriptor.resource:
                            continue
                        reg = int(u2.access.index)
                        rid_str = res_id(u2.descriptor.resource)
                        srvs.append((reg, rid_str))
                except Exception:
                    pass
                srvs.sort(key=lambda s: s[0])
            except Exception as e:
                log(f"  failed at eid {eid}: {e}")
                continue
            writer_details.append({
                "eventId": eid, "usage": kind, "entry": entry,
                "psHash": h[:16] if h else None,
                "crc32_zlib": f"0x{crc_zlib:08X}" if crc_zlib is not None else None,
                "crc32_castagnoli": f"0x{crc_cast:08X}" if crc_cast is not None else None,
                "vp_x": vp_x, "vp_w": vp_w, "pso": pso,
                "first_srvs": srvs[:6],
            })

        log("  ALL writers chronologically:")
        for w in writer_details:
            log(f"    eid {w['eventId']:5}  PS={w['entry'] or '?':30s}  "
                f"vp=({w['vp_x']},{w['vp_w']})  PSO={w['pso']}  "
                f"crc_zlib={w['crc32_zlib']}")
            for reg, rid in w["first_srvs"][:4]:
                log(f"      t{reg} = {rid}")

        chains[target_rid_str] = {
            "info": {"w": int(info.width), "h": int(info.height),
                     "format": str(info.format.Name())} if info else None,
            "writers": writer_details,
        }

    # =================================================================
    # DIFF
    # =================================================================
    log("\n\n=== DIFF: 141352 vs 141371 producer chain ===")
    w1 = chains.get("ResourceId::141352", {}).get("writers", [])
    w2 = chains.get("ResourceId::141371", {}).get("writers", [])
    log(f"  141352 writers: {len(w1)}, 141371 writers: {len(w2)}")
    set1 = set(w.get("psHash") for w in w1)
    set2 = set(w.get("psHash") for w in w2)
    log(f"  unique PS hashes writing 141352: {len(set1)}")
    log(f"  unique PS hashes writing 141371: {len(set2)}")
    only_141352 = set1 - set2
    only_141371 = set2 - set1
    common = set1 & set2
    log(f"\n  PSes writing to 141352 ONLY: {len(only_141352)}")
    for h in sorted(only_141352):
        # Get entry name
        e = next((w["entry"] for w in w1 if w["psHash"] == h), "?")
        crc = next((w["crc32_zlib"] for w in w1 if w["psHash"] == h), "?")
        log(f"    {h} ({e}) crc32_zlib={crc}")
    log(f"\n  PSes writing to 141371 ONLY: {len(only_141371)}")
    for h in sorted(only_141371):
        e = next((w["entry"] for w in w2 if w["psHash"] == h), "?")
        crc = next((w["crc32_zlib"] for w in w2 if w["psHash"] == h), "?")
        log(f"    {h} ({e}) crc32_zlib={crc}")
    log(f"\n  PSes writing to BOTH (common): {len(common)}")

    with open(os.path.join(OUT_DIR, "compare_141352_141371.json"), "w") as f:
        json.dump(chains, f, indent=2, default=str)
    log("\nwrote compare_141352_141371.json")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
