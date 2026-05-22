"""Quick verification — are ResourceId::141259 and 141279 (the
basepass t1/t2 53x30x48 R11G11B10F volumetric fog volumes) written
LEFT-only?

If yes → that's the bug surface (right eye samples LEFT-projected fog).
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
LOG = os.path.join(OUT_DIR, "verify_volfog_writers.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"

TARGETS = [
    "ResourceId::141259",  # basepass PS t1
    "ResourceId::141279",  # basepass PS t2
]


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


cap, controller = _lib.open_capture(CAPTURE)
try:
    textures_by_id = {res_id(t.resourceId): t for t in controller.GetTextures()}

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
            if "RWResource" in kind or "ColorTarget" in kind or "ColourTarget" in kind \
               or "CopyDst" in kind:
                writers.append((eid, kind))
            elif "Resource" in kind:
                readers.append((eid, kind))
        log(f"  {len(writers)} writers, {len(readers)} readers")

        # Classify each writer by eye via View CB offset (more reliable than viewport)
        LEFT_VIEW_OFF = 3166208  # may differ in live capture — also use 0
        for eid, kind in writers:
            try:
                controller.SetFrameEvent(eid, True)
                d3d12 = controller.GetD3D12PipelineState()
                pipe = controller.GetPipelineState()
                # CS shader
                cs_refl = None
                try:
                    cs_refl = pipe.GetShaderReflection(rd.ShaderStage.Compute)
                except Exception:
                    pass
                cs_entry = str(cs_refl.entryPoint) if cs_refl and len(cs_refl.rawBytes) > 0 else None
                # PS shader
                ps_refl = None
                try:
                    ps_refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
                except Exception:
                    pass
                ps_entry = str(ps_refl.entryPoint) if ps_refl and len(ps_refl.rawBytes) > 0 else None
                # Viewport
                vp_x = vp_w = None
                if len(d3d12.rasterizer.viewports) > 0:
                    vp = d3d12.rasterizer.viewports[0]
                    vp_x = float(vp.x); vp_w = float(vp.width)
                # View CB offset (find a CBV with byteSize ~10076)
                view_off = None
                try:
                    for p in d3d12.rootSignature.parameters:
                        d = p.descriptor
                        if d is None or getattr(d, "resource", None) is None:
                            continue
                        # Try by visibility + register
                        vis = str(p.visibility).split(".")[-1] if hasattr(p, "visibility") else None
                        reg = int(p.reg) if hasattr(p, "reg") else None
                        if reg == 1 and vis in ("Compute", "Pixel", "All"):
                            view_off = int(d.byteOffset)
                            break
                except Exception:
                    pass
                # Dispatch dims if CS
                dispatch = None
                for a in _lib.walk_actions(controller):
                    if int(a.eventId) == eid:
                        try:
                            dispatch = [int(v) for v in a.dispatchDimension]
                        except Exception:
                            pass
                        try:
                            num_idx = int(a.numIndices)
                        except Exception:
                            num_idx = None
                        break
            except Exception as e:
                cs_entry = ps_entry = None; vp_x = vp_w = None; view_off = None; dispatch = None
            log(f"    eid {eid:5} {kind:18} CS={cs_entry} PS={ps_entry} vp_x={vp_x} dispatch={dispatch} viewCB_off={view_off}")

    log("\nDONE")
finally:
    controller.Shutdown()
    cap.Shutdown()

os._exit(0)
