"""SQLite event index — fast SQL queries over a capture.

Walks every action + every resource + every shader once, populates a
SQLite database, then offers convenience queries.

Schema:
    events(event_id, kind, action_name, num_indices, num_instances,
           dispatch_x, dispatch_y, dispatch_z, viewport_x, viewport_y,
           viewport_w, viewport_h, pso_id, ps_entry, ps_hash_md5_short,
           ps_hash_crc32_zlib, cs_entry, cs_hash_md5_short, rt0_id,
           rt0_width, rt0_height, rt0_format, depth_id)
    resources(resource_id, name, kind, width, height, depth, arraysize,
              format, length, creation_flags)
    shaders(md5_full, md5_short, crc32_zlib, crc32_castagnoli, size,
            entry_point, stage)
    descriptor_writes(event_id, dest_heap, dest_slot, src_heap, src_slot,
                      category, resource_id, write_kind)

Once built you can run arbitrary SQL:
    SELECT pso_id, COUNT(*) FROM events
      WHERE kind='draw' AND viewport_x >= 600
      GROUP BY pso_id ORDER BY 2 DESC LIMIT 10;

CLI:
    python util/automation/sqlite_index.py build <capture.rdc> [--out indexdir]
    python util/automation/sqlite_index.py sql   <indexdir or capture.rdc> "SELECT ..."
    python util/automation/sqlite_index.py find-events <indexdir> --kind draw --pso ResourceId::123
    python util/automation/sqlite_index.py histogram <indexdir> --by pso
"""

import argparse
import json
import os
import sqlite3
import sys
import zlib
import hashlib

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


def _action_kind(a):
    f = int(a.flags)
    if f & int(rd.ActionFlags.Drawcall):     return "draw"
    if f & int(rd.ActionFlags.Dispatch):     return "dispatch"
    if f & int(rd.ActionFlags.Copy):         return "copy"
    if f & int(rd.ActionFlags.Resolve):      return "resolve"
    if f & int(rd.ActionFlags.Clear):        return "clear"
    if f & int(rd.ActionFlags.GenMips):      return "genmips"
    if f & int(rd.ActionFlags.Present):      return "present"
    if f & int(rd.ActionFlags.CmdList):      return "cmdlist"
    if f & int(rd.ActionFlags.PassBoundary): return "pass_boundary"
    if f & int(rd.ActionFlags.SetMarker):    return "marker"
    return "other"


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id            INTEGER PRIMARY KEY,
    kind                TEXT,
    action_name         TEXT,
    num_indices         INTEGER,
    num_instances       INTEGER,
    dispatch_x          INTEGER,
    dispatch_y          INTEGER,
    dispatch_z          INTEGER,
    viewport_x          REAL,
    viewport_y          REAL,
    viewport_w          REAL,
    viewport_h          REAL,
    pso_id              TEXT,
    root_sig_id         TEXT,
    ps_entry            TEXT,
    ps_hash_md5_short   TEXT,
    ps_hash_crc32_zlib  INTEGER,
    cs_entry            TEXT,
    cs_hash_md5_short   TEXT,
    cs_hash_crc32_zlib  INTEGER,
    rt0_id              TEXT,
    rt0_width           INTEGER,
    rt0_height          INTEGER,
    rt0_format          TEXT,
    depth_id            TEXT,
    eye                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_pso ON events(pso_id);
CREATE INDEX IF NOT EXISTS idx_ps_entry ON events(ps_entry);
CREATE INDEX IF NOT EXISTS idx_cs_entry ON events(cs_entry);
CREATE INDEX IF NOT EXISTS idx_ps_hash ON events(ps_hash_md5_short);
CREATE INDEX IF NOT EXISTS idx_rt0 ON events(rt0_id);
CREATE INDEX IF NOT EXISTS idx_eye ON events(eye);

CREATE TABLE IF NOT EXISTS resources (
    resource_id     TEXT PRIMARY KEY,
    name            TEXT,
    kind            TEXT,
    width           INTEGER,
    height          INTEGER,
    depth           INTEGER,
    arraysize       INTEGER,
    mips            INTEGER,
    format          TEXT,
    length          INTEGER,
    creation_flags  TEXT
);
CREATE INDEX IF NOT EXISTS idx_res_kind ON resources(kind);
CREATE INDEX IF NOT EXISTS idx_res_format ON resources(format);
CREATE INDEX IF NOT EXISTS idx_res_name ON resources(name);

