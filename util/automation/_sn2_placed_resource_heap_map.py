"""Extract CreatePlacedResource heap+offset for every resource in the
capture. Then group fog-shaped 3D textures by (heap, offset) to
definitively identify which ResourceIds share heap memory (= aliased).
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_aliasing_verify"
LOG = os.path.join(OUT_DIR, "placed_heap_map.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"


def res_id_str(obj):
    try:
        return _lib.resource_id_str(obj.AsResourceId())
    except Exception:
        pass
    try:
        return str(obj.data.basic.resourceId)
    except Exception:
        pass
    return None


def find_child(obj, name):
    try:
        for i in range(obj.NumChildren()):
            c = obj.GetChild(i)
            if str(c.name) == name:
                return c
    except Exception:
        pass
    return None


def child_as_int(obj, name, default=None):
    c = find_child(obj, name)
    if c is None:
        return default
    try:
        return int(c.AsInt())
    except Exception:
        return default


cap, controller = _lib.open_capture(CAPTURE)
try:
    # 3D texture catalog by shape
    textures_by_id = {}
    for t in controller.GetTextures():
        try:
            rid = _lib.resource_id_str(t.resourceId)
            textures_by_id[rid] = {
                "w": int(t.width), "h": int(t.height), "d": int(t.depth),
                "format": str(t.format.Name()),
                "is3D": int(t.depth) > 1,
            }
        except Exception:
            pass
    log(f"  texture catalog: {len(textures_by_id)} entries")

    sdfile = controller.GetStructuredFile()
    try:
        nchunks = sdfile.chunks.size()
    except Exception:
        nchunks = len(sdfile.chunks)
    log(f"  scanning {nchunks} chunks for resource creation calls...")

    # Walk all chunks looking for resource creation
    placed_resources = []   # list of {resource, heap, offset}
    committed_resources = []
    chunk_name_counts = {}

    for i in range(nchunks):
        chunk = sdfile.chunks[i]
        cname = str(chunk.name)
        if any(k in cname for k in ("CreatePlacedResource", "CreateReservedResource",
                                     "CreateCommittedResource", "OpenSharedHandle")):
            chunk_name_counts[cname] = chunk_name_counts.get(cname, 0) + 1
        # Specifically extract placed
        if "CreatePlacedResource" in cname:
            heap = find_child(chunk, "pHeap")
            heap_id = res_id_str(heap) if heap is not None else None
            offset = child_as_int(chunk, "HeapOffset")
            # Output resource — usually `ppvResource` or similar
            out_res = None
            for j in range(chunk.NumChildren()):
                c = chunk.GetChild(j)
                cn = str(c.name)
                # Look for an output ResourceId child
                if "Resource" in cn and cn != "pHeap":
                    candidate = res_id_str(c)
                    if candidate and candidate != "ResourceId::0":
                        out_res = candidate
            placed_resources.append({
                "chunkIndex": i, "resource": out_res,
                "heap": heap_id, "offset": offset,
            })
        elif "CreateCommittedResource" in cname:
            out_res = None
            for j in range(chunk.NumChildren()):
                c = chunk.GetChild(j)
                cn = str(c.name)
                if "Resource" in cn:
                    candidate = res_id_str(c)
                    if candidate and candidate != "ResourceId::0":
                        out_res = candidate
            committed_resources.append({
                "chunkIndex": i, "resource": out_res,
            })

    log(f"\n  chunk-kind counts:")
    for k, v in sorted(chunk_name_counts.items(), key=lambda kv: -kv[1]):
        log(f"    {v:5}  {k}")

    log(f"\n  placed resources: {len(placed_resources)}")
    log(f"  committed resources: {len(committed_resources)}")

    # Group by (heap, offset) — overlapping pairs = aliased
    from collections import defaultdict
    overlap_groups = defaultdict(list)
    for pr in placed_resources:
        if pr["heap"] and pr["offset"] is not None:
            overlap_groups[(pr["heap"], pr["offset"])].append(pr["resource"])

    log(f"\n  unique (heap, offset) buckets: {len(overlap_groups)}")
    multi_resource_buckets = [(k, v) for k, v in overlap_groups.items() if len(v) >= 2]
    log(f"  buckets with 2+ resources at SAME (heap, offset): {len(multi_resource_buckets)}")

    log(f"\n=== ALIASED RESOURCE GROUPS (top 30 by group size) ===")
    for (heap, offset), resources in sorted(multi_resource_buckets,
                                              key=lambda kv: -len(kv[1]))[:30]:
        # Get dimensions for each
        dims = [(r, textures_by_id.get(r, {})) for r in resources]
        log(f"\n  heap {heap} +{offset}:  {len(resources)} aliased resources")
        for rid, info in dims:
            if info:
                log(f"    {rid}: {info.get('w')}x{info.get('h')}x{info.get('d')} "
                    f"{info.get('format')}  is3D={info.get('is3D')}")
            else:
                log(f"    {rid}: (not a texture)")

    # Specifically check the known fog volumes
    log(f"\n=== KNOWN FOG VOLUMES — heap+offset and aliasing ===")
    KNOWN = ["ResourceId::28116", "ResourceId::30076",
             "ResourceId::28367", "ResourceId::28368",
             "ResourceId::28115", "ResourceId::28114",
             "ResourceId::28118", "ResourceId::28120",
             "ResourceId::28127", "ResourceId::28245",
             "ResourceId::28364", "ResourceId::28365"]
    for rid in KNOWN:
        pr = next((p for p in placed_resources if p["resource"] == rid), None)
        info = textures_by_id.get(rid, {})
        if pr:
            siblings = overlap_groups[(pr["heap"], pr["offset"])]
            log(f"  {rid} ({info.get('w')}x{info.get('h')}x{info.get('d')} {info.get('format')}): "
                f"heap {pr['heap']} +{pr['offset']}  ({len(siblings)} aliased siblings)")
            for sib in siblings:
                if sib != rid:
                    sinfo = textures_by_id.get(sib, {})
                    log(f"    sibling: {sib} ({sinfo.get('w')}x{sinfo.get('h')}x{sinfo.get('d')} {sinfo.get('format')})")
        else:
            # Not a placed resource — might be committed or proxy
            committed = any(c["resource"] == rid for c in committed_resources)
            log(f"  {rid} ({info.get('w')}x{info.get('h')}x{info.get('d')} {info.get('format')}): "
                f"{'committed (not aliased)' if committed else 'NOT a placed resource — no aliasing'}")

    out = {
        "chunkCounts": chunk_name_counts,
        "placedResourceCount": len(placed_resources),
        "committedResourceCount": len(committed_resources),
        "aliasedGroupCount": len(multi_resource_buckets),
        "aliasedGroups": [
            {"heap": k[0], "offset": k[1], "resources": v,
             "dimensions": [textures_by_id.get(r, {}) for r in v]}
            for k, v in multi_resource_buckets
        ],
    }
    with open(os.path.join(OUT_DIR, "placed_heap_map.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    log("\nwrote placed_heap_map.json")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
