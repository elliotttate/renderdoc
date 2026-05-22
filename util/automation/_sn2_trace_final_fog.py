"""Trace the final fog volume that basepass samples at PS t8/t9.

The goal: produce the exact UEVR injection spec for the CreateCommittedResource
hook approach. For one matched L/R water basepass pair and one opaque basepass
pair, find:

  1. The ResourceIds bound at PS t8 and t9 (VolumeLightingA / VolumeLightingB
     per pso3069 pseudocode) — these are the final fog volumes the basepass
     consumes.
  2. For each, the D3D12 resource description (format, w/h/d, flags) — UEVR
     uses this to shadow-allocate matching resources.
  3. The CS that wrote each (single writer, since these are shared
     single-instance allocations).
  4. The writer's full state (root params, dispatch dim, UAV slot) so UEVR
     can duplicate-and-redirect the dispatch.
  5. The descriptor table slot on the right-eye basepass that holds the
     t8/t9 binding (so UEVR knows what descriptor entry to swap).

Output: phase1_spec.json — the complete UEVR injection spec.
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_phase1_spec"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"

# Known L/R basepass pairs from earlier work
PAIRS = [
    ("water_basepass",         20618, 20623),  # 2 of the 14 water draws
    ("opaque_basepass",        11870, 13157),  # known matched opaque pair
    ("SLW_composite",          20876, 20954),  # SLW composite L/R (already runs both eyes)
    ("sky_atmosphere",         20488, 20495),  # sky atmosphere L/R
    ("compose_volumetric",     20554, 20560),  # compose volumetric L/R
]


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


def collect_ps_srv_bindings(controller, eid):
    """All PS read-only resource bindings (SRVs)."""
    controller.SetFrameEvent(int(eid), True)
    pipe = controller.GetPipelineState()
    binds = []
    try:
        for u in pipe.GetReadOnlyResources(rd.ShaderStage.Pixel, False):
            d = u.descriptor
            if d is None or d.resource is None:
                continue
            binds.append({
                "register": int(u.access.index),
                "resource": res_id(d.resource),
                "descStoreHeap": res_id(u.access.descriptorStore),
                "descStoreOffset": int(u.access.byteOffset),
            })
    except Exception:
        pass
    return binds


def get_resource_desc(controller, resource_id_str):
    """D3D12_RESOURCE_DESC equivalent for UEVR shadow allocation."""
    for t in controller.GetTextures():
        if res_id(t.resourceId) == resource_id_str:
            return {
                "kind": "Texture",
                "dimension": str(t.dimension).split(".")[-1] if hasattr(t, "dimension") else None,
                "type": str(t.type).split(".")[-1] if hasattr(t, "type") else None,
                "width": int(t.width),
                "height": int(t.height),
                "depth": int(t.depth),
                "arraysize": int(t.arraysize),
                "mips": int(t.mips),
                "samples": int(t.msSamp) if hasattr(t, "msSamp") else 1,
                "format": str(t.format.Name()),
                "creationFlags": str(t.creationFlags).split(".")[-1] if hasattr(t, "creationFlags") else None,
            }
    for b in controller.GetBuffers():
        if res_id(b.resourceId) == resource_id_str:
            return {
                "kind": "Buffer",
                "length": int(b.length),
                "creationFlags": str(b.creationFlags).split(".")[-1] if hasattr(b, "creationFlags") else None,
            }
    return None


def find_writers_filtered(controller, target_rid):
    """Find all writer events (excluding Discard/Barrier lifecycle markers)."""
    target = None
    for r in controller.GetResources():
        if res_id(r.resourceId) == target_rid:
            target = r.resourceId; break
    if target is None:
        return []
    usage = controller.GetUsage(target)
    real_writers = []
    for u in usage:
        kind = str(u.usage).split(".")[-1]
        if "Discard" in kind or "Barrier" in kind:
            continue
        if "RWResource" in kind or "ColorTarget" in kind or "ColourTarget" in kind \
           or "CopyDst" in kind:
            real_writers.append({"eventId": int(u.eventId), "usage": kind})
    return real_writers


def collect_writer_state(controller, eid):
    """Full state at a writer event."""
    controller.SetFrameEvent(int(eid), True)
    d3d12 = controller.GetD3D12PipelineState()
    pipe = controller.GetPipelineState()
    out = {"eventId": eid}
    # PSO + root sig
    try:
        out["pso"] = res_id(d3d12.pipelineResourceId)
        out["rootSig"] = res_id(d3d12.rootSignature.resourceId)
    except Exception:
        pass
    # Compute or pixel shader hash
    for stage_name, stage_enum in (("compute", rd.ShaderStage.Compute),
                                    ("pixel",   rd.ShaderStage.Pixel)):
        try:
            refl = pipe.GetShaderReflection(stage_enum)
        except Exception:
            refl = None
        if refl and len(refl.rawBytes) > 0:
            out[f"{stage_name}_shader"] = {
                "hash": _lib.shader_bytecode_hash(bytes(refl.rawBytes)),
                "entry": str(refl.entryPoint),
                "size": len(refl.rawBytes),
                "resourceId": res_id(refl.resourceId),
            }
    # Dispatch dim if compute
    for a in _lib.walk_actions(controller):
        if int(a.eventId) == eid:
            try:
                out["dispatchDimension"] = [int(v) for v in a.dispatchDimension]
            except Exception:
                pass
            try:
                out["numIndices"] = int(a.numIndices)
            except Exception:
                pass
            break
    # UAV bindings on compute side
    uavs = []
    try:
        for u in pipe.GetReadWriteResources(rd.ShaderStage.Compute, False):
            d = u.descriptor
            if d is None or d.resource is None:
                continue
            uavs.append({
                "register": int(u.access.index),
                "resource": res_id(d.resource),
            })
    except Exception:
        pass
    out["uavs"] = uavs
    # Root params
    root_params = []
    try:
        for i, p in enumerate(d3d12.rootSignature.parameters):
            entry = {"index": i}
            try:
                entry["visibility"] = str(p.visibility).split(".")[-1]
                entry["register"] = int(p.reg)
                entry["space"] = int(p.space)
            except Exception:
                pass
            try:
                d = p.descriptor
                if d is not None and getattr(d, "resource", None) is not None:
                    entry["descriptor"] = {
                        "resource": res_id(d.resource),
                        "byteOffset": int(d.byteOffset),
                        "byteSize": int(d.byteSize),
                        "type": str(d.type).split(".")[-1] if hasattr(d, "type") else None,
                    }
            except Exception:
                pass
            try:
                heap = res_id(p.heap)
                if heap and heap != "ResourceId::0":
                    entry["table"] = {
                        "heap": heap,
                        "heapByteOffset": int(p.heapByteOffset),
                    }
            except Exception:
                pass
            root_params.append(entry)
    except Exception:
        pass
    out["rootParams"] = root_params
    return out


cap, controller = _lib.open_capture(CAPTURE)
try:
    spec = {"pairs": {}, "candidateVolumes": {}}

    # =================================================================
    # PASS 1 — for each pair, dump PS SRV bindings
    # =================================================================
    log("=== PASS 1: PS SRV bindings for each pair ===")
    candidate_volume_ids = set()
    for label, L, R in PAIRS:
        log(f"\n  --- {label}: L={L} R={R} ---")
        l_binds = collect_ps_srv_bindings(controller, L)
        r_binds = collect_ps_srv_bindings(controller, R)
        # Compare per-register
        l_by_reg = {b["register"]: b for b in l_binds}
        r_by_reg = {b["register"]: b for b in r_binds}
        regs = sorted(set(l_by_reg.keys()) | set(r_by_reg.keys()))
        shared_t8_t9 = []
        for reg in regs:
            lb = l_by_reg.get(reg); rb = r_by_reg.get(reg)
            if lb and rb and lb["resource"] == rb["resource"]:
                if reg in (5, 6, 7, 8, 9, 14):
                    log(f"    t{reg}: SHARED = {lb['resource']}")
                if reg in (8, 9):  # the fog volume slots
                    shared_t8_t9.append({"register": reg, "resource": lb["resource"]})
                    candidate_volume_ids.add(lb["resource"])
            elif lb or rb:
                ll = lb["resource"] if lb else None
                rr = rb["resource"] if rb else None
                if reg in (5, 6, 7, 8, 9, 14):
                    log(f"    t{reg}: DIFFER L={ll} R={rr}")
        spec["pairs"][label] = {
            "L_event": L, "R_event": R,
            "L_bindings": l_binds,
            "R_bindings": r_binds,
            "shared_t8_t9": shared_t8_t9,
        }

    # =================================================================
    # PASS 2 — for each candidate fog volume, get desc + writers
    # =================================================================
    log(f"\n=== PASS 2: candidate fog volume analysis ({len(candidate_volume_ids)} unique) ===")
    for vol in sorted(candidate_volume_ids):
        log(f"\n  --- {vol} ---")
        desc = get_resource_desc(controller, vol)
        log(f"    desc: {desc}")
        writers = find_writers_filtered(controller, vol)
        log(f"    real writers (excl Discard/Barrier): {len(writers)}")
        for w in writers[:10]:
            log(f"      eid {w['eventId']} usage={w['usage']}")
        # If a single writer, collect its full state
        unique_writer_events = sorted(set(w["eventId"] for w in writers))
        writer_states = []
        for weid in unique_writer_events[:3]:
            log(f"    inspecting writer eid {weid}...")
            ws = collect_writer_state(controller, weid)
            writer_states.append(ws)
            shader = ws.get("compute_shader") or ws.get("pixel_shader") or {}
            log(f"      shader: {shader.get('entry')} hash={shader.get('hash', '')[:16]}  "
                f"dispatch={ws.get('dispatchDimension')}")
        spec["candidateVolumes"][vol] = {
            "desc": desc,
            "realWriters": writers,
            "writerEvents": unique_writer_events,
            "writerStates": writer_states,
        }

    # =================================================================
    # Save spec
    # =================================================================
    OUT_JSON = os.path.join(OUT_DIR, "phase1_spec.json")
    with open(OUT_JSON, "w") as f:
        json.dump(spec, f, indent=2, default=str)
    log(f"\nwrote {OUT_JSON}")

    # =================================================================
    # Concise summary
    # =================================================================
    log("\n=== CONCISE SUMMARY (UEVR injection spec) ===")
    for vol, info in spec["candidateVolumes"].items():
        d = info["desc"] or {}
        ws = info["writerStates"][0] if info["writerStates"] else {}
        shader = ws.get("compute_shader") or {}
        log(f"  {vol}: {d.get('kind')} {d.get('width')}x{d.get('height')}x{d.get('depth')} "
            f"{d.get('format')} written by {shader.get('entry')} (hash {shader.get('hash', '')[:16]})")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