CREATE TABLE IF NOT EXISTS shaders (
    md5_full          TEXT PRIMARY KEY,
    md5_short         TEXT,
    crc32_zlib        INTEGER,
    crc32_castagnoli  INTEGER,
    size              INTEGER,
    entry_point       TEXT,
    stage             TEXT,
    resource_id       TEXT,
    first_event       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sh_crc ON shaders(crc32_zlib);

CREATE TABLE IF NOT EXISTS descriptor_writes (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER,
    dest_heap       TEXT,
    dest_slot       INTEGER,
    src_heap        TEXT,
    src_slot        INTEGER,
    category        TEXT,
    resource_id     TEXT,
    write_kind      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_dw_event ON descriptor_writes(event_id);
CREATE INDEX IF NOT EXISTS idx_dw_dest ON descriptor_writes(dest_heap, dest_slot);
CREATE INDEX IF NOT EXISTS idx_dw_resource ON descriptor_writes(resource_id);
"""


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


def _eye_from_viewport_x(vp_x, main_w=None):
    if vp_x is None: return None
    if main_w and vp_x >= main_w / 2.5: return "right"
    if vp_x >= 400: return "right"
    return "left"


def build(capture_path: str, out_dir: str = None, progress=None) -> str:
    """Build the SQLite index for a capture. Returns the db path."""
    out_dir = out_dir or (capture_path + ".sqlite-index")
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, "capture.sqlite")
    if os.path.exists(db_path):
        os.unlink(db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    cap, controller = _lib.open_capture(capture_path)
    try:
        # Resources
        textures = {_lib.resource_id_str(t.resourceId): t for t in controller.GetTextures()}
        buffers  = {_lib.resource_id_str(b.resourceId): b for b in controller.GetBuffers()}
        name_by_id = {}
        for r in controller.GetResources():
            rid = _lib.resource_id_str(r.resourceId)
            if not rid: continue
            try: name_by_id[rid] = str(r.name)
            except Exception: name_by_id[rid] = None
        rows = []
        for rid, t in textures.items():
            rows.append((rid, name_by_id.get(rid), "Texture",
                         int(t.width), int(t.height), int(t.depth),
                         int(t.arraysize), int(t.mips), str(t.format.Name()),
                         None, str(t.creationFlags).split(".")[-1] if hasattr(t, "creationFlags") else None))
        for rid, b in buffers.items():
            rows.append((rid, name_by_id.get(rid), "Buffer", None, None, None, None, None,
                         None, int(b.length),
                         str(b.creationFlags).split(".")[-1] if hasattr(b, "creationFlags") else None))
        conn.executemany("""INSERT OR REPLACE INTO resources
            (resource_id, name, kind, width, height, depth, arraysize, mips,
             format, length, creation_flags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
        conn.commit()

        # Walk actions
        seen_shaders = {}
        ev_rows = []
        n = 0
        for a in _lib.walk_actions(controller):
            kind = _action_kind(a)
            eid = int(a.eventId)
            try:
                num_indices = int(a.numIndices)
            except Exception:
                num_indices = None
            try:
                num_inst = int(a.numInstances)
            except Exception:
                num_inst = None
            try:
                dx, dy, dz = [int(v) for v in a.dispatchDimension]
            except Exception:
                dx = dy = dz = None
            try:
                action_name = str(a.GetName(controller.GetStructuredFile()))
            except Exception:
                action_name = None
            vp_x = vp_y = vp_w = vp_h = None
            pso_id = root_sig_id = None
            ps_entry = cs_entry = None
            ps_md5_short = cs_md5_short = None
            ps_crc32 = cs_crc32 = None
            rt0_id = rt0_w = rt0_h = rt0_fmt = depth_id = None
            if kind in ("draw", "dispatch"):
                try:
                    controller.SetFrameEvent(eid, True)
                    pipe = controller.GetPipelineState()
                    d3d12 = controller.GetD3D12PipelineState()
                except Exception:
                    pipe = d3d12 = None
                if pipe and d3d12:
                    try:
                        if len(d3d12.rasterizer.viewports) > 0:
                            v = d3d12.rasterizer.viewports[0]
                            vp_x, vp_y = float(v.x), float(v.y)
                            vp_w, vp_h = float(v.width), float(v.height)
                    except Exception: pass
                    try: pso_id = _lib.resource_id_str(d3d12.pipelineResourceId)
                    except Exception: pass
                    try: root_sig_id = _lib.resource_id_str(d3d12.rootSignature.resourceId)
                    except Exception: pass
                    try:
                        for s_enum, s_lab in ((rd.ShaderStage.Pixel, "ps"),
                                                (rd.ShaderStage.Compute, "cs")):
                            try:
                                refl = pipe.GetShaderReflection(s_enum)
                            except Exception:
                                refl = None
                            if refl and len(refl.rawBytes) > 0:
                                raw = bytes(refl.rawBytes)
                                md5 = hashlib.md5(raw).hexdigest()
                                short = md5[:16]
                                cz = zlib.crc32(raw) & 0xFFFFFFFF
                                if md5 not in seen_shaders:
                                    seen_shaders[md5] = True
                                    cc = _crc32c(raw)
                                    conn.execute("""INSERT OR REPLACE INTO shaders
                                        (md5_full, md5_short, crc32_zlib, crc32_castagnoli,
                                         size, entry_point, stage, resource_id, first_event)
                                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                        (md5, short, cz, cc, len(raw),
                                         str(refl.entryPoint), s_lab.upper(),
                                         _lib.resource_id_str(refl.resourceId) if hasattr(refl, "resourceId") else None,
                                         eid))
                                if s_lab == "ps":
                                    ps_entry, ps_md5_short, ps_crc32 = str(refl.entryPoint), short, cz
                                else:
                                    cs_entry, cs_md5_short, cs_crc32 = str(refl.entryPoint), short, cz
                    except Exception: pass
                    try:
                        if len(d3d12.outputMerger.renderTargets) > 0:
                            rt = d3d12.outputMerger.renderTargets[0]
                            rt0_id = _lib.resource_id_str(rt.resource)
                            if rt0_id:
                                t = textures.get(rt0_id)
                                if t:
                                    rt0_w, rt0_h = int(t.width), int(t.height)
                                    rt0_fmt = str(t.format.Name())
                    except Exception: pass
                    try:
                        depth_id = _lib.resource_id_str(d3d12.outputMerger.depthTarget.resource)
                    except Exception: pass
            eye = _eye_from_viewport_x(vp_x)
            ev_rows.append((eid, kind, action_name, num_indices, num_inst,
                            dx, dy, dz, vp_x, vp_y, vp_w, vp_h,
                            pso_id, root_sig_id,
                            ps_entry, ps_md5_short, ps_crc32,
                            cs_entry, cs_md5_short, cs_crc32,
                            rt0_id, rt0_w, rt0_h, rt0_fmt, depth_id, eye))
            n += 1
            if progress and n % 200 == 0:
                progress(n)
            if len(ev_rows) >= 500:
                conn.executemany("""INSERT INTO events VALUES (
                  ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                  ?, ?, ?, ?, ?, ?, ?, ?, ?)""", ev_rows)
                ev_rows.clear()
                conn.commit()
        if ev_rows:
            conn.executemany("""INSERT INTO events VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?)""", ev_rows)
            conn.commit()

        # Descriptor writes via new C++ API (best-effort)
        try:
            if hasattr(controller, "GetDescriptorWrites"):
                writes = controller.GetDescriptorWrites()
                dw_rows = []
                for w in writes:
                    dw_rows.append((int(w.eventId),
                                     _lib.resource_id_str(w.destHeap),
                                     int(w.destSlot),
                                     _lib.resource_id_str(w.srcHeap) if hasattr(w, "srcHeap") else None,
                                     int(w.srcSlot) if hasattr(w, "srcSlot") else None,
                                     str(w.category).split(".")[-1] if hasattr(w, "category") else None,
                                     _lib.resource_id_str(w.resourceId) if hasattr(w, "resourceId") else None,
                                     int(w.writeKind) if hasattr(w, "writeKind") else None))
                if dw_rows:
                    conn.executemany("""INSERT INTO descriptor_writes
                        (event_id, dest_heap, dest_slot, src_heap, src_slot,
                         category, resource_id, write_kind)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", dw_rows)
                    conn.commit()
        except Exception:
            pass
    finally:
        controller.Shutdown()
        cap.Shutdown()
        conn.close()
    return db_path


def sql(db_path: str, query: str) -> list:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(query)
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _find_db(path):
    if path.endswith(".sqlite"):  return path
    if os.path.isfile(path) and path.endswith(".rdc"):
        return os.path.join(path + ".sqlite-index", "capture.sqlite")
    if os.path.isdir(path):
        cand = os.path.join(path, "capture.sqlite")
        if os.path.isfile(cand): return cand
    raise FileNotFoundError(f"can't find SQLite index for {path}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build")
    pb.add_argument("capture")
    pb.add_argument("--out")

    ps = sub.add_parser("sql")
    ps.add_argument("indexdir")
    ps.add_argument("query")
    ps.add_argument("--json")

    pf = sub.add_parser("find-events")
    pf.add_argument("indexdir")
    pf.add_argument("--kind")
    pf.add_argument("--pso")
    pf.add_argument("--ps-entry")
    pf.add_argument("--ps-crc32", type=lambda x: int(x, 0))
    pf.add_argument("--eye")
    pf.add_argument("--rt-id")
    pf.add_argument("--vp-x-min", type=float)
    pf.add_argument("--limit", type=int, default=50)

    ph = sub.add_parser("histogram")
    ph.add_argument("indexdir")
    ph.add_argument("--by", required=True)
    ph.add_argument("--kind")

    args = ap.parse_args()

    if args.cmd == "build":
        def progress(n): print(f"  …{n} events indexed", file=sys.stderr)
        db = build(args.capture, args.out, progress=progress)
        print(f"\nBuilt: {db}")
        # Print quick row counts
        rows = sql(db, "SELECT COUNT(*) AS n FROM events")
        print(f"  events:    {rows[0]['n']}")
        rows = sql(db, "SELECT COUNT(*) AS n FROM resources")
        print(f"  resources: {rows[0]['n']}")
        rows = sql(db, "SELECT COUNT(*) AS n FROM shaders")
        print(f"  shaders:   {rows[0]['n']}")
        return 0

    if args.cmd == "sql":
        db = _find_db(args.indexdir)
        rows = sql(db, args.query)
        if args.json:
            with open(args.json, "w") as f: json.dump(rows, f, indent=2, default=str)
            print(f"wrote {args.json} ({len(rows)} rows)")
        else:
            for r in rows: print(r)
            print(f"\n{len(rows)} row(s)")
        return 0

    if args.cmd == "find-events":
        db = _find_db(args.indexdir)
        clauses = []
        params = []
        if args.kind:       clauses.append("kind = ?"); params.append(args.kind)
        if args.pso:        clauses.append("pso_id = ?"); params.append(args.pso)
        if args.ps_entry:   clauses.append("ps_entry = ?"); params.append(args.ps_entry)
        if args.ps_crc32:   clauses.append("ps_hash_crc32_zlib = ?"); params.append(args.ps_crc32)
        if args.eye:        clauses.append("eye = ?"); params.append(args.eye)
        if args.rt_id:      clauses.append("rt0_id = ?"); params.append(args.rt_id)
        if args.vp_x_min:   clauses.append("viewport_x >= ?"); params.append(args.vp_x_min)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        q = f"SELECT * FROM events{where} ORDER BY event_id LIMIT {args.limit}"
        rows = sql(db, q + ";")
        # placeholder substitution
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        cur.execute(q.replace("LIMIT " + str(args.limit), "LIMIT " + str(args.limit)), params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()
        for r in rows: print(r)
        print(f"\n{len(rows)} event(s)")
        return 0

    if args.cmd == "histogram":
        db = _find_db(args.indexdir)
        col_map = {
            "kind": "kind", "pso": "pso_id", "ps_entry": "ps_entry",
            "cs_entry": "cs_entry", "ps_crc32": "ps_hash_crc32_zlib",
            "eye": "eye", "rt_id": "rt0_id",
            "rt_shape": "rt0_width || 'x' || rt0_height",
            "viewport_x": "viewport_x",
        }
        col = col_map.get(args.by, args.by)
        where = f" WHERE kind = '{args.kind}'" if args.kind else ""
        rows = sql(db, f"SELECT {col} AS k, COUNT(*) AS n FROM events{where} "
                        f"GROUP BY {col} ORDER BY n DESC")
        total = sum(r["n"] for r in rows)
        print(f"\nHistogram by {args.by}: {total} total, {len(rows)} groups\n")
        for r in rows[:40]:
            print(f"  {r['n']:>7}  {r['k']}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
