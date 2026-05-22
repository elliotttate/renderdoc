"""Extract:
  1. VS bytecode + CRC32 for events 14845, 14853
  2. Raw root signature blob for the shared root sig (ResourceId::11720)
     plus a canonical-parameter-hash so UEVR can gate by RS layout
"""

import hashlib
import json
import os
import struct
import sys
import time
import zlib

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
SHADER_DIR = os.path.join(OUT_DIR, "dxbc")
os.makedirs(SHADER_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "extract_vs_rootsig.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"
EVENTS = [14845, 14853]
ROOT_SIG_RID = "ResourceId::11720"


def crc32c(data):
    table = []
    for i in range(256):
        v = i
        for _ in range(8):
            v = (v >> 1) ^ (0x82F63B78 if (v & 1) else 0)
        table.append(v)
    crc = 0xFFFFFFFF
    for byte in data:
        crc = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def res_id(r):
    return _lib.resource_id_str(r) if r is not None else None


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
    # =================================================================
    # 1. VS bytecodes
    # =================================================================
    log("=== VS bytecode extraction ===")
    vs_results = []
    for eid in EVENTS:
        log(f"\n  eid {eid}")
        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()
            refl = pipe.GetShaderReflection(rd.ShaderStage.Vertex)
        except Exception as e:
            log(f"    failed: {e}"); continue
        if not refl or len(refl.rawBytes) == 0:
            log("    no VS reflection")
            continue
        bytecode = bytes(refl.rawBytes)
        md5 = hashlib.md5(bytecode).hexdigest()
        c_zlib = zlib.crc32(bytecode) & 0xFFFFFFFF
        c_cast = crc32c(bytecode)
        fname = os.path.join(SHADER_DIR, f"VS_{md5[:16]}.dxbc")
        with open(fname, "wb") as f:
            f.write(bytecode)
        log(f"    Entry: {refl.entryPoint}  size: {len(bytecode)}")
        log(f"    MD5:               {md5}")
        log(f"    CRC32 zlib:        0x{c_zlib:08X}")
        log(f"    CRC32 Castagnoli:  0x{c_cast:08X}")
        log(f"    Saved: {fname}")
        vs_results.append({
            "eventId": eid, "entry": str(refl.entryPoint),
            "size": len(bytecode), "md5": md5,
            "crc32_zlib": f"0x{c_zlib:08X}",
            "crc32_castagnoli": f"0x{c_cast:08X}",
            "path": fname,
        })

    # =================================================================
    # 2. Root signature blob extraction
    # =================================================================
    log(f"\n=== Root signature blob for {ROOT_SIG_RID} ===")
    sdfile = controller.GetStructuredFile()
    try:
        nchunks = sdfile.chunks.size()
    except Exception:
        nchunks = len(sdfile.chunks)

    target_rs_id_num = int(ROOT_SIG_RID.split("::")[-1])
    log(f"  scanning {nchunks} chunks for CreateRootSignature(resourceId={target_rs_id_num})...")

    found_blob = None
    for i in range(nchunks):
        chunk = sdfile.chunks[i]
        cname = str(chunk.name)
        if "CreateRootSignature" not in cname:
            continue
        # Get the output ResourceId
        out_rid = None
        for j in range(chunk.NumChildren()):
            c = chunk.GetChild(j)
            cn = str(c.name)
            if "RootSignature" in cn and "blob" not in cn.lower() and "size" not in cn.lower():
                rid = res_id(c)
                if rid:
                    out_rid = rid
                    break
        if out_rid != ROOT_SIG_RID:
            continue
        log(f"  found at chunk {i}, output RID={out_rid}")
        # Find the blob — usually `pBlobWithRootSignature` + `blobLengthInBytes`
        blob_data = None
        blob_size = None
        for j in range(chunk.NumChildren()):
            c = chunk.GetChild(j)
            cn = str(c.name)
            log(f"    child: {cn}  (type={type(c).__name__})")
            if "Blob" in cn or "blob" in cn:
                # Try to extract bytes
                try:
                    n_bytes = c.NumChildren()
                    if n_bytes > 0:
                        # blob stored as array of bytes
                        blob_data = bytearray()
                        for k in range(n_bytes):
                            try:
                                blob_data.append(int(c.GetChild(k).AsInt()) & 0xFF)
                            except Exception:
                                pass
                        log(f"      collected {len(blob_data)} bytes from blob array")
                except Exception as e:
                    log(f"      blob extract error: {e}")
            elif "size" in cn.lower() or "Size" in cn:
                try:
                    blob_size = int(c.AsInt())
                except Exception:
                    pass
        if blob_data:
            found_blob = bytes(blob_data)
            log(f"  ✅ blob size: {len(found_blob)} bytes (declared {blob_size})")
            break

    rs_result = {"resourceId": ROOT_SIG_RID}
    if found_blob:
        md5 = hashlib.md5(found_blob).hexdigest()
        c_zlib = zlib.crc32(found_blob) & 0xFFFFFFFF
        c_cast = crc32c(found_blob)
        fname = os.path.join(SHADER_DIR, f"rootsig_{md5[:16]}.bin")
        with open(fname, "wb") as f:
            f.write(found_blob)
        log(f"  Saved: {fname}")
        log(f"  MD5:               {md5}")
        log(f"  CRC32 zlib:        0x{c_zlib:08X}")
        log(f"  CRC32 Castagnoli:  0x{c_cast:08X}")
        rs_result.update({
            "size": len(found_blob), "md5": md5,
            "crc32_zlib": f"0x{c_zlib:08X}",
            "crc32_castagnoli": f"0x{c_cast:08X}",
            "path": fname,
        })
    else:
        log("  no blob found in chunk children — falling back to parameter hash")

    # =================================================================
    # 3. Parsed root sig parameters — canonical structure hash
    # =================================================================
    log(f"\n=== Parsed root sig parameters for {ROOT_SIG_RID} ===")
    controller.SetFrameEvent(EVENTS[0], True)
    d3d12 = controller.GetD3D12PipelineState()
    rs_params = []
    try:
        for i, p in enumerate(d3d12.rootSignature.parameters):
            entry = {
                "index": i,
                "visibility": str(p.visibility).split(".")[-1] if hasattr(p, "visibility") else None,
            }
            # Detect type
            try:
                d = p.descriptor
                if d is not None and getattr(d, "resource", None) is not None:
                    entry["kind"] = "ROOT_DESCRIPTOR"
                    entry["type"] = str(d.type).split(".")[-1] if hasattr(d, "type") else None
                    entry["reg"] = int(p.reg)
                    entry["space"] = int(p.space)
            except Exception:
                pass
            try:
                heap = res_id(p.heap)
                if heap and heap != "ResourceId::0":
                    entry["kind"] = "DESCRIPTOR_TABLE"
                    ranges = []
                    try:
                        for r in p.tableRanges:
                            ranges.append({
                                "category": str(r.category).split(".")[-1] if hasattr(r, "category") else None,
                                "base": int(r.baseShaderRegister) if hasattr(r, "baseShaderRegister") else None,
                                "space": int(r.registerSpace) if hasattr(r, "registerSpace") else None,
                                "count": int(r.numDescriptors) if hasattr(r, "numDescriptors") else None,
                            })
                    except Exception:
                        pass
                    entry["ranges"] = ranges
            except Exception:
                pass
            rs_params.append(entry)
    except Exception as e:
        log(f"  parameter extract failed: {e}")

    log(f"  {len(rs_params)} parameters:")
    for p in rs_params:
        log(f"    [{p['index']}] {p.get('kind','?')} vis={p.get('visibility')} "
            f"{('reg='+str(p.get('reg'))+' space='+str(p.get('space'))) if 'reg' in p else ''} "
            f"{('ranges='+str(len(p.get('ranges',[])))) if 'ranges' in p else ''}")

    # Canonical hash over the parameter structure
    canon_str = json.dumps(rs_params, sort_keys=True, default=str)
    canon_bytes = canon_str.encode("utf-8")
    rs_result["canonical_param_hash_md5"] = hashlib.md5(canon_bytes).hexdigest()
    rs_result["canonical_param_hash_crc32_zlib"] = f"0x{zlib.crc32(canon_bytes) & 0xFFFFFFFF:08X}"
    rs_result["parameters"] = rs_params
    log(f"\n  canonical-parameter MD5: {rs_result['canonical_param_hash_md5']}")
    log(f"  canonical-parameter CRC32 zlib: {rs_result['canonical_param_hash_crc32_zlib']}")

    # Save everything
    with open(os.path.join(OUT_DIR, "vs_and_rootsig.json"), "w") as f:
        json.dump({"vs": vs_results, "rootsig": rs_result}, f, indent=2, default=str)
    log("\nwrote vs_and_rootsig.json")
finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
