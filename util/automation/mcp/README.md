# RenderDoc Automation MCP Server

LLM-agent-driveable MCP wrapper around `util/automation`. Exposes every
analysis + replay-mutation tool in this fork as an `@mcp.tool` callable
over stdio JSON-RPC.

## Install

```bash
pip install mcp        # the MCP server framework (FastMCP)
```

The `renderdoc` Python module must be importable. Easiest path: run the
server from `qrenderdoc.exe --python`, which has the binding bundled.

## Run

```bash
# Stdio (Claude Code / Cursor / Continue etc.):
python -m util.automation.mcp.server

# Or via qrenderdoc's embedded Python (no separate pyrenderdoc needed):
qrenderdoc.exe --python util/automation/mcp/server.py
```

## Configure in Claude Code / Cursor

```json
{
  "mcpServers": {
    "renderdoc-automation": {
      "command": "qrenderdoc.exe",
      "args": [
        "--python",
        "E:/Github/renderdoc/util/automation/mcp/server.py"
      ]
    }
  }
}
```

## Tools

| Tool | Purpose |
|---|---|
| `rdoc_session_open` | Open a .rdc and keep a persistent replay-controller handle |
| `rdoc_session_list` / `rdoc_session_close` | Manage open sessions |
| `rdoc_doctor` | Health check — verify Python / pyrenderdoc / qrenderdoc / dxc / glslang |
| `rdoc_quick_triage` | First-look "what's in this capture?" |
| `rdoc_event_histogram` | Group events by kind / pso / ps_entry / rt_shape / viewport_x / … |
| `rdoc_sqlite_build` | Build the SQLite event index for fast SQL queries |
| `rdoc_sqlite_sql` | Run a read-only SQL query against the index |
| `rdoc_find_events` | Common filtered queries (kind / pso / eye / vp_x_min / …) |
| `rdoc_resource_resolve` | Find resources by name / handle / partial / dimensions+format |
| `rdoc_resource_lineage` | Per-resource event timeline by role |
| `rdoc_shader_index_build` | Build CRC32 + MD5 shader index |
| `rdoc_shader_find` | Reverse-lookup by CRC32 / MD5 / entry point |
| `rdoc_shader_compile_hlsl` | dxc.exe wrapper |
| `rdoc_shader_compile_glsl` | glslang wrapper |
| `rdoc_shader_disassemble` | `dxc -dumpbin` |
| `rdoc_top_n_costs` | Top-N expensive draws by GPU time / IA / PS / VS / CS |
| `rdoc_state_at_event` | Full pipeline state snapshot |
| `rdoc_descriptor_writes` | Driver-side normalized descriptor write log (new C++ API) |
| `rdoc_set_buffer_override_gpu` | **Patch GPU memory at replay time** (new C++ API ★) |
| `rdoc_clear_buffer_override_gpu` | Undo a GPU override |
| `rdoc_set_buffer_override` | Analysis-side patch (shader-debugger sees patched bytes) |
| `rdoc_eye_classify` | LEFT/RIGHT eye classification |
| `rdoc_pair_eye_events` | Auto-pair LEFT/RIGHT events by PSO+hash+RT |
| `rdoc_capture_diff` | Diff two captures (action counts / PSO usage / bindings) |
| `rdoc_diff_replay` | Replay-time RT min/max delta + first divergent event |
| `rdoc_fix_validator` | **PASS / WEAK-PASS / FAIL verdict pipeline for fix claims** |
| `rdoc_pso_swap_harness` | Generate ready-to-paste C++ PSO swap harness |
| `rdoc_export_cpp` | Nsight's "Generate C++ Capture" equivalent |
| `rdoc_index_capture` | Walk all events → JSONL tables |
| `rdoc_version` | Server / pyrenderdoc version |
| `rdoc_list_tools` | Enumerate every tool the server exposes |

## Typical agent workflow

```
1. rdoc_doctor                                 # verify environment
2. rdoc_quick_triage(capture)                  # what's in this capture?
3. rdoc_sqlite_build(capture)                  # index for SQL queries
4. rdoc_sqlite_sql(capture, "SELECT pso_id, COUNT(*) FROM events
                              WHERE kind='draw' AND eye='right'
                              GROUP BY pso_id ORDER BY 2 DESC LIMIT 10")
5. rdoc_resource_resolve(capture, name_regex='Translucent.*')
6. rdoc_shader_index_build(capture)
7. rdoc_shader_find(idx, by_crc32_zlib=0x8733F2E0)
8. rdoc_session_open(capture)
9. rdoc_set_buffer_override_gpu(session, buf, 880, "AQAAAA==")  # b'\x01\x00\x00\x00'
   # → replay re-runs with patched bytes; rendered RT changes
10. rdoc_fix_validator(before=baseline, after=patched, …)
```
