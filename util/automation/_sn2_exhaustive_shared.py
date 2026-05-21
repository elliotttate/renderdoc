"""Exhaustive shared-resource enumeration + minimal-repro export.

For the matched right-eye basepass draw at R=13157, walk every bound
SRV/UAV/CBV on the pixel + vertex stages. For each unique resource:
  - Compute its writers (every event that wrote to it)
  - Classify: shared-single-instance (1 writer, view-independent) vs
    per-eye (writers from multiple eyes) vs CPU-only
  - Tag known "global asset" resources (e.g., GPU scene buffer, descriptor
    heaps) so they don't pollute the bug list

Then export a minimal C++ repro of the right-eye basepass draw using
export_cpp.
"""

import json
import os
import sys
import time
import traceback

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_exhaustive"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

log("import _lib + compute_writers...")
from util.automation import _lib, compute_writers  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
RIGHT_BASEPASS_EID = 13157
LEFT_BASEPASS_EID = 11870


def write_json(name, payload):
    p = os.path.join(OUT_DIR, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    log(f"  -> {name}  ({os.path.getsize(p)} bytes)")


log("open_capture...")
cap, controller = _lib.open_capture(CAPTURE)
try:
    log(f"=== STEP 1: enumerate every binding at right-eye basepass R={RIGHT_BASEPASS_EID} ===")
    controller.SetFrameEvent(RIGHT_BASEPASS_EID, True)
    pipe = controller.GetPipelineState()

    bindings = []
    for stage in (rd.ShaderStage.Pixel, rd.ShaderStage.Vertex):
        for kind, getter in (("readonly", pipe.GetReadOnlyResources),
                             ("readwrite", pipe.GetReadWriteResources),
                             ("constant", pipe.GetConstantBlocks)):
            try:
                arr = getter(stage, False)
            except Exception:
                continue
            for u in arr:
                desc = u.descriptor
                if desc is None or desc.resource is None:
                    continue
                bindings.append({
                    "stage": _lib.shader_stage_name(stage),
                    "kind": kind,
                    "register": int(u.access.index),
                    "resource": _lib.resource_id_str(desc.resource),
                    "type": _lib.descriptor_type_name(u.access.type),
                })
    log(f"  total bindings: {len(bindings)}")
    write_json("step1_bindings.json", bindings)

    # ----- STEP 2: classify each unique resource -----
    log("=== STEP 2: classify each unique resource as shared/per-eye/global ===")
    unique_resources = sorted(set(b["resource"] for b in bindings))
    log(f"  {len(unique_resources)} unique resources to classify")

    # Pre-build action lookup by eid (for dispatch_dimension etc)
    action_by_eid = {int(a.eventId): a for a in _lib.walk_actions(controller)}

    classifications = []
    for res in unique_resources:
        try:
            w = compute_writers.find_writers(CAPTURE, res, eye_classify=False)
            s = w.get("summary", {})
            wc = s.get("writeCount", 0)
            writers = w.get("writes", [])
            # Quick heuristic: classify by writer count + writer event distribution
            if wc == 0:
                cls = "static_or_cpu_upload"
            elif wc == 1:
                cls = "single_writer_view_independent"
            else:
                # Multiple writers — check if they're temporally split (per-eye)
                # by event-id midpoint vs all clustered (shared multi-write)
                eids = sorted(int(w.get("eventId", 0)) for w in writers)
                if len(eids) >= 2:
                    spread = eids[-1] - eids[0]
                    if spread > 500:
                        cls = "multiple_writers_possibly_per_eye"
                    else:
                        cls = "multiple_writers_clustered"
                else:
                    cls = "single_writer_view_independent"
            classifications.append({
                "resource": res,
                "writeCount": wc,
                "classification": cls,
                "deadDispatches": s.get("deadDispatches", 0),
                "writerEvents": [int(w.get("eventId", 0)) for w in writers[:10]],
            })
            log(f"  {res}: writers={wc} cls={cls}")
        except Exception as exc:
            classifications.append({"resource": res, "error": str(exc)})
            log(f"  {res}: ERROR {exc}")

    write_json("step2_classifications.json", classifications)

    # Surface shared-and-suspicious resources
    suspicious = [c for c in classifications
                  if c.get("classification") == "single_writer_view_independent"]
    log(f"\n  SUSPICIOUS (single-writer view-independent): {len(suspicious)}")
    for s in suspicious:
        # Find which bindings used this resource
        binds = [b for b in bindings if b["resource"] == s["resource"]]
        regs = [(b["stage"], b["kind"], b["register"]) for b in binds]
        log(f"    {s['resource']}: writer event {s.get('writerEvents', [None])[0]}  bound at {regs}")

    # ----- STEP 3: minimal-repro export -----
    log("=== STEP 3: minimal-repro C++ export of right-eye basepass draw ===")
    # Export a narrow range around the right basepass draw
    export_first = RIGHT_BASEPASS_EID - 10
    export_last = RIGHT_BASEPASS_EID + 10
    log(f"  export range: events {export_first}..{export_last}")
    try:
        from util.automation import export_cpp  # type: ignore
        # Check what entry points export_cpp exposes
        if hasattr(export_cpp, "export_capture"):
            r = export_cpp.export_capture(
                CAPTURE,
                out_dir=os.path.join(OUT_DIR, "minimal_repro"),
                first_event=export_first,
                last_event=export_last,
                blobs_dir=os.path.join(OUT_DIR, "minimal_repro", "shaders"),
            )
            log(f"  export_capture done: {r}")
        else:
            log(f"  export_cpp does not expose export_capture, methods: {[m for m in dir(export_cpp) if not m.startswith('_')][:20]}")
    except Exception:
        log("  export failed:\n" + traceback.format_exc())

    # ----- STEP 4: dump root sig structure for the basepass PSO -----
    log("=== STEP 4: extract root sig layout for the basepass PSO ===")
    try:
        controller.SetFrameEvent(RIGHT_BASEPASS_EID, True)
        d3d12 = controller.GetD3D12PipelineState()
        pso_id = _lib.resource_id_str(d3d12.pipelineResourceId)
        rs_id = _lib.resource_id_str(d3d12.rootSignatureResourceId)
        log(f"  PSO: {pso_id}  RootSig: {rs_id}")
        rs_struct = {"pso": pso_id, "rootSig": rs_id, "params": []}
        # Iterate the root parameters
        params = d3d12.rootElements
        for i, p in enumerate(params):
            entry = {
                "index": i,
                "type": str(p.type).split(".")[-1] if hasattr(p, "type") else None,
                "visibility": str(p.visibility).split(".")[-1] if hasattr(p, "visibility") else None,
            }
            # Different fields per parameter type
            for attr in ("constantBuffer", "descriptorTable", "rangeIndex", "shaderRegister",
                         "registerSpace", "constants", "ranges", "byteSize"):
                if hasattr(p, attr):
                    try:
                        v = getattr(p, attr)
                        # Skip complex nested objects
                        if attr in ("ranges",) and hasattr(v, "__iter__"):
                            entry[attr] = [
                                {
                                    "type": str(r.type).split(".")[-1] if hasattr(r, "type") else None,
                                    "shaderRegister": int(r.shaderRegister) if hasattr(r, "shaderRegister") else None,
                                    "registerSpace": int(r.registerSpace) if hasattr(r, "registerSpace") else None,
                                    "count": int(r.count) if hasattr(r, "count") else None,
                                }
                                for r in v
                            ]
                        elif attr == "constants":
                            entry[attr] = {
                                "shaderRegister": int(v.shaderRegister) if hasattr(v, "shaderRegister") else None,
                                "registerSpace": int(v.registerSpace) if hasattr(v, "registerSpace") else None,
                                "numDwords": int(v.numDwords) if hasattr(v, "numDwords") else None,
                            }
                        else:
                            try:
                                entry[attr] = str(v)[:200]
                            except Exception:
                                entry[attr] = None
                    except Exception:
                        pass
            rs_struct["params"].append(entry)
        write_json("step4_root_sig.json", rs_struct)
        log(f"  {len(rs_struct['params'])} root parameters")
        for p in rs_struct["params"][:20]:
            log(f"    [{p['index']}] type={p.get('type')} vis={p.get('visibility')}")
    except Exception:
        log("  root sig dump failed:\n" + traceback.format_exc())

finally:
    controller.Shutdown()
    cap.Shutdown()

log("=== DONE ===")
os._exit(0)
