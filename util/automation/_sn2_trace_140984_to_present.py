"""Trace 140984 → final present surface.

Goals:
  1. Full read/write history for 140984
  2. Identify the swapchain backbuffer in this capture
  3. Walk backward from backbuffer's last write to find the source chain
  4. Identify any CopyResource / CopyTextureRegion / Present-style chunks
     that move pixels between 140984 and the backbuffer
  5. Surface ALL late-frame (eid > 16000) ColorTarget writes to find
     the actual present-feeding RT(s)
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "trace_140984.log")


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
    # 1. 140984 read/write history
    # =================================================================
    log("=== 1. ResourceId::140984 lineage ===")
    target_rid_str = "ResourceId::140984"
    target = None
    for r in controller.GetResources():
        if res_id(r.resourceId) == target_rid_str:
            target = r.resourceId; break
    if target is None:
        log("  not found")
    else:
        usage = controller.GetUsage(target)
        log(f"  total usages: {len(usage)}")
        writers = []
        readers = []
        copy_srcs = []
        copy_dsts = []
        for u in usage:
            kind = str(u.usage).split(".")[-1]
            eid = int(u.eventId)
            if "Discard" in kind or "Barrier" in kind:
                continue
            if "CopySrc" in kind:
                copy_srcs.append((eid, kind))
            elif "CopyDst" in kind:
                copy_dsts.append((eid, kind))
            elif "RWResource" in kind or "ColorTarget" in kind or "ColourTarget" in kind:
                writers.append((eid, kind))
            elif "Resource" in kind:
                readers.append((eid, kind))
        log(f"  writers: {len(writers)}, readers: {len(readers)}, copy_src: {len(copy_srcs)}, copy_dst: {len(copy_dsts)}")
        log("  All WRITERS:")
        for eid, kind in writers:
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
        log("  All READERS:")
        for eid, kind in readers:
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
        log("  CopyDsts (something COPIES TO 140984):")
        for eid, kind in copy_dsts:
            log(f"    eid {eid} {kind}")
        log("  CopySrcs (140984 IS copied FROM):")
        for eid, kind in copy_srcs:
            log(f"    eid {eid} {kind}")

    # =================================================================
    # 2. Identify backbuffer / present resources
    # =================================================================
    log("\n\n=== 2. Identify present / backbuffer ===")
    sdfile = controller.GetStructuredFile()
    try:
        nchunks = sdfile.chunks.size()
    except Exception:
        nchunks = len(sdfile.chunks)
    log(f"  scanning {nchunks} chunks for Present/CopyResource chunks...")

    present_chunks = []
    copy_chunks = []
    for i in range(nchunks):
        chunk = sdfile.chunks[i]
        cname = str(chunk.name)
        # Find Present chunks
        if "Present" in cname:
            try:
                eid = int(chunk.metadata.eventId)
            except Exception:
                eid = None
            present_chunks.append((i, cname, eid))
        # Find CopyResource / CopyTextureRegion
        if "CopyResource" in cname or "CopyTextureRegion" in cname:
            try:
                eid = int(chunk.metadata.eventId)
            except Exception:
                eid = None
            # Extract src/dst resources
            src = None; dst = None
            for j in range(chunk.NumChildren()):
                c = chunk.GetChild(j)
                cn = str(c.name)
                # Heuristic: first 2 ResourceId children are pDstResource then pSrcResource
                if "Dst" in cn or "dst" in cn:
                    try:
                        dst = _lib.resource_id_str(c.AsResourceId())
                    except Exception:
                        pass
                elif "Src" in cn or "src" in cn:
                    try:
                        src = _lib.resource_id_str(c.AsResourceId())
                    except Exception:
                        pass
            copy_chunks.append((i, cname, eid, src, dst))
    log(f"  found {len(present_chunks)} Present chunks, {len(copy_chunks)} Copy chunks")
    log("  Present chunks (first 5):")
    for i, cname, eid in present_chunks[:5]:
        log(f"    chunk {i} eid={eid}: {cname}")

    # Late-frame CopyResource (eid > 14000 probably, or chunkIndex > 80%)
    log("  Late-frame copy chunks (eid > 14000):")
    for i, cname, eid, src, dst in copy_chunks:
        if eid is None or eid < 14000:
            continue
        log(f"    chunk {i} eid={eid}: {cname}  src={src} dst={dst}")

    # =================================================================
    # 3. Find the LAST writer in the frame (latest event with ColorTarget or
    #    CopyDst) to any 1264x712 or 1280x720 R10G10B10A2/R8G8B8A8 texture
    # =================================================================
    log("\n\n=== 3. Last-write analysis on display-format textures ===")
    display_targets = {}   # rid -> (last_eid, format, dim)
    for rid_str, tex in textures_by_id.items():
        if tex is None: continue
        try:
            w, h = int(tex.width), int(tex.height)
        except Exception:
            continue
        # Only ~720p or 4K-ish 2D textures that could be presented
        if not ((1264 <= w <= 1280 and 712 <= h <= 720) or
                (1280 == w and 720 == h)):
            continue
        fmt = str(tex.format.Name())
        if not ("R10G10B10A2" in fmt or "R8G8B8A8" in fmt or "B8G8R8A8" in fmt):
            continue
        # Find latest writer
        usage = controller.GetUsage(tex.resourceId)
        latest = -1
        last_kind = None
        for u in usage:
            kind = str(u.usage).split(".")[-1]
            if "Discard" in kind or "Barrier" in kind:
                continue
            if "Resource" in kind and "RWResource" not in kind and "CopyDst" not in kind \
               and "ColorTarget" not in kind:
                continue  # skip pure reads
            eid = int(u.eventId)
            if eid > latest:
                latest = eid
                last_kind = kind
        display_targets[rid_str] = {"format": fmt, "w": w, "h": h,
                                      "last_event": latest, "last_kind": last_kind}
    log(f"  {len(display_targets)} display-shaped textures found")
    # Sort by last_event descending
    sorted_disp = sorted(display_targets.items(), key=lambda kv: -(kv[1]["last_event"] or 0))
    log("  Display-shaped textures by latest-write descending (top 15):")
    for rid, info in sorted_disp[:15]:
        log(f"    {rid} {info['w']}x{info['h']} {info['format']}: last write eid {info['last_event']} ({info['last_kind']})")

    with open(os.path.join(OUT_DIR, "trace_140984.json"), "w") as f:
        json.dump({"display_targets": display_targets,
                   "present_chunks": present_chunks,
                   "copy_chunks_late": [c for c in copy_chunks
                                          if c[2] is not None and c[2] > 14000]},
                  f, indent=2, default=str)
    log("\nwrote trace_140984.json")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
