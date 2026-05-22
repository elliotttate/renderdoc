"""Identify every event that READS the final fog volumes (28374/28375/28376)
and characterize the binding site so UEVR knows where to swap the SRV
descriptor on the right-eye consumer.

For each reader:
  - Event ID, PS/VS entry name, shader hash
  - Eye classification (via View cbuffer offset)
  - Which shader register (t#) holds the fog SRV
  - The descriptor table slot — heap + offset — where the SRV descriptor
    actually lives at the time of the draw
  - The root parameter index that points at that table

Output: per-eye, per-fog-output binding site table.
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_phase1_spec"
LOG = os.path.join(OUT_DIR, "fog_consumers.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"

# The final-res fog outputs that the basepass / composite consume
FOG_OUTPUTS = ["ResourceId::28374", "ResourceId::28375", "ResourceId::28376"]

LEFT_VIEW_OFFSET = 3166208
RIGHT_VIEW_OFFSET = 3155968


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


def to_int(v):
    try:
        return int(v)
    except Exception:
        return None


def classify_eye(d3d12):
    """Classify L/R by the View cbuffer offset (b1, ResourceId::29343)."""
    try:
        for p in d3d12.rootSignature.parameters:
            try:
                d = p.descriptor
            except Exception:
                continue
            if d is None or getattr(d, "resource", None) is None:
                continue
            if res_id(d.resource) == "ResourceId::29343":
                off = int(d.byteOffset)
                if off == LEFT_VIEW_OFFSET:
                    return "left", off
                if off == RIGHT_VIEW_OFFSET:
                    return "right", off
                return f"other_{off}", off
    except Exception:
        pass
    return "unknown", None


def find_srv_binding_for_resource(controller, target_rid, stage):
    """For a stage, find which (register, descriptor-store, offset) binds target_rid."""
    pipe = controller.GetPipelineState()
    try:
        arr = pipe.GetReadOnlyResources(stage, False)
    except Exception:
        return None
    for u in arr:
        d = u.descriptor
        if d is None or d.resource is None:
            continue
        if res_id(d.resource) == target_rid:
            return {
                "register": int(u.access.index),
                "descriptorStore": res_id(u.access.descriptorStore),
                "descriptorStoreOffset": int(u.access.byteOffset),
            }
    return None


def find_root_param_for_table_offset(d3d12, heap_rid, descriptor_offset):
    """For a descriptor store + offset, identify which root parameter's
    descriptor-table window includes this descriptor."""
    try:
        for i, p in enumerate(d3d12.rootSignature.parameters):
            heap = res_id(p.heap) if hasattr(p, "heap") else None
            if heap != heap_rid:
                continue
            table_offset = to_int(p.heapByteOffset) if hasattr(p, "heapByteOffset") else None
            if table_offset is None:
                continue
            # The SRV's descriptor offset must be >= table_offset (and the table
            # ranges define how many descriptors are within). We don't have
            # exact range sizes available, so just report the root index whose
            # table-base is the closest (less than or equal to) the SRV offset.
            if table_offset <= descriptor_offset:
                # Could be inside this table — keep candidate
                return {
                    "rootIndex": i,
                    "tableBaseOffset": table_offset,
                    "delta_to_srv": descriptor_offset - table_offset,
                }
    except Exception:
        pass
    return None


cap, controller = _lib.open_capture(CAPTURE)
try:
    all_results = {}
    for fog_res in FOG_OUTPUTS:
        log(f"\n=== {fog_res} consumers ===")
        # Resolve
        target = None
        for r in controller.GetResources():
            if res_id(r.resourceId) == fog_res:
                target = r.resourceId; break
        if target is None:
            log(f"  not found")
            continue
        # Get all usages, filter to reads
        usage = controller.GetUsage(target)
        readers = []
        for u in usage:
            kind = str(u.usage).split(".")[-1]
            if "Resource" in kind and "RWResource" not in kind:
                readers.append({"eventId": int(u.eventId), "usage": kind})
        log(f"  {len(readers)} read usages")

        # Per-reader: get shader info, eye, binding location
        reader_details = []
        for r_entry in readers:
            eid = r_entry["eventId"]
            try:
                controller.SetFrameEvent(eid, True)
                d3d12 = controller.GetD3D12PipelineState()
                pipe = controller.GetPipelineState()
                eye, view_offset = classify_eye(d3d12)
                # Identify which stage reads it
                stage_used = None
                binding = None
                for stage_enum, stage_name in (
                    (rd.ShaderStage.Pixel, "Pixel"),
                    (rd.ShaderStage.Vertex, "Vertex"),
                    (rd.ShaderStage.Compute, "Compute"),
                ):
                    b = find_srv_binding_for_resource(controller, fog_res, stage_enum)
                    if b is not None:
                        stage_used = stage_name
                        binding = b
                        break
                # Get shader entry for that stage
                ps_entry = None
                if stage_used:
                    stage_enum = getattr(rd.ShaderStage, stage_used)
                    try:
                        refl = pipe.GetShaderReflection(stage_enum)
                        if refl and len(refl.rawBytes) > 0:
                            ps_entry = str(refl.entryPoint)
                    except Exception:
                        pass
                # Find the root parameter that owns this descriptor
                root_info = None
                if binding:
                    root_info = find_root_param_for_table_offset(
                        d3d12, binding["descriptorStore"], binding["descriptorStoreOffset"])
                reader_details.append({
                    "eventId": eid, "usage": r_entry["usage"],
                    "eye": eye, "viewOffset": view_offset,
                    "stage": stage_used, "entryPoint": ps_entry,
                    "binding": binding,
                    "rootParam": root_info,
                })
            except Exception as e:
                reader_details.append({"eventId": eid, "error": str(e)})
        # Summarize
        from collections import Counter
        eye_counts = Counter(r.get("eye") for r in reader_details)
        log(f"  eye distribution: {dict(eye_counts)}")
        for r in reader_details:
            entry = r.get("entryPoint") or "?"
            b = r.get("binding") or {}
            rp = r.get("rootParam") or {}
            log(f"    eid {r.get('eventId')} {r.get('stage', '?')} {entry[:35]:35} "
                f"eye={r.get('eye'):8} t{b.get('register', '?')} "
                f"heap={b.get('descriptorStore')}+{b.get('descriptorStoreOffset')} "
                f"root={rp.get('rootIndex')}+{rp.get('delta_to_srv')}")
        all_results[fog_res] = {"readers": reader_details}

    # Save
    out_json = os.path.join(OUT_DIR, "fog_consumers.json")
    with open(out_json, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    log(f"\nwrote {out_json}")

    # Final summary table
    log("\n=== UEVR ACTION ITEMS ===")
    log("For each (right-eye consumer event), UEVR needs to swap the SRV descriptor")
    log("at the listed (heap, offset) to point at the shadow allocation of the fog volume:")
    log("")
    for fog_res, info in all_results.items():
        right_readers = [r for r in info.get("readers", []) if r.get("eye") == "right"]
        log(f"\n  {fog_res}:")
        if not right_readers:
            log(f"    NO right-eye readers — fix may not be needed at this output")
            continue
        for r in right_readers:
            entry = r.get("entryPoint") or "?"
            b = r.get("binding") or {}
            log(f"    right-eye eid {r.get('eventId')} ({entry}): "
                f"swap descriptor at heap {b.get('descriptorStore')} +{b.get('descriptorStoreOffset')} "
                f"(t{b.get('register')})")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
