"""Extract the two consumer PS DXBC bytecodes for UEVR's CRC32 lookup.

Saves:
  E:/tmp_dir/sn2_view_cb_diff/dxbc/MainPS_<RDhash16>.dxbc

Also computes:
  - Standard CRC32 (zlib, ITU-T V.42, used by UEVR per convention)
  - CRC32C (Castagnoli, less common but worth dumping)
  - MD5 (for cross-reference with RenderDoc's hash)
"""

import binascii
import hashlib
import json
import os
import sys
import time
import zlib

sys.path.insert(0, r"E:\Github\renderdoc")

OUT_DIR = r"E:\tmp_dir\sn2_view_cb_diff"
SHADER_DIR = os.path.join(OUT_DIR, "dxbc")
os.makedirs(SHADER_DIR, exist_ok=True)
LOG = os.path.join(OUT_DIR, "extract_ps_dxbc.log")


def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n"); f.flush()
    print(msg, flush=True)


with open(LOG, "w"): pass

from util.automation import _lib  # type: ignore
import renderdoc as rd  # noqa: E402

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260521_220823_frame1127.rdc"
EVENTS = [14845, 14853]


def crc32c(data):
    """CRC32-Castagnoli (used by SSE 4.2 hw crc32 instruction).
    Slow software impl — but for verification only.
    """
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


cap, controller = _lib.open_capture(CAPTURE)
try:
    results = []
    for eid in EVENTS:
        log(f"\n=== eid {eid} ===")
        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()
            refl = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
        except Exception as e:
            log(f"  failed: {e}"); continue
        if not refl or len(refl.rawBytes) == 0:
            log("  no reflection bytes"); continue
        bytecode = bytes(refl.rawBytes)
        entry = str(refl.entryPoint)

        # Hashes
        md5 = hashlib.md5(bytecode).hexdigest()
        crc32_zlib = zlib.crc32(bytecode) & 0xFFFFFFFF
        crc32_c = crc32c(bytecode)

        fname = os.path.join(SHADER_DIR, f"MainPS_{md5[:16]}.dxbc")
        with open(fname, "wb") as f:
            f.write(bytecode)
        log(f"  Entry: {entry}")
        log(f"  Size: {len(bytecode)} bytes")
        log(f"  MD5 (RD hash):    {md5}")
        log(f"  MD5 (first 16):   {md5[:16]}")
        log(f"  CRC32 zlib:       0x{crc32_zlib:08X}")
        log(f"  CRC32 Castagnoli: 0x{crc32_c:08X}")
        log(f"  Saved: {fname}")
        results.append({
            "eventId": eid, "entry": entry,
            "size": len(bytecode), "md5": md5,
            "crc32_zlib": f"0x{crc32_zlib:08X}",
            "crc32_castagnoli": f"0x{crc32_c:08X}",
            "path": fname,
        })

    log("\n=== UEVR CRC32 WHITELIST CANDIDATES ===")
    log("If UEVR uses zlib CRC32 (most common):")
    for r in results:
        log(f"  {r['crc32_zlib']}  // MainPS @ eid {r['eventId']}  (MD5 {r['md5'][:16]})")
    log("\nIf UEVR uses CRC32-C / Castagnoli:")
    for r in results:
        log(f"  {r['crc32_castagnoli']}  // MainPS @ eid {r['eventId']}  (MD5 {r['md5'][:16]})")

    with open(os.path.join(OUT_DIR, "ps_crc_extracted.json"), "w") as f:
        json.dump(results, f, indent=2, default=str)
    log("\nwrote ps_crc_extracted.json")

finally:
    controller.Shutdown()
    cap.Shutdown()

log("DONE")
os._exit(0)
