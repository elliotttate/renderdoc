"""CRC32 shader indexing for cross-tool shader identity matching.

RenderDoc internally hashes shader bytecode with MD5. UEVR and many other
tools (ShaderToggler, ReShade addons, NV Nsight) use CRC32 variants.

This module computes the full identity set for every shader in a capture
and writes a persistent JSON+SQLite index so reverse lookups are O(1):

  - md5_full            (32 hex chars)         — matches RenderDoc's hash
  - md5_short           (first 16 hex chars)   — matches our internal _lib.shader_bytecode_hash
  - crc32_zlib          (8 hex chars)          — IEEE-802.3 / zlib.crc32
                                                 most common; what UEVR uses
  - crc32_castagnoli    (8 hex chars)          — SSE 4.2 CRC32C
  - shader_toggler_crc  (8 hex chars)          — ShaderToggler's variant
                                                 (same as crc32_zlib by default;
                                                 if a fork uses a non-standard
                                                 polynomial, override via init_args)

Usage:

    # Index all shaders in a capture
    python util/automation/crc32_shader_index.py <capture.rdc> --out <dir>

    # Reverse lookup by any hash
    python util/automation/crc32_shader_index.py <out_dir> --find-by-crc32 0x8733F2E0
    python util/automation/crc32_shader_index.py <out_dir> --find-by-md5  1b3cb1a890626439

    # From Python:
    from util.automation import crc32_shader_index
    idx = crc32_shader_index.build_index(capture_path)
    hits = idx.find_by_crc32_zlib(0x8733F2E0)
    bytecode = idx.load_bytecode(hits[0])
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import zlib

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


# Castagnoli (CRC32C, used by SSE 4.2's hardware CRC32 instruction).
def _crc32c(data: bytes) -> int:
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


def hashes_for(bytecode: bytes) -> dict:
    md5 = hashlib.md5(bytecode).hexdigest()
    z = zlib.crc32(bytecode) & 0xFFFFFFFF
    c = _crc32c(bytecode)
    return {
        "md5_full": md5,
        "md5_short": md5[:16],
        "crc32_zlib": z,
        "crc32_zlib_hex": f"0x{z:08X}",
        "crc32_castagnoli": c,
        "crc32_castagnoli_hex": f"0x{c:08X}",
        # ShaderToggler currently uses zlib CRC32 over the DXBC bytes.
        # If a fork uses a different polynomial, add it as another hash here.
        "shader_toggler_crc": z,
        "shader_toggler_crc_hex": f"0x{z:08X}",
        "size": len(bytecode),
    }


class ShaderIndex:
    def __init__(self, out_dir: str):
        self.out_dir = out_dir
        os.makedirs(out_dir, exist_ok=True)
        self.db_path = os.path.join(out_dir, "shaders.sqlite")
        self.blob_dir = os.path.join(out_dir, "blobs")
        os.makedirs(self.blob_dir, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self._init_schema()

    def _init_schema(self):
        c = self.conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS shaders (
                md5_full         TEXT PRIMARY KEY,
                md5_short        TEXT,
                crc32_zlib       INTEGER,
                crc32_castagnoli INTEGER,
                size             INTEGER,
                entry_point      TEXT,
                stage            TEXT,
                resource_id      TEXT,
                first_event      INTEGER,
                blob_path        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_md5_short ON shaders(md5_short);
            CREATE INDEX IF NOT EXISTS idx_crc32_zlib ON shaders(crc32_zlib);
            CREATE INDEX IF NOT EXISTS idx_crc32_castagnoli ON shaders(crc32_castagnoli);
            CREATE INDEX IF NOT EXISTS idx_resource_id ON shaders(resource_id);
            CREATE INDEX IF NOT EXISTS idx_entry_point ON shaders(entry_point);
        """)
        self.conn.commit()

    def add(self, bytecode: bytes, entry_point: str = None, stage: str = None,
            resource_id: str = None, event_id: int = None) -> dict:
        h = hashes_for(bytecode)
        c = self.conn.cursor()
        existing = c.execute(
            "SELECT blob_path FROM shaders WHERE md5_full = ?", (h["md5_full"],)
        ).fetchone()
        if existing:
            return h
        blob_path = os.path.join(self.blob_dir, f"{h['md5_short']}.dxbc")
        if not os.path.exists(blob_path):
            with open(blob_path, "wb") as f:
                f.write(bytecode)
        c.execute("""
            INSERT INTO shaders (md5_full, md5_short, crc32_zlib, crc32_castagnoli,
                                 size, entry_point, stage, resource_id, first_event,
                                 blob_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (h["md5_full"], h["md5_short"], h["crc32_zlib"], h["crc32_castagnoli"],
              h["size"], entry_point, stage, resource_id, event_id, blob_path))
        self.conn.commit()
        return h

    def find_by_md5(self, md5_or_prefix: str) -> list:
        c = self.conn.cursor()
        if len(md5_or_prefix) == 32:
            rows = c.execute("SELECT * FROM shaders WHERE md5_full = ?", (md5_or_prefix,)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM shaders WHERE md5_short LIKE ? OR md5_full LIKE ?",
                (md5_or_prefix + "%", md5_or_prefix + "%")
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def find_by_crc32_zlib(self, crc: int) -> list:
        crc = int(crc) & 0xFFFFFFFF
        c = self.conn.cursor()
        rows = c.execute("SELECT * FROM shaders WHERE crc32_zlib = ?", (crc,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def find_by_crc32_castagnoli(self, crc: int) -> list:
        crc = int(crc) & 0xFFFFFFFF
        c = self.conn.cursor()
        rows = c.execute("SELECT * FROM shaders WHERE crc32_castagnoli = ?", (crc,)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def find_by_entry_point(self, entry: str) -> list:
        c = self.conn.cursor()
        rows = c.execute(
            "SELECT * FROM shaders WHERE entry_point = ?", (entry,)
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def all(self) -> list:
        c = self.conn.cursor()
        rows = c.execute("SELECT * FROM shaders").fetchall()
        return [self._row_to_dict(r) for r in rows]

    def load_bytecode(self, entry) -> bytes:
        path = entry["blob_path"] if isinstance(entry, dict) else entry
        with open(path, "rb") as f:
            return f.read()

    def _row_to_dict(self, row):
        cols = ["md5_full", "md5_short", "crc32_zlib", "crc32_castagnoli", "size",
                "entry_point", "stage", "resource_id", "first_event", "blob_path"]
        out = dict(zip(cols, row))
        # Hex-format the crc32s for human readability
        out["crc32_zlib_hex"] = f"0x{out['crc32_zlib']:08X}"
        out["crc32_castagnoli_hex"] = f"0x{out['crc32_castagnoli']:08X}"
        return out

    def close(self):
        self.conn.close()


def build_index(capture_path: str, out_dir: str = None,
                progress=None) -> ShaderIndex:
    """Walk every event in capture_path, hash every PS/VS/CS/HS/DS/GS/AS/MS
    shader reflection, populate the index.

    If out_dir is None, the index is created next to the capture as
    `<capture>.shader-index/`.
    """
    if out_dir is None:
        out_dir = capture_path + ".shader-index"
    idx = ShaderIndex(out_dir)
    cap, controller = _lib.open_capture(capture_path)
    seen_resource_ids = set()
    stages = [
        (rd.ShaderStage.Vertex,    "Vertex"),
        (rd.ShaderStage.Hull,      "Hull"),
        (rd.ShaderStage.Domain,    "Domain"),
        (rd.ShaderStage.Geometry,  "Geometry"),
        (rd.ShaderStage.Pixel,     "Pixel"),
        (rd.ShaderStage.Compute,   "Compute"),
        (rd.ShaderStage.Amplification, "Amplification"),
        (rd.ShaderStage.Mesh,      "Mesh"),
    ]
    n = 0
    for a in _lib.walk_actions(controller):
        flags = int(a.flags)
        if not (flags & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
            continue
        eid = int(a.eventId)
        try:
            controller.SetFrameEvent(eid, True)
            pipe = controller.GetPipelineState()
        except Exception:
            continue
        for stage_enum, stage_name in stages:
            try:
                refl = pipe.GetShaderReflection(stage_enum)
            except Exception:
                refl = None
            if not refl or len(refl.rawBytes) == 0:
                continue
            rid_str = _lib.resource_id_str(refl.resourceId) if hasattr(refl, "resourceId") else None
            key = (rid_str, stage_name)
            if key in seen_resource_ids:
                continue
            seen_resource_ids.add(key)
            bytecode = bytes(refl.rawBytes)
            idx.add(bytecode,
                    entry_point=str(refl.entryPoint),
                    stage=stage_name,
                    resource_id=rid_str,
                    event_id=eid)
            n += 1
            if progress and n % 50 == 0:
                progress(n)
    controller.Shutdown()
    cap.Shutdown()
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="capture file (.rdc) OR existing index dir")
    ap.add_argument("--out", help="output dir for index (default: <capture>.shader-index/)")
    ap.add_argument("--find-by-crc32", help="reverse lookup; hex (0x...) or decimal")
    ap.add_argument("--find-by-crc32c", help="reverse lookup by Castagnoli CRC")
    ap.add_argument("--find-by-md5", help="reverse lookup by MD5 or MD5 prefix")
    ap.add_argument("--list", action="store_true", help="list every shader in the index")
    args = ap.parse_args()

    # Reverse-lookup mode operates on an existing index dir
    if (args.find_by_crc32 or args.find_by_crc32c or args.find_by_md5 or args.list):
        idx_path = args.path
        if os.path.isfile(args.path):
            idx_path = args.path + ".shader-index"
        if not os.path.isdir(idx_path):
            print(f"index dir not found: {idx_path}"); return 2
        idx = ShaderIndex(idx_path)
        if args.find_by_crc32:
            crc = int(args.find_by_crc32, 0)
            hits = idx.find_by_crc32_zlib(crc)
        elif args.find_by_crc32c:
            crc = int(args.find_by_crc32c, 0)
            hits = idx.find_by_crc32_castagnoli(crc)
        elif args.find_by_md5:
            hits = idx.find_by_md5(args.find_by_md5)
        else:
            hits = idx.all()
        for h in hits:
            print(json.dumps(h, indent=2))
        print(f"\n{len(hits)} hit(s)")
        idx.close()
        return 0

    # Build-index mode
    if not os.path.isfile(args.path):
        print(f"capture file not found: {args.path}"); return 2
    out_dir = args.out or (args.path + ".shader-index")
    print(f"Building shader index at {out_dir}…")
    def progress(n):
        print(f"  …{n} shaders indexed", file=sys.stderr)
    idx = build_index(args.path, out_dir, progress=progress)
    all_shaders = idx.all()
    print(f"\nIndexed {len(all_shaders)} unique shader bytecodes.")
    print(f"Database: {idx.db_path}")
    print(f"Blob dir: {idx.blob_dir}")
    idx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
