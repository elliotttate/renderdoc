"""Dump items 1, 2, 4, 5 of the UWE fog dispatch investigation from the
existing sn2_nouevr capture.

Items:
  1. Compute root signature layout for each fog CS dispatch
  2. Per-dispatch resource bindings (CBV / SRV / UAV / sampler table)
  4. Resource write history for each UAV (excluding Discard/Barrier)
  5. DXIL bytecode dumped to disk for each CS

Output: phase1_fog_spec.json + per-shader .dxbc binary blobs.
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_phase1_spec"
os.makedirs(OUT_DIR, exist_ok=True)
SHADER_DIR = os.path.join(OUT_DIR, "fog_shaders")
os.makedirs(SHADER_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "fog_dispatch.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"

# UWE fog CS events identified earlier
TARGET_EVENTS = [
    (21348, "UWEFogReconstructCS"),
    (21370, "UWEFogDenoiseCS_read1"),
    (21377, "UWEFogDenoiseCS_write"),
    (21390, "UWEFogResolveCS"),
]


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


def dump_root_sig(d3d12):
    """Dump compute root sig with full per-parameter type + binding info."""
    out = {"resourceId": None, "parameters": []}
    try:
        out["resourceId"] = res_id(d3d12.rootSignature.resourceId)
        for i, p in enumerate(d3d12.rootSignature.parameters):
            entry = {"index": i}
            try:
                entry["visibility"] = str(p.visibility).split(".")[-1]
            except Exception:
                pass
            try:
                entry["space"] = to_int(p.space)
                entry["shaderRegister"] = to_int(p.reg)
            except Exception:
                pass
            # Detect kind: descriptor (CBV/SRV/UAV), table, or root constants
            has_descriptor = False
            has_table = False
            has_constants = False
            try:
                d = p.descriptor
                if d is not None and getattr(d, "resource", None) is not None:
                    rid = res_id(d.resource)
                    if rid and rid != "ResourceId::0":
                        has_descriptor = True
                        entry["kind"] = "ROOT_DESCRIPTOR"
                        entry["descriptor"] = {
                            "type": str(d.type).split(".")[-1] if hasattr(d, "type") else None,
                            "resource": rid,
                            "byteOffset": to_int(d.byteOffset),
                            "byteSize": to_int(d.byteSize),
                        }
            except Exception:
                pass
            try:
                if hasattr(p, "constants") and p.constants and len(p.constants) > 0:
                    has_constants = True
                    entry["kind"] = "32BIT_CONSTANTS"
                    entry["constants_bytes"] = list(bytes(p.constants))[:64]
            except Exception:
                pass
            try:
                heap = res_id(p.heap)
                if heap and heap != "ResourceId::0":
                    has_table = True
                    entry["kind"] = "DESCRIPTOR_TABLE"
                    entry["table"] = {
                        "heap": heap,
                        "heapByteOffset": to_int(p.heapByteOffset),
                    }
                    ranges = []
                    try:
                        for r in p.tableRanges:
                            ranges.append({
                                "category": str(r.category).split(".")[-1] if hasattr(r, "category") else None,
                                "baseShaderRegister": to_int(r.baseShaderRegister) if hasattr(r, "baseShaderRegister") else None,
                                "registerSpace": to_int(r.registerSpace) if hasattr(r, "registerSpace") else None,
                                "numDescriptors": to_int(r.numDescriptors) if hasattr(r, "numDescriptors") else None,
                                "appendDescriptor": to_int(r.appendDescriptor) if hasattr(r, "appendDescriptor") else None,
                            })
                    except Exception:
                        pass
                    if ranges:
                        entry["table"]["ranges"] = ranges
            except Exception:
                pass
            if not (has_descriptor or has_table or has_constants):
                entry["kind"] = "EMPTY_OR_UNBOUND"
            out["parameters"].append(entry)
    except Exception as e:
        out["error"] = str(e)
    return out


def dump_bindings(controller):
    """Dump all CS bindings (CBV root descriptors and per-table-slot CBV/SRV/UAV)."""
    pipe = controller.GetPipelineState()
    out = {"cbvs": [], "srvs": [], "uavs": [], "samplers": []}
    for kind, getter, key in (
        ("cbvs", pipe.GetConstantBlocks, "cbvs"),
        ("srvs", pipe.GetReadOnlyResources, "srvs"),
        ("uavs", pipe.GetReadWriteResources, "uavs"),
        ("samplers", pipe.GetSamplers, "samplers"),
    ):
        try:
            arr = getter(rd.ShaderStage.Compute, False)
        except Exception:
            arr = []
        for u in arr:
            desc = u.descriptor
            if desc is None or desc.resource is None:
                continue
            out[key].append({
                "register": to_int(u.access.index),
                "resource": res_id(desc.resource),
                "descriptorStore": res_id(u.access.descriptorStore),
                "descriptorStoreOffset": to_int(u.access.byteOffset),
                "type": str(u.access.type).split(".")[-1] if hasattr(u.access, "type") else None,
            })
    return out


def get_resource_desc(controller, rid_str):
    if not rid_str:
        return None
    for t in controller.GetTextures():
        if res_id(t.resourceId) == rid_str:
            return {
                "kind": "Texture",
                "type": str(t.type).split(".")[-1] if hasattr(t, "type") else None,
                "width": int(t.width), "height": int(t.height), "depth": int(t.depth),
                "arraysize": int(t.arraysize), "mips": int(t.mips),
                "format": str(t.format.Name()),
                "creationFlags": str(t.creationFlags).split(".")[-1] if hasattr(t, "creationFlags") else None,
            }
    for b in controller.GetBuffers():
        if res_id(b.resourceId) == rid_str:
            return {"kind": "Buffer", "length": int(b.length),
                    "creationFlags": str(b.creationFlags).split(".")[-1] if hasattr(b, "creationFlags") else None}
    return None


def real_writers(controller, rid_str):
    target = None
    for r in controller.GetResources():
        if res_id(r.resourceId) == rid_str:
            target = r.resourceId; break
    if target is None:
        return []
    out = []
    for u in controller.GetUsage(target):
        kind = str(u.usage).split(".")[-1]
        if "Discard" in kind or "Barrier" in kind:
            continue
        if "RWResource" in kind or "ColorTarget" in kind or "ColourTarget" in kind or "CopyDst" in kind:
            out.append({"eventId": int(u.eventId), "usage": kind})
    return out


def writer_classifier(controller, writer_eid):
    """Quick eye classification of a writer event via View cbuffer offset."""
    LEFT = 3166208; RIGHT = 3155968
    try:
        controller.SetFrameEvent(int(writer_eid), True)
        d3d12 = controller.GetD3D12PipelineState()
        for p in d3d12.rootSignature.parameters:
            try:
                d = p.descriptor
            except Exception:
                continue
            if d is None or getattr(d, "resource", None) is None:
                continue
            if res_id(d.resource) == "ResourceId::29343":
                off = int(d.byteOffset)
                if off == LEFT: return "left"
                if off == RIGHT: return "right"
                return f"other_{off}"
    except Exception:
        pass
    return "unknown"


cap, controller = _lib.open_capture(CAPTURE)
try:
    spec = {"events": [], "uavs": {}}

    # =================================================================
    # Pass 1 — for each fog CS event, dump root sig + bindings + DXIL
    # =================================================================
    log("=== Per-event root sig + bindings + DXIL ===")
    uav_resources_seen = set()
    for eid, label in TARGET_EVENTS:
        log(f"\n  --- event {eid} ({label}) ---")
        try:
            controller.SetFrameEvent(eid, True)
            d3d12 = controller.GetD3D12PipelineState()
            pipe = controller.GetPipelineState()

            # CS shader reflection
            cs_info = {}
            try:
                refl = pipe.GetShaderReflection(rd.ShaderStage.Compute)
            except Exception:
                refl = None
            if refl and len(refl.rawBytes) > 0:
                raw = bytes(refl.rawBytes)
                h = _lib.shader_bytecode_hash(raw)
                cs_info = {
                    "hash": h,
                    "entry": str(refl.entryPoint),
                    "bytecodeSize": len(raw),
                    "resourceId": res_id(refl.resourceId),
                }
                # Save bytecode to disk for offline DXIL analysis
                fname = os.path.join(SHADER_DIR, f"{label}_{h[:16]}.dxbc")
                with open(fname, "wb") as f:
                    f.write(raw)
                cs_info["dxbcPath"] = fname
                log(f"    shader: {refl.entryPoint}  hash={h[:16]}  size={len(raw)}  saved={fname}")

            # Action info
            action_info = {}
            for a in _lib.walk_actions(controller):
                if int(a.eventId) == eid:
                    try:
                        action_info["dispatchDimension"] = [int(v) for v in a.dispatchDimension]
                    except Exception: pass
                    try:
                        action_info["dispatchThreadsDimension"] = [int(v) for v in a.dispatchThreadsDimension]
                    except Exception: pass
                    sdfile = controller.GetStructuredFile()
                    action_info["name"] = str(a.GetName(sdfile))
                    break
            log(f"    action: {action_info}")

            # Root sig
            rs = dump_root_sig(d3d12)
            log(f"    rootSig: {rs['resourceId']}  ({len(rs['parameters'])} params)")
            for p in rs["parameters"]:
                kind = p.get("kind", "?")
                if kind == "ROOT_DESCRIPTOR":
                    desc = p.get("descriptor", {})
                    log(f"      [{p['index']}] {kind}  type={desc.get('type')} "
                        f"reg={p.get('shaderRegister')} space={p.get('space')} "
                        f"-> {desc.get('resource')} +{desc.get('byteOffset')} size={desc.get('byteSize')}")
                elif kind == "DESCRIPTOR_TABLE":
                    t = p.get("table", {})
                    ranges = t.get("ranges", [])
                    range_summary = ', '.join(
                        f"{r.get('category')}@{r.get('baseShaderRegister')},s{r.get('registerSpace')}×{r.get('numDescriptors')}"
                        for r in ranges[:5]
                    )
                    log(f"      [{p['index']}] {kind}  heap={t.get('heap')}+{t.get('heapByteOffset')} ranges={range_summary}")
                else:
                    log(f"      [{p['index']}] {kind} reg={p.get('shaderRegister')} space={p.get('space')}")

            # Bindings (resolved via descriptor stores — what the shader sees)
            binds = dump_bindings(controller)
            log(f"    bindings: CBVs={len(binds['cbvs'])} SRVs={len(binds['srvs'])} UAVs={len(binds['uavs'])}")
            for u in binds["uavs"]:
                rid = u["resource"]
                desc = get_resource_desc(controller, rid)
                d_s = f"{desc.get('kind')} {desc.get('width')}x{desc.get('height')}x{desc.get('depth')} {desc.get('format')}" if desc else "?"
                log(f"      UAV u{u['register']} = {rid}  [{d_s}]  store={u['descriptorStore']}+{u['descriptorStoreOffset']}")
                if rid:
                    uav_resources_seen.add(rid)
            for s in binds["srvs"][:8]:
                rid = s["resource"]
                desc = get_resource_desc(controller, rid)
                d_s = f"{desc.get('kind')} {desc.get('width')}x{desc.get('height')}x{desc.get('depth')} {desc.get('format')}" if desc else "?"
                log(f"      SRV t{s['register']} = {rid}  [{d_s}]")

            spec["events"].append({
                "eventId": eid, "label": label, "shader": cs_info,
                "action": action_info, "rootSig": rs, "bindings": binds,
            })
        except Exception as e:
            log(f"    ERROR: {e}")
            spec["events"].append({"eventId": eid, "label": label, "error": str(e)})

    # =================================================================
    # Pass 4 — write history for each UAV resource found
    # =================================================================
    log(f"\n=== UAV write history ({len(uav_resources_seen)} UAVs) ===")
    for rid in sorted(uav_resources_seen):
        log(f"\n  --- {rid} ---")
        desc = get_resource_desc(controller, rid)
        log(f"    desc: {desc}")
        writers = real_writers(controller, rid)
        log(f"    real writers (excl Discard/Barrier): {len(writers)}")
        # Classify each writer by eye
        for w in writers[:30]:
            eye = writer_classifier(controller, w["eventId"])
            controller.SetFrameEvent(w["eventId"], True)
            pipe = controller.GetPipelineState()
            try:
                refl = pipe.GetShaderReflection(rd.ShaderStage.Compute)
            except Exception:
                refl = None
            entry = str(refl.entryPoint) if (refl and len(refl.rawBytes) > 0) else None
            log(f"      eid {w['eventId']} usage={w['usage']:20} eye={eye:8} entry={entry}")
            w["eye"] = eye; w["entry"] = entry
        spec["uavs"][rid] = {"desc": desc, "writers": writers}

    out_json = os.path.join(OUT_DIR, "phase1_fog_spec.json")
    with open(out_json, "w") as f:
        json.dump(spec, f, indent=2, default=str)
    log(f"\nwrote {out_json}")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
