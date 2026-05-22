"""RenderDoc Automation MCP Server.

Exposes the util.automation toolkit as MCP tools so LLM agents can drive
the full RenderDoc analysis + replay-mutation pipeline.

Tool categories:
  * Session         — open / list / close captures (persistent handles)
  * Triage          — quick_triage, event_histogram, doctor
  * Indexing        — SQLite event index + SQL queries
  * Resources       — resource_resolver, resource_lineage
  * Shaders         — CRC32 shader index, compile, disassemble
  * Cost            — top-N expensive draws
  * State           — state_at_event, descriptor writes
  * Replay-mutation — SetBufferOverride, SetBufferOverrideGPU
  * Stereo          — eye_classifier, pair_eye_events
  * Diff            — capture_diff, eye_image_diff, event_diff
  * Fix validation  — fix_validator
  * PSO swap        — pso_swap_harness generator

Usage:
    python -m util.automation.mcp.server          # stdio MCP server
    python -m util.automation.mcp.server --http   # HTTP/SSE server (if FastMCP supports)

Or wire into Claude Code / Cursor config:
    {
      "mcpServers": {
        "renderdoc-automation": {
          "command": "qrenderdoc.exe",
          "args": ["--python", "<repo>/util/automation/mcp/server.py"]
        }
      }
    }

If qrenderdoc isn't used, the renderdoc Python module must be on PYTHONPATH.
"""

import argparse
import base64
import json
import os
import sys
import traceback

# Allow running both as module and as script
_THIS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_THIS)))  # add 'util/' parent
sys.path.insert(0, os.path.dirname(_THIS))                   # add 'util/automation/'

try:
    from automation.mcp.session import manager
    from automation import _lib  # noqa
except Exception:
    from mcp.session import manager  # type: ignore
    import _lib  # type: ignore  # noqa


try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    print("ERROR: 'mcp' Python package not installed.", file=sys.stderr)
    print("Install with:  pip install mcp", file=sys.stderr)
    print(f"  ({e})", file=sys.stderr)
    sys.exit(2)


mcp = FastMCP("renderdoc-automation")


def _err(msg, **extra):
    return {"ok": False, "error": str(msg), **extra}


def _ok(**kwargs):
    return {"ok": True, **kwargs}


def _import_renderdoc():
    """Lazy import renderdoc so the server starts even if the binding is missing."""
    try:
        import renderdoc  # noqa: F401
        return True, None
    except ImportError as e:
        return False, str(e)


# =====================================================================
# SESSION
# =====================================================================
@mcp.tool()
def rdoc_session_open(path: str) -> dict:
    """Open a .rdc capture and keep a persistent replay-controller handle.

    Returns:
      { ok, session_id, path }
    """
    ok, err = _import_renderdoc()
    if not ok: return _err(err)
    try:
        sid = manager().open(path)
        return _ok(session_id=sid, path=os.path.abspath(path))
    except Exception as e:
        return _err(e)


@mcp.tool()
def rdoc_session_list() -> dict:
    """List currently-open capture sessions."""
    return _ok(sessions=manager().list())


@mcp.tool()
def rdoc_session_close(session_id: str) -> dict:
    """Close a capture session and release the replay controller."""
    return _ok(closed=manager().close(session_id))


# =====================================================================
# TRIAGE
# =====================================================================
@mcp.tool()
def rdoc_doctor(strict: bool = False) -> dict:
    """Run the automation-toolkit health check (Python / pyrenderdoc /
    qrenderdoc / renderdoccmd / dxc / glslang / etc.)."""
    try:
        from automation import doctor
        report = doctor.run()
        return _ok(report=report)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_quick_triage(capture_path: str, top: int = 10) -> dict:
    """First-look "what's in this capture?" report.

    Returns: action counts by kind, top PSOs, top shader entries,
    RT shapes, largest textures, stereo hint, etc.
    """
    try:
        from automation import quick_triage
        return _ok(report=quick_triage.triage(capture_path, top))
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_event_histogram(capture_path: str, group_by: str = "kind",
                          kind_filter: str = "all", top: int = 40) -> dict:
    """Histogram events by various keys: kind, pso, ps_entry, cs_entry,
    rt_shape, rt_resource, viewport_x, action_name, num_indices_bucket."""
    try:
        from automation import event_histogram
        h = event_histogram.histogram(capture_path, group_by, kind_filter)
        items = h.most_common(top)
        return _ok(group_by=group_by, total=sum(h.values()),
                    unique_groups=len(h), buckets=items)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


