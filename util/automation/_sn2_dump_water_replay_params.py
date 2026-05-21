"""Dump exact replay parameters for the 14 water-material basepass draws
(events 20618-20793). The output JSON is intended to be consumed by a UEVR
mid-hook that re-executes these draws for the right eye after the natural
left-eye SLW pass runs.

Per draw, captures:
  - Pipeline: PSO ResourceId + per-stage shader bytecode hashes
  - Root signature: ResourceId + per-root-parameter values
      * descriptor tables → resolved descriptor handles + heap offsets
      * root CBVs → resource ID + offset
      * root SRVs/UAVs → resource ID + offset
      * root constants → raw bytes
  - Geometry: vertex buffer(s) + offsets + strides, index buffer + format
              + offset, primitive topology
  - Output merger: render targets (8 slots) + depth-stencil + viewport + scissor
  - Action: numIndices, numInstances, startIndex, baseVertex, etc.

This is the "if UEVR can't fix the engine, here's what to inject" payload.
"""

import json
import os
import sys
import time
import traceback

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_target"
JSON_OUT = os.path.join(OUT_DIR, "water_replay_params.json")
LOG = os.path.join(OUT_DIR, "water_replay_params.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

log("import _lib...")
from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
# The 14 water-material basepass event IDs (from right_basepass_mrt.json)
WATER_EVENT_IDS = []
mrt = json.load(open(os.path.join(OUT_DIR, "right_basepass_mrt.json")))
for b in mrt.get("rightBindsAuxRT_30085", []):
    WATER_EVENT_IDS.append(int(b["eventId"]))
log(f"loaded {len(WATER_EVENT_IDS)} water-draw event IDs from MRT analysis")


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


def collect_pipeline(d3d12):
    """Pull pipeline state info."""
    rs_id = None
    try:
        rs_id = res_id(d3d12.rootSignature.resourceId)
    except Exception:
        pass
    return {
        "pso": res_id(d3d12.pipelineResourceId),
        "rootSig": rs_id,
    }


def collect_shader_hashes(controller):
    pipe = controller.GetPipelineState()
    out = {}
    for stage, name in (
        (rd.ShaderStage.Vertex, "vs"),
        (rd.ShaderStage.Hull, "hs"),
        (rd.ShaderStage.Domain, "ds"),
        (rd.ShaderStage.Geometry, "gs"),
        (rd.ShaderStage.Pixel, "ps"),
    ):
        try:
            refl = pipe.GetShaderReflection(stage)
        except Exception:
            refl = None
        if refl is not None and len(refl.rawBytes) > 0:
            out[name] = {
                "hash": _lib.shader_bytecode_hash(bytes(refl.rawBytes)),
                "size": len(refl.rawBytes),
                "entry": str(refl.entryPoint),
                "shaderId": res_id(refl.resourceId),
            }
    return out


def collect_root_params(d3d12):
    """Dump every root parameter and its current value.

    D3D12 root parameters come in three flavours and the renderdoc schema
    union-encodes them on a single RootParam struct:
      - Root constants: ``constants`` holds the raw bytes
      - Root CBV/SRV/UAV: ``descriptor`` holds the bound resource
      - Descriptor table: ``heap`` + ``heapByteOffset`` + ``tableRanges``
        identify the table window in the bound CBV/SRV/UAV heap
    """
    out = []
    try:
        params = d3d12.rootSignature.parameters
    except Exception:
        return out
    for i, p in enumerate(params):
        entry = {
            "index": i,
            "visibility": str(p.visibility).split(".")[-1] if hasattr(p, "visibility") else None,
            "space": to_int(p.space) if hasattr(p, "space") else None,
            "register": to_int(p.reg) if hasattr(p, "reg") else None,
        }
        # Root constants: non-empty constants bytebuf
        try:
            if hasattr(p, "constants") and p.constants and len(p.constants) > 0:
                # Take up to 64 bytes
                entry["constants_bytes"] = list(bytes(p.constants))[:64]
        except Exception:
            pass
        # Root descriptor (CBV/SRV/UAV) — Descriptor struct
        try:
            d = p.descriptor
            if d is not None and getattr(d, "resource", None) is not None:
                rid = res_id(d.resource)
                if rid:
                    entry["descriptor"] = {
                        "resource": rid,
                        "byteOffset": to_int(d.byteOffset) if hasattr(d, "byteOffset") else None,
                        "byteSize": to_int(d.byteSize) if hasattr(d, "byteSize") else None,
                        "type": str(d.type).split(".")[-1] if hasattr(d, "type") else None,
                    }
        except Exception:
            pass
        # Descriptor table — heap + offset + tableRanges
        try:
            heap = p.heap if hasattr(p, "heap") else None
            heap_id = res_id(heap) if heap is not None else None
            if heap_id and heap_id != "ResourceId::0":
                entry["table"] = {
                    "heap": heap_id,
                    "heapByteOffset": to_int(p.heapByteOffset) if hasattr(p, "heapByteOffset") else None,
                }
                ranges = []
                try:
                    for tr in p.tableRanges:
                        ranges.append({
                            "category": str(tr.category).split(".")[-1] if hasattr(tr, "category") else None,
                            "register": to_int(tr.baseShaderRegister) if hasattr(tr, "baseShaderRegister") else None,
                            "space": to_int(tr.registerSpace) if hasattr(tr, "registerSpace") else None,
                            "numDescriptors": to_int(tr.numDescriptors) if hasattr(tr, "numDescriptors") else None,
                            "appendOffset": to_int(tr.appendDescriptor) if hasattr(tr, "appendDescriptor") else None,
                        })
                except Exception:
                    pass
                if ranges:
                    entry["table"]["ranges"] = ranges
        except Exception:
            pass
        out.append(entry)
    return out


def collect_geometry(d3d12):
    """Vertex/index buffers + topology."""
    ia = d3d12.inputAssembly
    out = {
        "topology": str(ia.topology).split(".")[-1] if hasattr(ia, "topology") else None,
        "indexBuffer": {
            "resource": res_id(ia.indexBuffer.resourceId) if hasattr(ia.indexBuffer, "resourceId") else None,
            "offset": to_int(ia.indexBuffer.byteOffset) if hasattr(ia.indexBuffer, "byteOffset") else None,
            "byteStride": to_int(ia.indexBuffer.byteStride) if hasattr(ia.indexBuffer, "byteStride") else None,
        },
        "vertexBuffers": [],
    }
    try:
        for vb in ia.vertexBuffers:
            out["vertexBuffers"].append({
                "resource": res_id(vb.resourceId),
                "offset": to_int(vb.byteOffset),
                "byteStride": to_int(vb.byteStride),
                "byteSize": to_int(vb.byteSize) if hasattr(vb, "byteSize") else None,
            })
    except Exception:
        pass
    return out


def collect_om(d3d12):
    """Output merger: render targets + depth-stencil."""
    om = d3d12.outputMerger
    out = {"renderTargets": [], "depthStencil": None,
           "viewports": [], "scissors": []}
    try:
        for i, rt in enumerate(om.renderTargets):
            r = res_id(rt.resource)
            if r is None:
                continue
            out["renderTargets"].append({
                "slot": i, "resource": r,
                "firstMip": to_int(rt.firstMip),
                "firstSlice": to_int(rt.firstSlice),
                "numSlices": to_int(rt.numSlices),
                "format": str(rt.viewFormat.Name()) if hasattr(rt, "viewFormat") else None,
            })
    except Exception:
        pass
    try:
        ds = om.depthStencilTarget if hasattr(om, "depthStencilTarget") else om.depthStencil
        if ds is not None and res_id(ds.resource) is not None:
            out["depthStencil"] = {
                "resource": res_id(ds.resource),
                "firstMip": to_int(ds.firstMip),
                "firstSlice": to_int(ds.firstSlice),
                "format": str(ds.viewFormat.Name()) if hasattr(ds, "viewFormat") else None,
            }
    except Exception:
        pass
    try:
        for v in d3d12.rasterizer.viewports:
            out["viewports"].append({
                "x": float(v.x), "y": float(v.y),
                "width": float(v.width), "height": float(v.height),
                "minDepth": float(v.minDepth), "maxDepth": float(v.maxDepth),
            })
    except Exception:
        pass
    try:
        for s in d3d12.rasterizer.scissors:
            out["scissors"].append({
                "x": to_int(s.x), "y": to_int(s.y),
                "width": to_int(s.width), "height": to_int(s.height),
            })
    except Exception:
        pass
    return out


def collect_action(action):
    return {
        "name": str(action.GetName(action.parent.parent.GetStructuredFile()))
                if hasattr(action, "parent") else None,
        "numIndices": to_int(action.numIndices),
        "numInstances": to_int(action.numInstances),
        "indexOffset": to_int(action.indexOffset) if hasattr(action, "indexOffset") else None,
        "baseVertex": to_int(action.baseVertex) if hasattr(action, "baseVertex") else None,
        "instanceOffset": to_int(action.instanceOffset) if hasattr(action, "instanceOffset") else None,
        "flags": _lib.action_flag_names(int(action.flags)) if hasattr(_lib, "action_flag_names") else None,
    }


log("open_capture...")
cap, controller = _lib.open_capture(CAPTURE)
try:
    # Build action lookup
    action_by_eid = {int(a.eventId): a for a in _lib.walk_actions(controller)}

    draws = []
    for eid in WATER_EVENT_IDS:
        log(f"  eid {eid}...")
        try:
            controller.SetFrameEvent(eid, True)
            d3d12 = controller.GetD3D12PipelineState()
            entry = {
                "eventId": eid,
                "pipeline": collect_pipeline(d3d12),
                "shaders": collect_shader_hashes(controller),
                "rootParams": collect_root_params(d3d12),
                "geometry": collect_geometry(d3d12),
                "outputMerger": collect_om(d3d12),
            }
            a = action_by_eid.get(eid)
            if a is not None:
                try:
                    entry["action"] = {
                        "numIndices": to_int(a.numIndices),
                        "numInstances": to_int(a.numInstances),
                        "indexOffset": to_int(a.indexOffset) if hasattr(a, "indexOffset") else None,
                        "baseVertex": to_int(a.baseVertex) if hasattr(a, "baseVertex") else None,
                        "instanceOffset": to_int(a.instanceOffset) if hasattr(a, "instanceOffset") else None,
                    }
                except Exception:
                    entry["action"] = {"error": traceback.format_exc()}
            draws.append(entry)
        except Exception:
            draws.append({"eventId": eid, "error": traceback.format_exc()})

    out = {
        "capture": CAPTURE,
        "drawCount": len(draws),
        "draws": draws,
    }
    with open(JSON_OUT, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)
    log(f"\nwrote {JSON_OUT}")
    log(f"  {len(draws)} draws with full replay params")

    # Quick summary — group by PS hash + RTV layout
    from collections import defaultdict
    by_ps = defaultdict(list)
    for d in draws:
        if "error" in d:
            continue
        psh = (d.get("shaders") or {}).get("ps", {}).get("hash", "?")
        by_ps[psh].append(d["eventId"])
    log("\nGrouped by PS shader hash:")
    for h, eids in by_ps.items():
        log(f"  {h[:16]}.. : {len(eids)} draws → events {eids}")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
