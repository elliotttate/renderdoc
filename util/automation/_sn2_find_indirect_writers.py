"""For each dead-right INDIRECT dispatch, identify the indirect-arg buffer
and find every compute dispatch that writes to it.

The action.copyDestination on an ExecuteIndirect action contains the
argument buffer's ResourceId. We pull it out, then call
compute_writers.find_writers() on that resource — that surfaces every
compute pass that produces the indirect dispatch dimensions.

For the SN2 case: dead-right IndirectDispatches likely have arg buffers
that view-0's compute pipeline writes for L but view-1's compute pipeline
fails to write for R. Identifying those writer dispatches narrows the bug
to a specific engine call path.
"""

import json
import os
import sys

sys.path.insert(0, r"E:\Github\renderdoc")
from util.automation import _lib, compute_writers  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = os.environ.get(
    "SN2_INVESTIGATION_CAPTURE",
    r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc",
)
DEAD = os.environ.get("SN2_DEAD_JSON", r"E:\tmp_dir\sn2_invest_v5\dead_dispatches.json")
OUT = os.environ.get("SN2_INDIRECT_OUT", r"E:\tmp_dir\sn2_invest_v5\indirect_arg_writers.json")

with open(DEAD) as f:
    dead = json.load(f)

# Filter to entries whose action name contains "IndirectDispatch" or "ExecuteIndirect"
indirect_pairs = [r for r in dead["deadRight"]
                  if "Indirect" in (r.get("leftName") or "") or
                     "Indirect" in (r.get("rightName") or "")]
print(f"Found {len(indirect_pairs)} dead-right indirect dispatches")

cap, controller = _lib.open_capture(CAPTURE)
try:
    sdfile = controller.GetStructuredFile()
    try:
        nchunks = sdfile.chunks.size()
    except Exception:
        nchunks = len(sdfile.chunks)

    # Build eventId -> chunk index lookup for ExecuteIndirect chunks
    eid_to_chunk_idx = {}
    for i in range(nchunks):
        chunk = sdfile.chunks[i]
        try:
            meta_eid = int(chunk.metadata.eventId)
        except Exception:
            continue
        name = str(chunk.name)
        if "ExecuteIndirect" in name or "Indirect" in name:
            eid_to_chunk_idx[meta_eid] = i

    print(f"indexed {len(eid_to_chunk_idx)} indirect-style chunks")

    rows = []
    arg_resources_seen = set()
    for r in indirect_pairs[:10]:  # cap at 10 to keep it quick
        L = int(r["leftEvent"])
        R = int(r["rightEvent"])
        entry = {
            "leftEvent": L,
            "rightEvent": R,
            "leftDispatch": r["leftDispatch"],
            "rightDispatch": r["rightDispatch"],
            "leftName": r["leftName"],
            "rightName": r["rightName"],
        }
        # Look up chunks for both sides; extract argument buffer ResourceId
        for side, eid in (("left", L), ("right", R)):
            idx = eid_to_chunk_idx.get(eid)
            if idx is None:
                entry[f"{side}ArgBufferError"] = "no indirect chunk for event"
                continue
            chunk = sdfile.chunks[idx]
            # ExecuteIndirect args: pArgumentBuffer (ResourceId), ArgumentBufferOffset, ...
            arg_buf = None
            arg_offset = None
            cmd_signature = None
            for j in range(chunk.NumChildren()):
                c = chunk.GetChild(j)
                cname = str(c.name)
                try:
                    if cname == "pArgumentBuffer":
                        arg_buf = _lib.resource_id_str(c.AsResourceId())
                    elif cname == "ArgumentBufferOffset":
                        arg_offset = int(c.AsInt())
                    elif cname == "pCommandSignature":
                        cmd_signature = _lib.resource_id_str(c.AsResourceId())
                except Exception:
                    pass
            entry[f"{side}ArgBuffer"] = arg_buf
            entry[f"{side}ArgOffset"] = arg_offset
            entry[f"{side}CmdSignature"] = cmd_signature
            if arg_buf:
                arg_resources_seen.add(arg_buf)
        rows.append(entry)

    out = {
        "summary": {
            "deadRightIndirectCount": len(indirect_pairs),
            "argResourcesUniqueScanned": len(arg_resources_seen),
        },
        "indirectEvents": rows,
    }
finally:
    controller.Shutdown()
    cap.Shutdown()

print(f"unique arg-buffer resources to scan: {len(arg_resources_seen)}")
print(f"  {sorted(arg_resources_seen)[:8]}")

# For each unique arg-buffer resource, call compute_writers.find_writers
writers_per_resource = {}
for res in sorted(arg_resources_seen):
    print(f"Finding writers for {res}...")
    try:
        w = compute_writers.find_writers(CAPTURE, res, eye_classify=False)
        writers_per_resource[res] = {
            "summary": w.get("summary", {}),
            "writes": w.get("writes", [])[:20],  # cap
        }
    except Exception as exc:
        writers_per_resource[res] = {"error": str(exc)}

out["writersPerResource"] = writers_per_resource

with open(OUT, "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False, default=str)
print(f"wrote {OUT}")
print()
print("Summary per arg-buffer:")
for res, info in writers_per_resource.items():
    s = info.get("summary") or {}
    print(f"  {res}: writeCount={s.get('writeCount', 0)}  "
          f"deadDispatches={s.get('deadDispatches', 0)}")
    for w in (info.get("writes") or [])[:3]:
        print(f"    event {w.get('eventId')} dim={w.get('dispatchDimension')} "
              f"name={(w.get('name') or '')[:60]}")