# =====================================================================
# INDEXING (SQLite)
# =====================================================================
@mcp.tool()
def rdoc_sqlite_build(capture_path: str, out_dir: str = None) -> dict:
    """Build a SQLite event index for fast SQL queries.

    Returns the db path. Subsequent rdoc_sqlite_sql calls use that path.
    """
    try:
        from automation import sqlite_index
        db = sqlite_index.build(capture_path, out_dir)
        return _ok(db_path=db)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_sqlite_sql(db_path_or_capture: str, query: str,
                     limit: int = 200) -> dict:
    """Run a read-only SQL query against a previously-built SQLite event
    index. Pass either the .sqlite path or the original .rdc path
    (the index is assumed to be at `<rdc>.sqlite-index/capture.sqlite`).
    """
    try:
        from automation import sqlite_index
        db = sqlite_index._find_db(db_path_or_capture)
        rows = sqlite_index.sql(db, query)
        return _ok(rows=rows[:limit], total=len(rows), truncated=len(rows) > limit)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_find_events(db_path_or_capture: str,
                      kind: str = None, pso: str = None,
                      ps_entry: str = None, ps_crc32: int = None,
                      eye: str = None, rt_id: str = None,
                      vp_x_min: float = None, limit: int = 50) -> dict:
    """Filter events from the SQLite index by common criteria."""
    try:
        import sqlite3
        from automation import sqlite_index
        db = sqlite_index._find_db(db_path_or_capture)
        clauses, params = [], []
        if kind:     clauses.append("kind = ?"); params.append(kind)
        if pso:      clauses.append("pso_id = ?"); params.append(pso)
        if ps_entry: clauses.append("ps_entry = ?"); params.append(ps_entry)
        if ps_crc32 is not None:
            clauses.append("ps_hash_crc32_zlib = ?"); params.append(int(ps_crc32) & 0xFFFFFFFF)
        if eye:      clauses.append("eye = ?"); params.append(eye)
        if rt_id:    clauses.append("rt0_id = ?"); params.append(rt_id)
        if vp_x_min is not None: clauses.append("viewport_x >= ?"); params.append(vp_x_min)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        q = f"SELECT * FROM events{where} ORDER BY event_id LIMIT {int(limit)}"
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        cur.execute(q, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()
        return _ok(rows=rows, count=len(rows))
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


# =====================================================================
# RESOURCES
# =====================================================================
@mcp.tool()
def rdoc_resource_resolve(capture_path: str, resource_id: str = None,
                           resource_id_substr: str = None,
                           name_regex: str = None,
                           any_str: str = None,
                           dim_w: int = None, dim_h: int = None, dim_d: int = None,
                           format_substr: str = None,
                           is_3d: bool = None) -> dict:
    """Find resources by name / handle / partial-ID / dimensions+format.
    Returns each match with usage events bucketed by role."""
    try:
        from automation import resource_resolver
        dim = None
        if dim_w and dim_h and dim_d:
            dim = (dim_w, dim_h, dim_d)
        match = resource_resolver.Match(
            resource_id=resource_id,
            resource_id_substr=resource_id_substr,
            name_regex=name_regex,
            any_str=any_str,
            dim=dim,
            format_substr=format_substr,
            is_3d=is_3d,
        )
        hits = resource_resolver.resolve(capture_path, match)
        return _ok(hits=[{
            "resource_id": h.resource_id,
            "name": h.name,
            "summary": h.summary,
            "info": h.info,
            "events_by_role": {k: v for k, v in h.events_by_role.items()},
        } for h in hits])
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_resource_lineage(capture_path: str, resource_id: str) -> dict:
    """List every read/write of a resource, with the last writer + event ids."""
    try:
        from automation import resource_lineage
        return _ok(report=resource_lineage.lineage(capture_path, resource_id))
    except Exception:
        # Fall back to resource_resolver if module shape doesn't match
        try:
            from automation import resource_resolver
            hits = resource_resolver.resolve(capture_path,
                resource_resolver.Match(resource_id=resource_id))
            return _ok(hits=[{
                "resource_id": h.resource_id, "summary": h.summary,
                "events_by_role": {k: v for k, v in h.events_by_role.items()},
            } for h in hits])
        except Exception as e:
            return _err(e, trace=traceback.format_exc())


# =====================================================================
# SHADERS
# =====================================================================
@mcp.tool()
def rdoc_shader_index_build(capture_path: str, out_dir: str = None) -> dict:
    """Build the CRC32 shader index (MD5 + CRC32-zlib + CRC32C) for a capture."""
    try:
        from automation import crc32_shader_index
        idx = crc32_shader_index.build_index(capture_path, out_dir)
        shaders = idx.all()
        idx.close()
        return _ok(count=len(shaders),
                    db_path=idx.db_path,
                    blob_dir=idx.blob_dir)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_shader_find(out_dir: str, by_crc32_zlib: int = None,
                      by_crc32_castagnoli: int = None, by_md5: str = None,
                      by_entry: str = None) -> dict:
    """Reverse-lookup a shader in a previously-built shader index."""
    try:
        from automation import crc32_shader_index
        idx_dir = out_dir
        if os.path.isfile(out_dir):
            idx_dir = out_dir + ".shader-index"
        idx = crc32_shader_index.ShaderIndex(idx_dir)
        hits = []
        if by_crc32_zlib is not None:
            hits = idx.find_by_crc32_zlib(int(by_crc32_zlib))
        elif by_crc32_castagnoli is not None:
            hits = idx.find_by_crc32_castagnoli(int(by_crc32_castagnoli))
        elif by_md5:
            hits = idx.find_by_md5(by_md5)
        elif by_entry:
            hits = idx.find_by_entry_point(by_entry)
        else:
            hits = idx.all()
        idx.close()
        return _ok(hits=hits[:200], count=len(hits))
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_shader_compile_hlsl(source: str, entry: str = "main",
                              target: str = "ps_6_6",
                              defines: dict = None,
                              spirv: bool = False) -> dict:
    """Compile HLSL → DXBC/DXIL (or SPIR-V if spirv=True) via dxc.exe.
    Returns base64-encoded bytecode in the result."""
    try:
        from automation import shader_compile
        r = shader_compile.compile_hlsl(source=source, entry=entry,
                                          target=target, defines=defines or {},
                                          spirv=spirv)
        if r.get("bytecode"):
            r["bytecode_b64"] = base64.b64encode(r["bytecode"]).decode("ascii")
            r["bytecode_size"] = len(r["bytecode"])
            del r["bytecode"]
        return _ok(**r)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_shader_compile_glsl(source: str, stage: str = None) -> dict:
    """Compile GLSL → SPIR-V via glslangValidator."""
    try:
        from automation import shader_compile
        r = shader_compile.compile_glsl(source=source, stage=stage)
        if r.get("bytecode"):
            r["bytecode_b64"] = base64.b64encode(r["bytecode"]).decode("ascii")
            r["bytecode_size"] = len(r["bytecode"])
            del r["bytecode"]
        return _ok(**r)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_shader_disassemble(bytecode_b64: str = None,
                              path: str = None) -> dict:
    """Disassemble a DXBC/DXIL blob via dxc -dumpbin."""
    try:
        from automation import shader_compile
        bc = base64.b64decode(bytecode_b64) if bytecode_b64 else None
        r = shader_compile.disassemble_dxbc(bytecode=bc, path=path)
        return _ok(**r)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


# =====================================================================
# COST
# =====================================================================
@mcp.tool()
def rdoc_top_n_costs(capture_path: str, n: int = 20,
                      kind_filter: str = "all",
                      sort_by: str = "gpu_time_ns",
                      name_regex: str = None) -> dict:
    """Top-N most expensive draws/dispatches by GPU time (or other counters)."""
    try:
        from automation import top_n_costs
        rows = top_n_costs.top_n(capture_path, n, kind_filter,
                                   name_regex, sort_by)
        return _ok(rows=rows)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


# =====================================================================
# STATE
# =====================================================================
@mcp.tool()
def rdoc_state_at_event(capture_path: str, event_id: int) -> dict:
    """Full pipeline-state snapshot at an event (root sig, bindings,
    RT/DS, PSO/shaders, viewport)."""
    try:
        from automation import state_at_event
        return _ok(state=state_at_event.collect(capture_path, int(event_id)))
    except Exception:
        # Fall back to _lib.collect_state_at_event
        try:
            cap, controller = _lib.open_capture(capture_path)
            try:
                state = _lib.collect_state_at_event(controller, int(event_id))
                return _ok(state=state)
            finally:
                controller.Shutdown(); cap.Shutdown()
        except Exception as e:
            return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_descriptor_writes(capture_path: str, limit: int = 500) -> dict:
    """Driver-side normalized descriptor write log (uses our new
    `IReplayController::GetDescriptorWrites()` C++ API)."""
    try:
        import renderdoc as rd  # noqa
        cap, controller = _lib.open_capture(capture_path)
        try:
            if not hasattr(controller, "GetDescriptorWrites"):
                return _err("GetDescriptorWrites not bound — rebuild qrenderdoc")
            writes = controller.GetDescriptorWrites()
            out = []
            for w in writes[:limit]:
                out.append({
                    "event_id":    int(w.eventId),
                    "dest_heap":   _lib.resource_id_str(w.destHeap),
                    "dest_slot":   int(w.destSlot),
                    "src_heap":    _lib.resource_id_str(w.srcHeap) if hasattr(w, "srcHeap") else None,
                    "src_slot":    int(w.srcSlot) if hasattr(w, "srcSlot") else None,
                    "category":    str(w.category).split(".")[-1] if hasattr(w, "category") else None,
                    "resource_id": _lib.resource_id_str(w.resourceId) if hasattr(w, "resourceId") else None,
                    "write_kind":  int(w.writeKind) if hasattr(w, "writeKind") else None,
                })
            return _ok(writes=out, total=len(writes), truncated=len(writes) > limit)
        finally:
            controller.Shutdown(); cap.Shutdown()
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


# =====================================================================
# REPLAY MUTATION
# =====================================================================
@mcp.tool()
def rdoc_set_buffer_override_gpu(session_id: str, buffer_resource_id: str,
                                  offset: int, data_b64: str) -> dict:
    """Patch GPU memory at replay time — write `data` into the captured
    buffer at `offset`. Subsequent draws GPU-sample the patched bytes.

    Uses our new `SetBufferOverrideGPU` C++ API.
    """
    try:
        import renderdoc as rd  # noqa
        sess = manager().get(session_id)
        if not sess:
            return _err(f"unknown session_id {session_id}")
        data = base64.b64decode(data_b64)
        # Resolve resource id
        target = None
        for r in sess.controller.GetResources():
            if _lib.resource_id_str(r.resourceId) == buffer_resource_id:
                target = r.resourceId; break
        if target is None:
            return _err(f"resource not found: {buffer_resource_id}")
        with sess.lock:
            if not hasattr(sess.controller, "SetBufferOverrideGPU"):
                return _err("SetBufferOverrideGPU not bound — rebuild qrenderdoc")
            ok = sess.controller.SetBufferOverrideGPU(target, int(offset), data)
        return _ok(applied=ok, bytes_written=len(data))
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_clear_buffer_override_gpu(session_id: str,
                                     buffer_resource_id: str) -> dict:
    """Undo a previous SetBufferOverrideGPU."""
    try:
        sess = manager().get(session_id)
        if not sess: return _err(f"unknown session_id {session_id}")
        target = None
        for r in sess.controller.GetResources():
            if _lib.resource_id_str(r.resourceId) == buffer_resource_id:
                target = r.resourceId; break
        if target is None:
            return _err(f"resource not found: {buffer_resource_id}")
        with sess.lock:
            sess.controller.ClearBufferOverrideGPU(target)
        return _ok()
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_set_buffer_override(session_id: str, buffer_resource_id: str,
                              offset: int, data_b64: str) -> dict:
    """Patch buffer reads at the analysis level (GetBufferData /
    GetCBufferVariableContents see the patched bytes). Doesn't affect
    the actual GPU rendering — for use with the shader debugger."""
    try:
        sess = manager().get(session_id)
        if not sess: return _err(f"unknown session_id {session_id}")
        data = base64.b64decode(data_b64)
        target = None
        for r in sess.controller.GetResources():
            if _lib.resource_id_str(r.resourceId) == buffer_resource_id:
                target = r.resourceId; break
        if target is None:
            return _err(f"resource not found: {buffer_resource_id}")
        with sess.lock:
            sess.controller.SetBufferOverride(target, int(offset), data)
        return _ok(bytes_written=len(data))
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


# =====================================================================
# STEREO
# =====================================================================
@mcp.tool()
def rdoc_eye_classify(capture_path: str, fast: bool = True) -> dict:
    """Classify every draw/dispatch as LEFT/RIGHT eye via viewport.x /
    RT shape / instanced-stereo slice."""
    try:
        if fast:
            try:
                from automation import eye_classifier_fast as ec
            except ImportError:
                from automation import eye_classifier as ec
        else:
            from automation import eye_classifier as ec
        result = ec.classify(capture_path)
        return _ok(result=result)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_pair_eye_events(capture_path: str) -> dict:
    """Auto-pair LEFT/RIGHT events by PSO + shader hash + RT."""
    try:
        from automation import pair_eye_events
        return _ok(pairs=pair_eye_events.pair(capture_path))
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


# =====================================================================
# DIFF
# =====================================================================
@mcp.tool()
def rdoc_capture_diff(capture_a: str, capture_b: str) -> dict:
    """Diff two captures (action counts, PSO usage, binding deltas)."""
    try:
        from automation import capture_diff
        return _ok(diff=capture_diff.diff(capture_a, capture_b))
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_diff_replay(capture_a: str, capture_b: str) -> dict:
    """Replay two captures side-by-side and report per-event RT min/max
    delta + first event where outputs diverge."""
    try:
        from automation import diff_replay
        return _ok(report=diff_replay.diff(capture_a, capture_b))
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


# =====================================================================
# FIX VALIDATION
# =====================================================================
@mcp.tool()
def rdoc_fix_validator(before_capture: str, after_capture: str,
                        left_roi: list = None, right_roi: list = None,
                        target_pso_ids: list = None,
                        out_dir: str = "fix_evidence") -> dict:
    """Run the fix-claim evidence pipeline. Returns a verdict
    (PASS / WEAK-PASS / FAIL) + score + notes + evidence ZIP."""
    try:
        from automation import fix_validator
        lr = tuple(left_roi) if left_roi else None
        rr = tuple(right_roi) if right_roi else None
        verdict = fix_validator.run(
            before_capture, after_capture,
            left_roi=lr, right_roi=rr,
            target_pso_ids=target_pso_ids,
            out_dir=out_dir,
        )
        return _ok(verdict=verdict)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


# =====================================================================
# PSO SWAP HARNESS
# =====================================================================
@mcp.tool()
def rdoc_pso_swap_harness(capture_path: str, pso_id: str,
                            new_shader_path: str,
                            out_dir: str = None,
                            right_eye_only: bool = True,
                            right_eye_min_x: float = 600.0) -> dict:
    """Generate a ready-to-paste C++ PSO swap harness for UEVR patch trials."""
    try:
        from automation import pso_swap_harness
        r = pso_swap_harness.generate(
            capture_path, pso_id, new_shader_path,
            out_dir=out_dir,
            right_eye_only=right_eye_only,
            right_eye_min_x=right_eye_min_x,
        )
        return _ok(**r)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


# =====================================================================
# CAPTURE EXPORT
# =====================================================================
@mcp.tool()
def rdoc_export_cpp(capture_path: str, out_dir: str) -> dict:
    """Generate a buildable C++ project from a D3D12 capture (Nsight
    'Generate C++ Capture' equivalent)."""
    try:
        from automation import export_cpp
        return _ok(report=export_cpp.export(capture_path, out_dir))
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


@mcp.tool()
def rdoc_index_capture(capture_path: str, out_dir: str) -> dict:
    """Walk every event and write JSONL tables (events, actions, state,
    bindings, shaders, resources, descriptors)."""
    try:
        from automation import index_capture
        n = index_capture.index_capture(capture_path, out_dir)
        return _ok(out_dir=out_dir, events_indexed=n)
    except Exception as e:
        return _err(e, trace=traceback.format_exc())


# =====================================================================
# Misc
# =====================================================================
@mcp.tool()
def rdoc_version() -> dict:
    """Return server / API version info."""
    info = {"server": "renderdoc-automation-mcp/0.1.0"}
    try:
        import renderdoc as rd  # noqa
        info["renderdoc_module"] = getattr(rd, "__file__", None)
    except ImportError:
        info["renderdoc_module"] = None
    return _ok(info=info)


@mcp.tool()
def rdoc_list_tools() -> dict:
    """List every MCP tool this server exposes."""
    tools = [n for n in globals() if n.startswith("rdoc_")
             and callable(globals()[n])]
    return _ok(tools=sorted(tools), count=len(tools))


# =====================================================================
# MAIN
# =====================================================================
def main():
    ap = argparse.ArgumentParser(description="RenderDoc Automation MCP server")
    ap.add_argument("--http", action="store_true",
                     help="run over HTTP/SSE (if FastMCP supports it)")
    args = ap.parse_args()
    try:
        if args.http:
            mcp.run(transport="sse")
        else:
            mcp.run()
    except KeyboardInterrupt:
        pass
    finally:
        manager().close_all()


if __name__ == "__main__":
    main()
