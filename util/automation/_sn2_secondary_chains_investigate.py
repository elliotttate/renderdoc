"""Secondary-chain investigation after TLV ruled out as bug surface.

Investigate 4 LEFT-only producers + their right-eye consumers:
  A. 141622 (1264×712 R11G11B10F): written by 9c2134cc + 088d809a, read by
     events 13979 (WaterRefractionCopyPS), 15033 (Main), 15590 (MeshBlendMainPS)
  B. 141308 (315×355 R11G11B10F): written by d04fe146, read by event 15736 (MainPS t2)
  C. 141304 (315×355 R11G11B10F): written by eeeecca6, read by event 15736 (MainPS t3)

Tasks:
  1. PS CRC32 + DXBC dump for each right-eye consumer
  2. Full PS SRV bindings at each (so we see WHICH slot binds the target)
  3. Trace 141622 lineage: all writers + readers, is it sampled by tonemap?
  4. Enumerate all LEFT-only producers writing to >= 640x360 targets
  5. Characterize event 13979 (WaterRefractionCopyPS) — basepass-time or post-process?
"""

import hashlib
import json
import os
import sys
import time
import zlib

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
SHADER_DIR = os.path.join(OUT_DIR, "dxbc")
LOG = os.path.join(OUT_DIR, "secondary_chains.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"

RIGHT_EYE_CONSUMERS = [
    # (event, expected reading, comment)
    (13979, "ResourceId::141622", "WaterRefractionCopyPS reader of 141622"),
    (15033, "ResourceId::141622", "Main reader of 141622"),
    (15590, "ResourceId::141622", "MeshBlendMainPS reader of 141622"),
    (15736, "ResourceId::141308", "MainPS t2 reader of 141308"),
    # 15736 ALSO reads 141304 at t3 — same event, multiple inputs
]

LEFT_ONLY_TARGETS_TO_TRACE = [
    "ResourceId::141622",  # 1264x712 R11G11B10F
    "ResourceId::141308",  # 315x355
    "ResourceId::141304",  # 315x355
]


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


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


cap, controller = _lib.open_capture(CAPTURE)
try:
    textures_by_id = {res_id(t.resourceId): t for t in controller.GetTextures()}

    # =================================================================
    # Task 1+2: PS CRC + bindings for each right-eye consumer
    # =================================================================
    log("=== TASK 1+2: Right-eye consumer CRCs + bindings ===")
    consumer_results = []
    for eid, expected_rid, comment in RIGHT_EYE_CONSUMERS:
        log(f"\n--- event {eid} ({comment}) ---")
        try:
            controller.SetFrameEvent(eid, True)
            d3d12 = controller.GetD3D12PipelineState()
            pipe = controller.GetPipelineState()
        except Exception as e:
            log(f"  SetFrameEvent failed: {e}"); continue

        # PS shader
        try:
            refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
        except Exception:
            refl = None
        if not refl or len(refl.rawBytes) == 0:
            log("  no PS reflection"); continue
        bytecode = bytes(refl.rawBytes)
        md5 = hashlib.md5(bytecode).hexdigest()
        c_zlib = zlib.crc32(bytecode) & 0xFFFFFFFF
        c_cast = crc32c(bytecode)
        entry = str(refl.entryPoint)

        # Save .dxbc
        fname = os.path.join(SHADER_DIR, f"Secondary_{entry}_{md5[:16]}.dxbc")
        with open(fname, "wb") as f:
            f.write(bytecode)

        # Viewport
        vp_x = None
        try:
            if len(d3d12.rasterizer.viewports) > 0:
                vp_x = float(d3d12.rasterizer.viewports[0].x)
        except Exception:
            pass

        # PSO + root sig
        pso_id = res_id(d3d12.pipelineResourceId)
        rs_id = res_id(d3d12.rootSignature.resourceId)

        # PS SRV bindings — full list
        bindings = []
        try:
            for u in pipe.GetReadOnlyResources(rd.ShaderStage.Pixel, False):
                d = u.descriptor
                if d is None or d.resource is None:
                    continue
                reg = int(u.access.index)
                rid = res_id(d.resource)
                tex = textures_by_id.get(rid)
                if tex is None:
                    desc = "(not texture)"
                else:
                    desc = f"{int(tex.width)}x{int(tex.height)}x{int(tex.depth)} {tex.format.Name()}"
                bindings.append({
                    "register": reg, "resource": rid, "desc": desc,
                    "descStore": res_id(u.access.descriptorStore),
                    "descStoreOffset": int(u.access.byteOffset),
                })
        except Exception:
            pass
        bindings.sort(key=lambda b: b["register"])

        # Find which slot binds the expected resource
        target_slot = next((b for b in bindings if b["resource"] == expected_rid), None)

        log(f"  PS Entry: {entry}  size: {len(bytecode)}")
        log(f"  Viewport.x: {vp_x}")
        log(f"  PSO: {pso_id}  RootSig: {rs_id}")
        log(f"  MD5:               {md5}")
        log(f"  CRC32 zlib:        0x{c_zlib:08X}")
        log(f"  CRC32 Castagnoli:  0x{c_cast:08X}")
        log(f"  saved: {fname}")
        if target_slot:
            log(f"  ★ target {expected_rid} bound at PS t{target_slot['register']}")
        else:
            log(f"  ⚠ target {expected_rid} NOT FOUND in PS SRV bindings — checking VS")
            # Try VS
            try:
                for u in pipe.GetReadOnlyResources(rd.ShaderStage.Vertex, False):
                    d = u.descriptor
                    if d and d.resource and res_id(d.resource) == expected_rid:
                        log(f"  ★ target found at VS t{int(u.access.index)}")
                        break
            except Exception:
                pass

        log(f"  Full PS SRV bindings (top 16):")
        for b in bindings[:16]:
            log(f"    t{b['register']:2} = {b['resource']}  [{b['desc']}]")

        consumer_results.append({
            "eventId": eid, "expected": expected_rid, "comment": comment,
            "entry": entry, "psSize": len(bytecode), "md5": md5,
            "crc32_zlib": f"0x{c_zlib:08X}",
            "crc32_castagnoli": f"0x{c_cast:08X}",
            "vp_x": vp_x, "pso": pso_id, "rootSig": rs_id,
            "psBindings": bindings[:20],
            "target_slot": target_slot["register"] if target_slot else None,
            "dxbcPath": fname,
        })

    # =================================================================
    # Task 3: Trace 141622 lineage
    # =================================================================
    log("\n\n=== TASK 3: Full lineage trace for ResourceId::141622 ===")
    target_rid_str = "ResourceId::141622"
    target = None
    for r in controller.GetResources():
        if res_id(r.resourceId) == target_rid_str:
            target = r.resourceId; break
    if target is None:
        log("  not found")
    else:
        usage = controller.GetUsage(target)
        # Classify all usages
        writers = []   # ColorTarget, CS_RWResource, CopyDst
        readers = []   # PS_Resource, VS_Resource, CS_Resource (non-RW)
        misc = []
        for u in usage:
            kind = str(u.usage).split(".")[-1]
            eid = int(u.eventId)
            if "Discard" in kind or "Barrier" in kind:
                continue
            if "RWResource" in kind or "ColorTarget" in kind or "ColourTarget" in kind \
               or "CopyDst" in kind:
                writers.append((eid, kind))
            elif "Resource" in kind or "CopySrc" in kind:
                readers.append((eid, kind))
            else:
                misc.append((eid, kind))
        log(f"  total usages: {len(usage)}, writers: {len(writers)}, readers: {len(readers)}, misc: {len(misc)}")
        log(f"  All WRITERS:")
        for eid, kind in writers:
            log(f"    eid {eid} {kind}")
        log(f"  All READERS:")
        for eid, kind in readers:
            # Classify by event id range (rough eye guess) and look up PS hash
            try:
                controller.SetFrameEvent(eid, True)
                pipe = controller.GetPipelineState()
                refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                entry = str(refl.entryPoint) if refl and len(refl.rawBytes) > 0 else None
                d3d12 = controller.GetD3D12PipelineState()
                vp_x = None
                if len(d3d12.rasterizer.viewports) > 0:
                    vp_x = float(d3d12.rasterizer.viewports[0].x)
            except Exception:
                entry = None; vp_x = None
            log(f"    eid {eid} {kind:18} PS={entry} vp_x={vp_x}")

    # =================================================================
    # Task 4: All LEFT-only producers writing ≥ 640x360
    # =================================================================
    log("\n\n=== TASK 4: LEFT-only producers writing >= 640x360 ===")
    inv = json.load(open(os.path.join(OUT_DIR, "ps_crc_inventory.json")))
    left_events_map = inv.get("left", {})
    left_only_keys = inv.get("left_only", [])
    large_producers = []
    for key in left_only_keys:
        eids = left_events_map.get(key, [])
        if not eids:
            continue
        eid = eids[0]
        try:
            controller.SetFrameEvent(eid, True)
            d3d12 = controller.GetD3D12PipelineState()
        except Exception:
            continue
        try:
            for i, rt in enumerate(d3d12.outputMerger.renderTargets):
                rid = res_id(rt.resource)
                if not rid:
                    continue
                tex = textures_by_id.get(rid)
                if tex is None: continue
                if int(tex.width) >= 640 and int(tex.height) >= 360:
                    large_producers.append({
                        "ps_key": key, "first_event": eid,
                        "rtv_slot": i, "resource": rid,
                        "dim": f"{int(tex.width)}x{int(tex.height)}x{int(tex.depth)}",
                        "format": str(tex.format.Name()),
                    })
        except Exception:
            pass
    log(f"  found {len(large_producers)} LEFT-only producers writing to >= 640x360 targets:")
    for lp in large_producers:
        h, entry = lp["ps_key"].split("|", 1)
        log(f"    {h[:16]} ({entry}) @ eid {lp['first_event']}: RTV{lp['rtv_slot']} = {lp['resource']}  [{lp['dim']} {lp['format']}]")

    # =================================================================
    # Task 5: Characterize WaterRefractionCopyPS at event 13979
    # =================================================================
    log("\n\n=== TASK 5: Characterize WaterRefractionCopyPS @ event 13979 ===")
    try:
        controller.SetFrameEvent(13979, True)
        d3d12 = controller.GetD3D12PipelineState()
        pipe = controller.GetPipelineState()

        # Viewport + scissor (region info)
        try:
            for i, vp in enumerate(d3d12.rasterizer.viewports[:2]):
                log(f"  Viewport[{i}]: x={float(vp.x)}, y={float(vp.y)}, w={float(vp.width)}, h={float(vp.height)}")
            for i, sc in enumerate(d3d12.rasterizer.scissors[:2]):
                log(f"  Scissor[{i}]: left={int(sc.x)}, top={int(sc.y)}, right={int(sc.x+sc.width)}, bottom={int(sc.y+sc.height)}")
        except Exception:
            pass
        # Output RT
        try:
            for i, rt in enumerate(d3d12.outputMerger.renderTargets[:2]):
                rid = res_id(rt.resource)
                tex = textures_by_id.get(rid) if rid else None
                desc = f"{int(tex.width)}x{int(tex.height)} {tex.format.Name()}" if tex else "?"
                log(f"  Output RT[{i}]: {rid}  [{desc}]")
        except Exception:
            pass
        # Action info: is this a draw or a CopyResource?
        for a in _lib.walk_actions(controller):
            if int(a.eventId) == 13979:
                sdfile = controller.GetStructuredFile()
                log(f"  Action name: {a.GetName(sdfile)}")
                log(f"  Flags: {a.flags}")
                log(f"  NumIndices: {int(a.numIndices)}")
                break
        # PS bytecode (already extracted above)
    except Exception as e:
        log(f"  failed: {e}")

    with open(os.path.join(OUT_DIR, "secondary_chains.json"), "w") as f:
        json.dump({
            "consumer_results": consumer_results,
            "large_left_only_producers": large_producers,
        }, f, indent=2, default=str)
    log("\nwrote secondary_chains.json")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
