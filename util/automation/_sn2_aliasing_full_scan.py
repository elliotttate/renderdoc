"""Re-run aliasing scan WITHOUT truncation + check every aliasing-barrier
target against ALL 3D R11G11B10F textures (fog-volume-shaped resources).
"""

import json
import os
import sys
import time

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_aliasing_verify"
LOG = os.path.join(OUT_DIR, "aliasing_full.log")


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


cap, controller = _lib.open_capture(CAPTURE)
try:
    # Build a catalog of all 3D textures (fog-volume shape candidates)
    log("=== Build 3D R11G11B10F texture catalog ===")
    candidates = {}  # rid_str -> dimensions
    for t in controller.GetTextures():
        try:
            if int(t.depth) > 1 or "3D" in str(t.type):
                rid = _lib.resource_id_str(t.resourceId)
                candidates[rid] = {
                    "w": int(t.width), "h": int(t.height), "d": int(t.depth),
                    "format": str(t.format.Name()),
                }
        except Exception:
            pass
    log(f"  found {len(candidates)} 3D textures total")
    fog_shaped = {k: v for k, v in candidates.items()
                  if "R11G11B10" in v["format"] or "R16G16B16A16" in v["format"]}
    log(f"  of which fog-shaped (R11G11B10F or R16G16B16A16F): {len(fog_shaped)}")
    for rid, dim in sorted(fog_shaped.items())[:30]:
        log(f"    {rid}: {dim['w']}x{dim['h']}x{dim['d']} {dim['format']}")

    # Scan ALL aliasing barriers (full, no truncation)
    log("\n=== Full aliasing-barrier scan ===")
    sdfile = controller.GetStructuredFile()
    try:
        nchunks = sdfile.chunks.size()
    except Exception:
        nchunks = len(sdfile.chunks)
    all_barriers = []
    for i in range(nchunks):
        chunk = sdfile.chunks[i]
        if "ResourceBarrier" not in str(chunk.name):
            continue
        bs = find_child(chunk, "pBarriers") or find_child(chunk, "Barriers")
        if bs is None:
            continue
        try:
            n = bs.NumChildren()
        except Exception:
            continue
        for k in range(n):
            b = bs.GetChild(k)
            tc = find_child(b, "Type")
            if tc is None:
                continue
            try:
                tv = int(tc.AsInt())
            except Exception:
                continue
            if tv != 1:
                continue
            al = find_child(b, "Aliasing")
            before = after = None
            if al is not None:
                rb = find_child(al, "pResourceBefore")
                ra = find_child(al, "pResourceAfter")
                if rb is not None: before = res_id_str(rb)
                if ra is not None: after = res_id_str(ra)
            all_barriers.append({
                "chunkIndex": i, "before": before, "after": after,
            })
    log(f"  total aliasing barriers: {len(all_barriers)}")

    # Check if any aliasing barrier targets a 3D fog-shaped resource
    log("\n=== Aliasing barriers targeting 3D fog-shaped textures ===")
    fog_set = set(fog_shaped.keys())
    fog_3d_set = set(candidates.keys())  # all 3D, not just fog-shaped
    hits = []
    for ab in all_barriers:
        if ab["before"] in fog_3d_set or ab["after"] in fog_3d_set:
            hits.append(ab)
    log(f"  aliasing barriers touching ANY 3D texture: {len(hits)}")
    fog_hits = [h for h in hits if h["before"] in fog_set or h["after"] in fog_set]
    log(f"  aliasing barriers touching FOG-SHAPED 3D textures (R11G11B10F/R16G16B16A16F): {len(fog_hits)}")
    for h in fog_hits[:30]:
        bd = fog_shaped.get(h["after"]) or fog_shaped.get(h["before"]) or {}
        log(f"    chunk {h['chunkIndex']}: {h['before']} -> {h['after']}  "
            f"({bd.get('w')}x{bd.get('h')}x{bd.get('d')} {bd.get('format')})")

    # Check our KNOWN fog 3D textures (from earlier work)
    log("\n=== Aliasing barriers on KNOWN UWE fog 3D textures ===")
    KNOWN = ["ResourceId::28116", "ResourceId::30076",
             "ResourceId::28367", "ResourceId::28368"]
    for rid in KNOWN:
        hits = [ab for ab in all_barriers if ab["before"] == rid or ab["after"] == rid]
        log(f"  {rid}: {len(hits)} aliasing barriers")
        for h in hits[:5]:
            log(f"    chunk {h['chunkIndex']}: {h['before']} -> {h['after']}")

    out = {
        "totalAliasingBarriers": len(all_barriers),
        "3DTexturesTotal": len(candidates),
        "fogShaped3DTextures": fog_shaped,
        "aliasingHitsAny3D": len(hits),
        "fogShapedHits": fog_hits,
        "knownFogVolumeHits": {
            rid: [ab for ab in all_barriers if ab["before"] == rid or ab["after"] == rid]
            for rid in KNOWN
        },
    }
    with open(os.path.join(OUT_DIR, "aliasing_full.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)
    log("\nwrote aliasing_full.json")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
