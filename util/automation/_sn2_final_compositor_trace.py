"""Trace the final compositor: identify what writes the swapchain
backbuffer (and the right-half writer in particular).

Tasks:
  A. Find the swapchain backbuffer texture(s) — register/GetBuffer chunks
  B. Walk their full write history; identify the LAST writers, especially
     anything in the right-half region (viewport.x ~632)
  C. For 140984 specifically: dump every writer with viewport.x to see
     the LEFT vs RIGHT split
  D. For each writer to the backbuffer that runs with right-eye viewport,
     identify its input SRV — that's the right-eye final-composite source
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "final_compositor.log")


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


def find_child(obj, name):
    try:
        for i in range(obj.NumChildren()):
            c = obj.GetChild(i)
            if str(c.name) == name:
                return c
    except Exception:
        pass
    return None


cap, controller = _lib.open_capture(CAPTURE)
try:
    textures_by_id = {res_id(t.resourceId): t for t in controller.GetTextures()}

    # =================================================================
    # A. Find swapchain backbuffer texture(s)
    # =================================================================
    log("=== A. Find swapchain backbuffer ===")
    sdfile = controller.GetStructuredFile()
    try:
        nchunks = sdfile.chunks.size()
    except Exception:
        nchunks = len(sdfile.chunks)
    backbuffer_rids = set()
    for i in range(nchunks):
        chunk = sdfile.chunks[i]
        cname = str(chunk.name)
        if "GetBuffer" in cname or "WrapSwapchainBuffer" in cname:
            # Find a ResourceId child
            for j in range(chunk.NumChildren()):
                c = chunk.GetChild(j)
                try:
                    rid_obj = c.AsResourceId()
                    rid_str = _lib.resource_id_str(rid_obj)
                    if rid_str and rid_str != "ResourceId::0":
                        backbuffer_rids.add(rid_str)
                except Exception:
                    pass
        elif "CreateSwapChain" in cname or "Swapchain" in cname:
            for j in range(chunk.NumChildren()):
                c = chunk.GetChild(j)
                try:
                    rid_obj = c.AsResourceId()
                    rid_str = _lib.resource_id_str(rid_obj)
                    if rid_str and rid_str != "ResourceId::0":
                        backbuffer_rids.add(rid_str)
                except Exception:
                    pass
    log(f"  candidate backbuffer resourceIds from swapchain chunks: {sorted(backbuffer_rids)}")

    # Also try GetBackbuffer-style heuristic: textures with no resource flags
    # but display-size dimensions and no name. Look at the resources of the
    # exact swapchain dimensions
    log("  resource dim 1264x712 candidate backbuffers (by usage pattern):")
    for rid_str, tex in textures_by_id.items():
        if tex is None: continue
        try:
            w, h = int(tex.width), int(tex.height)
        except Exception:
            continue
        if (w, h) not in [(1264, 712), (1280, 720)]:
            continue
        # Check if it's a swapchain target (typically Presentable creation flag)
        flags = str(tex.creationFlags) if hasattr(tex, "creationFlags") else ""
        if "SwapBuffer" in flags or "Presentable" in flags:
            backbuffer_rids.add(rid_str)
            log(f"    {rid_str} {w}x{h} {tex.format.Name()}  flags={flags}")

    log(f"\n  final backbuffer candidates: {sorted(backbuffer_rids)}")

    # =================================================================
    # B. Walk backbuffer write history
    # =================================================================
    log("\n=== B. Backbuffer write history ===")
    for bb_rid in sorted(backbuffer_rids):
        log(f"\n  --- {bb_rid} ---")
        target = None
        for r in controller.GetResources():
            if res_id(r.resourceId) == bb_rid:
                target = r.resourceId; break
        if target is None:
            log("    not found"); continue
        usage = controller.GetUsage(target)
        # All non-Discard non-Barrier events
        writers = []
        for u in usage:
            kind = str(u.usage).split(".")[-1]
            if "Discard" in kind or "Barrier" in kind:
                continue
            eid = int(u.eventId)
            writers.append((eid, kind))
        log(f"    {len(writers)} write/read events")
        for eid, kind in writers:
            try:
                controller.SetFrameEvent(eid, True)
                pipe = controller.GetPipelineState()
                refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                entry = str(refl.entryPoint) if refl and len(refl.rawBytes) > 0 else None
                d3d12 = controller.GetD3D12PipelineState()
                vp_x = None; vp_w = None
                if len(d3d12.rasterizer.viewports) > 0:
                    vp = d3d12.rasterizer.viewports[0]
                    vp_x = float(vp.x); vp_w = float(vp.width)
            except Exception:
                entry = None; vp_x = vp_w = None
            log(f"    eid {eid} {kind:18} PS={entry} vp_x={vp_x} vp_w={vp_w}")

    # =================================================================
    # C. 140984 per-eye writer split — full list with viewports
    # =================================================================
    log("\n\n=== C. ResourceId::140984 writers with viewport.x classification ===")
    target_rid_str = "ResourceId::140984"
    target = None
    for r in controller.GetResources():
        if res_id(r.resourceId) == target_rid_str:
            target = r.resourceId; break
    if target is None:
        log("  not found")
    else:
        usage = controller.GetUsage(target)
        # Classify each writer by viewport.x
        writers_with_vp = []
        for u in usage:
            kind = str(u.usage).split(".")[-1]
            if "Discard" in kind or "Barrier" in kind:
                continue
            if not ("RWResource" in kind or "ColorTarget" in kind or "ColourTarget" in kind):
                continue
            eid = int(u.eventId)
            try:
                controller.SetFrameEvent(eid, True)
                pipe = controller.GetPipelineState()
                refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                entry = str(refl.entryPoint) if refl and len(refl.rawBytes) > 0 else None
                d3d12 = controller.GetD3D12PipelineState()
                vp_x = None; vp_w = None
                if len(d3d12.rasterizer.viewports) > 0:
                    vp = d3d12.rasterizer.viewports[0]
                    vp_x = float(vp.x); vp_w = float(vp.width)
                # Also dump scissor
                sc_x = sc_w = None
                if len(d3d12.rasterizer.scissors) > 0:
                    sc = d3d12.rasterizer.scissors[0]
                    sc_x = int(sc.x); sc_w = int(sc.width)
                # Identify input SRV (first one)
                first_srv = None
                try:
                    arr = pipe.GetReadOnlyResources(rd.ShaderStage.Pixel, False)
                    for u2 in arr:
                        if u2.descriptor and u2.descriptor.resource:
                            first_srv = res_id(u2.descriptor.resource)
                            break
                except Exception:
                    pass
            except Exception:
                entry = None; vp_x = vp_w = None; sc_x = sc_w = None; first_srv = None
            eye = "?"
            if vp_x is not None:
                eye = "right" if vp_x >= 400 else "left"
            writers_with_vp.append({
                "eventId": eid, "usage": kind, "entry": entry,
                "vp_x": vp_x, "vp_w": vp_w, "sc_x": sc_x, "sc_w": sc_w,
                "first_srv": first_srv, "eye": eye,
            })
        log(f"  total writers: {len(writers_with_vp)}")
        # Sort by event id
        writers_with_vp.sort(key=lambda w: w["eventId"])
        from collections import Counter
        eye_counts = Counter(w["eye"] for w in writers_with_vp)
        log(f"  eye distribution: {dict(eye_counts)}")
        log(f"\n  ALL writers chronologically (eid, PS entry, vp_x, vp_w, first SRV):")
        for w in writers_with_vp:
            log(f"    eid {w['eventId']:5} {w['eye']:5} {w['entry'] or '?':25} "
                f"vp=({w['vp_x']},{w['vp_w']}) sc=({w['sc_x']},{w['sc_w']}) "
                f"t0 SRV={w['first_srv']}")

        # Identify the FINAL writer
        if writers_with_vp:
            last = writers_with_vp[-1]
            log(f"\n  ⭐ LAST writer: eid {last['eventId']} {last['entry']} "
                f"vp_x={last['vp_x']} reading SRV {last['first_srv']}")

        # Right-eye writers specifically
        right_writers = [w for w in writers_with_vp if w["eye"] == "right"]
        log(f"\n  RIGHT-eye writers to 140984 ({len(right_writers)}):")
        for w in right_writers:
            log(f"    eid {w['eventId']} {w['entry']} vp=({w['vp_x']},{w['vp_w']}) t0 SRV={w['first_srv']}")

        with open(os.path.join(OUT_DIR, "final_compositor.json"), "w") as f:
            json.dump({"backbuffer_candidates": list(backbuffer_rids),
                       "writers_140984": writers_with_vp}, f, indent=2, default=str)
        log("\nwrote final_compositor.json")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
