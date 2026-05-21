"""Target the ACTUAL buggy draws — SingleLayerWaterCompositePS and
RenderSkyAtmosphereRayMarchingPS — and run the full root-cause drill on them.

Also fixes step 3 (use export_cpp.export instead of export_capture) and
step 4 (use the correct D3D12 root signature API).
"""

import hashlib
import json
import os
import struct
import sys
import time
import traceback

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_invest_target"
os.makedirs(OUT_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "_progress.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

log("loading candidates from v2...")
candidates = json.load(open(r"E:\tmp_dir\sn2_invest_root_v2\candidates.json"))

# Filter for the buggy shader entries
TARGET_ENTRIES = ("SingleLayerWaterCompositePS",
                  "RenderSkyAtmosphereRayMarchingPS",
                  "ComposeVolumetricRTOverScenePS")

target_draws = [c for c in candidates if c.get("psEntry") in TARGET_ENTRIES]
log(f"target draws: {len(target_draws)}")
for c in target_draws:
    log(f"  event {c['eventId']} entry={c.get('psEntry')} idx={c.get('numIndices')} eye={c['eye']} hash={c.get('psHash', '')[:16]}")

# Group by entry name + split by event-id midpoint within each group
from collections import defaultdict
by_entry = defaultdict(list)
for c in target_draws:
    by_entry[c.get("psEntry")].append(c)

pairs_to_analyze = []
for entry, draws in by_entry.items():
    if len(draws) < 2:
        log(f"  {entry}: only {len(draws)} draws, can't make pair")
        continue
    draws_sorted = sorted(draws, key=lambda c: c["eventId"])
    # Assume first half is left, second half is right
    mid = len(draws_sorted) // 2
    if mid == 0:
        continue
    L = draws_sorted[0]; R = draws_sorted[-1]
    pairs_to_analyze.append((entry, int(L["eventId"]), int(R["eventId"]), draws_sorted))

log(f"\npairs to analyze: {len(pairs_to_analyze)}")


def write_json(name, payload):
    p = os.path.join(OUT_DIR, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    log(f"  -> {name}  ({os.path.getsize(p)} bytes)")


log("import _lib...")
from util.automation import _lib, compute_writers, export_cpp  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
cap, controller = _lib.open_capture(CAPTURE)
try:
    for entry, L, R, all_draws in pairs_to_analyze:
        log(f"\n=== {entry}: L={L} R={R} ===")

        # Step A: dump bindings for each side
        all_resources = set()
        bindings_by_side = {}
        for side, eid in (("left", L), ("right", R)):
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()
            binds = []
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
                        rid = _lib.resource_id_str(desc.resource)
                        binds.append({
                            "stage": _lib.shader_stage_name(stage),
                            "kind": kind,
                            "register": int(u.access.index),
                            "resource": rid,
                            "type": _lib.descriptor_type_name(u.access.type),
                        })
                        all_resources.add(rid)
            bindings_by_side[side] = binds
        log(f"  L bindings: {len(bindings_by_side['left'])}  R bindings: {len(bindings_by_side['right'])}")
        log(f"  unique resources across both eyes: {len(all_resources)}")

        # Step B: compare which resources are shared vs different per-register
        l_dict = {(b["stage"], b["kind"], b["register"]): b for b in bindings_by_side["left"]}
        r_dict = {(b["stage"], b["kind"], b["register"]): b for b in bindings_by_side["right"]}
        keys = sorted(set(l_dict.keys()) | set(r_dict.keys()))
        shared = []
        differ = []
        for k in keys:
            lb = l_dict.get(k); rb = r_dict.get(k)
            if lb and rb and lb["resource"] == rb["resource"]:
                shared.append({"stage": k[0], "kind": k[1], "reg": k[2], "resource": lb["resource"]})
            elif lb and rb:
                differ.append({"stage": k[0], "kind": k[1], "reg": k[2],
                               "L": lb["resource"], "R": rb["resource"]})
            else:
                differ.append({"stage": k[0], "kind": k[1], "reg": k[2],
                               "L": lb["resource"] if lb else "NONE",
                               "R": rb["resource"] if rb else "NONE"})
        log(f"  shared resources: {len(shared)}  differ: {len(differ)}")

        # Surface the high-value PS shared bindings
        log(f"  shared PS bindings:")
        for s in shared:
            if s["stage"] == "Pixel" and s["kind"] in ("readonly", "constant"):
                log(f"    {s['kind']} t{s['reg']} = {s['resource']}")
        log(f"  differing PS bindings:")
        for d in differ:
            if d["stage"] == "Pixel" and d["kind"] in ("readonly", "constant"):
                log(f"    {d['kind']} t{d['reg']}: L={d['L']} R={d['R']}")

        # Step C: classify each unique PS-readonly resource
        log(f"  classifying {len(all_resources)} unique resources...")
        classifications = []
        for res in sorted(all_resources):
            try:
                w = compute_writers.find_writers(CAPTURE, res, eye_classify=False)
                s = w.get("summary", {})
                wc = s.get("writeCount", 0)
                writers = w.get("writes", [])
                if wc == 0:
                    cls = "static_or_cpu_upload"
                elif wc == 1:
                    cls = "single_writer_view_independent"
                else:
                    eids = sorted(int(w.get("eventId", 0)) for w in writers)
                    spread = eids[-1] - eids[0]
                    if spread > 500:
                        cls = "multiple_writers_possibly_per_eye"
                    else:
                        cls = "multiple_writers_clustered"
                classifications.append({
                    "resource": res, "writeCount": wc, "classification": cls,
                    "writerEvents": [int(w.get("eventId", 0)) for w in writers[:10]],
                })
            except Exception as exc:
                classifications.append({"resource": res, "error": str(exc)})

        suspicious = [c for c in classifications
                      if c.get("classification") == "single_writer_view_independent"]
        log(f"  SUSPICIOUS (single-writer view-independent): {len(suspicious)}")
        for s in suspicious:
            # Find bindings that use this resource
            binds_l = [b for b in bindings_by_side["left"] if b["resource"] == s["resource"]]
            binds_r = [b for b in bindings_by_side["right"] if b["resource"] == s["resource"]]
            regs_l = [(b["stage"], b["kind"], b["register"]) for b in binds_l]
            regs_r = [(b["stage"], b["kind"], b["register"]) for b in binds_r]
            log(f"    {s['resource']}: writer event {s.get('writerEvents', [None])[0]}  "
                f"L bound={regs_l} R bound={regs_r}")

        write_json(f"{entry}_analysis.json", {
            "entry": entry, "leftEventId": L, "rightEventId": R,
            "leftBindings": bindings_by_side["left"],
            "rightBindings": bindings_by_side["right"],
            "sharedResources": shared,
            "differingResources": differ,
            "classifications": classifications,
            "suspicious": suspicious,
        })

    # Step D: root sig dump for each entry's PSO (use correct API)
    log(f"\n=== STEP D: root signature dump ===")
    for entry, L, R, _ in pairs_to_analyze:
        controller.SetFrameEvent(L, True)
        d3d12 = controller.GetD3D12PipelineState()
        pso_id = _lib.resource_id_str(d3d12.pipelineResourceId)
        rs_id = _lib.resource_id_str(d3d12.rootSignature.resourceId) if hasattr(d3d12, "rootSignature") else None
        log(f"  {entry}: PSO={pso_id} RootSig={rs_id}")
        # Try to dump root sig structure
        try:
            rs = d3d12.rootSignature
            params = list(rs.parameters) if hasattr(rs, "parameters") else []
            log(f"    {len(params)} root params")
            params_out = []
            for i, p in enumerate(params):
                pt = str(p.parameterType).split(".")[-1] if hasattr(p, "parameterType") else None
                entry_p = {"index": i, "parameterType": pt,
                           "visibility": str(p.visibility).split(".")[-1] if hasattr(p, "visibility") else None}
                if pt == "DescriptorTable" and hasattr(p, "ranges"):
                    entry_p["ranges"] = []
                    for r in p.ranges:
                        rt = str(r.category).split(".")[-1] if hasattr(r, "category") else (
                             str(r.type).split(".")[-1] if hasattr(r, "type") else None)
                        entry_p["ranges"].append({
                            "type": rt,
                            "shaderRegister": int(r.baseShaderRegister) if hasattr(r, "baseShaderRegister") else None,
                            "registerSpace": int(r.registerSpace) if hasattr(r, "registerSpace") else None,
                            "count": int(r.numDescriptors) if hasattr(r, "numDescriptors") else None,
                        })
                elif pt in ("Constants",) and hasattr(p, "constant"):
                    entry_p["constants"] = {
                        "shaderRegister": int(p.constant.shaderRegister) if hasattr(p.constant, "shaderRegister") else None,
                        "registerSpace": int(p.constant.registerSpace) if hasattr(p.constant, "registerSpace") else None,
                        "numDwords": int(p.constant.num32BitValues) if hasattr(p.constant, "num32BitValues") else None,
                    }
                elif hasattr(p, "descriptor"):
                    entry_p["descriptor"] = {
                        "shaderRegister": int(p.descriptor.shaderRegister) if hasattr(p.descriptor, "shaderRegister") else None,
                        "registerSpace": int(p.descriptor.registerSpace) if hasattr(p.descriptor, "registerSpace") else None,
                    }
                params_out.append(entry_p)
            write_json(f"{entry}_root_sig.json", {
                "pso": pso_id, "rootSig": rs_id, "params": params_out,
            })
        except Exception:
            log("    root sig dump failed:\n" + traceback.format_exc())

    # Step E: minimal C++ export for the most interesting pair
    log(f"\n=== STEP E: minimal C++ export ===")
    if pairs_to_analyze:
        entry, L, R, _ = pairs_to_analyze[0]
        log(f"  exporting around L={L} R={R} for {entry}")
        try:
            out_dir = os.path.join(OUT_DIR, "minimal_repro")
            os.makedirs(out_dir, exist_ok=True)
            ret = export_cpp.export(
                CAPTURE, out_dir,
                first_event=L - 5, last_event=R + 5,
                shaders_subdir="shaders",
            )
            log(f"  export done — {ret}")
        except TypeError as e:
            log(f"  signature mismatch: {e}")
            # Try alternate signature
            try:
                ret = export_cpp.export(CAPTURE, out_dir)
                log(f"  export (no-range) done — {ret}")
            except Exception:
                log("  export failed:\n" + traceback.format_exc())
        except Exception:
            log("  export failed:\n" + traceback.format_exc())

finally:
    controller.Shutdown()
    cap.Shutdown()

log("=== DONE ===")
os._exit(0)
