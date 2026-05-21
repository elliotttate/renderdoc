"""For each interesting dead-right dispatch, identify the bound CS shader.

Picks one representative event per left-dispatch-dimension and does a
single SetFrameEvent + GetD3D12PipelineState to grab the CS reflection.
"""

import json
import os
import sys

sys.path.insert(0, r"E:\Github\renderdoc")
from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = os.environ.get(
    "SN2_INVESTIGATION_CAPTURE",
    r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc",
)
DEAD = os.environ.get("SN2_DEAD_JSON", r"E:\tmp_dir\sn2_invest_v5\dead_dispatches.json")
OUT = os.environ.get("SN2_SHADER_OUT", r"E:\tmp_dir\sn2_invest_v5\dead_dispatch_shaders.json")

with open(DEAD) as f:
    d = json.load(f)

# Pick one representative per left-dispatch-dim
seen = {}
for r in d["deadRight"]:
    dim = tuple(r["leftDispatch"])
    if dim not in seen:
        seen[dim] = r

print(f"Picking {len(seen)} representative events...")

cap, controller = _lib.open_capture(CAPTURE)
try:
    out_rows = []
    for dim, r in sorted(seen.items(), key=lambda kv: -int(kv[1]["leftEvent"])):
        L = int(r["leftEvent"])
        R = int(r["rightEvent"])
        row = {
            "leftEvent": L,
            "rightEvent": R,
            "leftDispatch": list(dim),
            "rightDispatch": r["rightDispatch"],
        }
        try:
            controller.SetFrameEvent(L, True)
            pipe = controller.GetPipelineState()
            try:
                refl = pipe.GetShaderReflection(rd.ShaderStage.Compute)
            except Exception:
                refl = None
            if refl is not None and len(refl.rawBytes) > 0:
                h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))
                row["leftCsShader"] = h
                row["leftCsEntry"] = str(refl.entryPoint)
                row["leftCsBytecodeSize"] = len(refl.rawBytes)
                # Also peek at root signature + first few SRV/UAV bindings
                try:
                    rws = pipe.GetReadWriteResources(rd.ShaderStage.Compute, False)
                    uavs = []
                    for u in rws[:8]:
                        desc = u.descriptor
                        if desc is not None and desc.resource is not None:
                            uavs.append({
                                "register": int(u.access.index),
                                "resource": _lib.resource_id_str(desc.resource),
                            })
                    row["leftUAVs"] = uavs
                except Exception:
                    pass
            else:
                row["leftCsShader"] = None
        except Exception as exc:
            row["leftError"] = str(exc)
        try:
            controller.SetFrameEvent(R, True)
            pipe = controller.GetPipelineState()
            try:
                refl = pipe.GetShaderReflection(rd.ShaderStage.Compute)
            except Exception:
                refl = None
            if refl is not None and len(refl.rawBytes) > 0:
                h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))
                row["rightCsShader"] = h
                row["rightCsEntry"] = str(refl.entryPoint)
        except Exception as exc:
            row["rightError"] = str(exc)
        out_rows.append(row)

    with open(OUT, "w") as f:
        json.dump(out_rows, f, indent=2, ensure_ascii=False, default=str)
    print(f"wrote {OUT}")
    print()
    print("Dead-right CS shaders by dim:")
    for r in out_rows:
        same = r.get("leftCsShader") == r.get("rightCsShader")
        marker = "SAME" if same else "DIFF"
        print(f"  dim={r['leftDispatch']} L={r['leftEvent']} {marker} hash={r.get('leftCsShader')} "
              f"entry={r.get('leftCsEntry')}")
finally:
    controller.Shutdown()
    cap.Shutdown()
