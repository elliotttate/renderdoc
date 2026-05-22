# Nsight Graphics MCP vs This RenderDoc Fork — Gap Analysis

Comparison of [`E:/Github/nsight-graphics-mcp`](file:///E:/Github/nsight-graphics-mcp)
(~252 MCP tools wrapping Nsight Graphics CLIs) against this RenderDoc fork's
automation toolkit (~40 reusable Python modules + 60 SN2 case-study scripts +
5 new C++ replay APIs + 2 renderdoccmd subcommands).

The goal: identify capabilities we should add for parity, and capabilities
we already have that Nsight MCP lacks.

---

## TL;DR

| Direction | Count |
|---|---|
| Nsight MCP has, we don't (HIGH value to add) | **10** |
| Nsight MCP has, we don't (medium / low) | **9** |
| Nsight MCP has, we don't (N/A — Nsight-internal RE) | **6** |
| We have, Nsight MCP doesn't | **7** |

**The single biggest gap**: this fork's automation isn't exposed as an
**MCP server**. Everything is callable from Python or `renderdoccmd`,
but an LLM agent can't drive it the way it can drive Nsight MCP.

**Our unique strengths**: replay-time GPU memory patching
(`SetBufferOverrideGPU`), full cross-API support (D3D12/D3D11/Vulkan/GL),
and an open-source driver layer we can modify end-to-end.

---

## HIGH-value gaps (worth implementing)

### 1. MCP server wrapper

| Nsight MCP | Us |
|---|---|
| Full FastMCP server exposing ~252 tools at `python -m nsight_graphics_mcp` | We have ~40 Python modules but they're library-only |

**Impact**: LLM agents (Claude, Cursor, etc.) can use the Nsight MCP via
JSON-RPC. Our toolkit requires writing Python scripts or shell-invoking
`renderdoccmd`. Wrapping our existing functions as MCP tools would make
the entire toolkit agent-accessible.

**What to build**:
- `rdoc_mcp/server.py` using FastMCP
- One `@mcp.tool` decorator per existing Python module function
- Auto-launch the RenderDoc replay controller as a long-running session
- Persistent capture handles + SQL-backed event index for fast queries

**Effort**: 2-3 days for a parity-equivalent MCP wrapping the existing modules.

### 2. SQLite-backed event index

| Nsight MCP | Us |
|---|---|
| `ngfx_event_query` / `ngfx_object_query` run **arbitrary SQL** against the indexed capture | `index_capture` emits JSONL, no SQL |

**Impact**: Interactive queries like "every Dispatch in the first 500 events
with a UAV write to resource X" are one-shot SQL with Nsight. In our toolkit
they need a Python script that parses + filters JSONL.

**What to build**:
- Modify `index_capture` to also emit a SQLite database (next to the JSONL)
- Add `event_query(sql)` and `object_query(sql)` functions
- Add `find_events` / `find_calls_by_arg` / `event_histogram` convenience wrappers

**Effort**: 1 day.

### 3. PSO swap harness + rehydration plan

| Nsight MCP | Us |
|---|---|
| `ngfx_pso_swap_harness_plan` + `ngfx_pso_rehydration_plan` generate ready-to-paste C++ that swaps a PSO at draw time with a patched shader | `pso_recreate` reconstructs the PSO desc but doesn't generate a swap harness |

**Impact**: For testing per-eye PSO patches in UEVR, you currently hand-write
the swap hook. The Nsight tool emits a C++ snippet directly.

**What to build**:
- `pso_swap_harness.py` module
- Input: PSO ResourceId + new shader bytecode path
- Output: a `.cpp` snippet using D3D12 APIs + a `.json` plan with shader
  byte offsets, root sig pointer expectations, etc.
- Plus a UEVR-specific variant emitting the hook structure UEVR consumes

**Effort**: 1 day.

### 4. Shader CRC32 indexing

| Nsight MCP | Us |
|---|---|
| `ngfx_pso_find_by_shader`, `ngfx_shader_blobs_find_crc32`, `ngfx_shader_blob_dump` — all CRC32-keyed | We have MD5 (RenderDoc-native) but only ad-hoc CRC32 (see `_sn2_extract_ps_dxbc.py`) |

**Impact**: UEVR uses CRC32-IEEE (zlib) over shader DXBC as its primary
shader identity. RenderDoc reports MD5. Every UEVR-targeted analysis we do
requires manual CRC32 computation.

**What to build**:
- `shader_index.py` module
- During capture indexing, compute and store: MD5, CRC32-zlib,
  CRC32-Castagnoli, ShaderToggler CRC for every shader blob seen
- Reverse lookup by any of those hashes
- Persistent shader-blob cache keyed by hash (so we don't rehash on every query)

**Effort**: 0.5 day.

### 5. Shader compilation integration

| Nsight MCP | Us |
|---|---|
| `ngfx_dxc_compile`, `ngfx_glslang_compile`, `ngfx_shader_disassembly_summary` | None (we only have `controller.DisassembleShader` for already-captured shaders) |

**Impact**: Patching a shader requires compiling. Currently you bash out to
`dxc.exe` manually. A wrapper would let agents compile from HLSL/GLSL with
the same toolchain consistency.

**What to build**:
- `shader_compile.py` thin wrappers over `dxc.exe` / `glslang.exe`
- Optional auto-discover bundled DXC from Windows SDK or local DXC install

**Effort**: 0.5 day.

### 6. Top-N cost analysis

| Nsight MCP | Us |
|---|---|
| `ngfx_top_n_costs` — top draws/dispatches by GPU time, with kind/name regex filters | `perf_counters.py` provides raw counter data but no top-N convenience |

**Impact**: "What are the 10 most expensive draws in this capture?" is a
single Nsight call. With us it requires running `perf_counters` then
sorting in pandas.

**What to build**:
- `top_n_costs.py` that calls `FetchCounters`, sorts by GPU time, filters
  by kind/regex, returns top-N

**Effort**: 0.5 day.

### 7. Doctor / health check

| Nsight MCP | Us |
|---|---|
| `ngfx_doctor` — verifies install, paths, layers, output dirs, GPU drivers, all in one call | No equivalent |

**Impact**: First-run "is everything wired correctly?" diagnostic. Catches
missing pyrenderdoc module, wrong Python version, missing capture
directory, etc.

**What to build**:
- `doctor.py` checking: Python version (3.6+), pyrenderdoc importable,
  qrenderdoc.exe locatable, write-permissions to temp dirs, optional dxc
  install, optional MSBuild for export_cpp builds

**Effort**: 0.5 day.

### 8. Fix-claim evidence framework

| Nsight MCP | Us |
|---|---|
| `ngfx_validate_fix_claim`, `ngfx_shader_fix_regression_score`, `ngfx_fix_attempt_log`, `ngfx_fix_claim_evidence_bundle` — formal pipeline for proving "this patch fixed the bug" | We have `replay_probe.Experiment` (multi-step driver) but no formal verdict scoring |

**Impact**: When validating a UEVR patch, currently you eyeball the
before/after screenshot. The Nsight framework runs a structured comparison
(LEFT-eye drift, RIGHT-eye delta, sequence preservation, repeatability)
and emits a verdict.

**What to build**:
- `fix_validator.py`:
  - Replay capture before-patch → store ROI hashes + per-event RT min/max
  - Replay capture after-patch → same
  - Compute: LEFT delta (should be ~0), RIGHT delta (should be large +
    move-toward-expected direction), event-sequence-identical (no missing
    work), repeatability (run 3× consistent)
  - Score 0-100, emit verdict + evidence-bundle ZIP

**Effort**: 1 day.

### 9. Resource resolver (find-by-anything)

| Nsight MCP | Us |
|---|---|
| `ngfx_resolve_handle` — find an object by handle/name/partial ID, bucket every event referring to it by role (create/bind/write/copy/etc.) | `resource_lineage` does similar but only by full ResourceId; doesn't handle name lookup |

**Impact**: "Find that texture called RenderTarget_Foo" requires walking
the resource list manually. Nsight does it in one call.

**What to build**:
- `resource_resolver.py` extending `resource_lineage`:
  - Match by ResourceId, name pattern (regex), partial ID, dimensions+format
  - Return: create call + per-role event lists

**Effort**: 0.5 day.

### 10. Event histogram + quick triage

| Nsight MCP | Us |
|---|---|
| `ngfx_event_histogram` (kind: draw / dispatch / copy / barrier / present / ...), `ngfx_quick_triage` (1-shot capture overview) | We have `index_capture` but no quick-triage tool |

**Impact**: First-look "what's in this capture?" is 10+ lines of Python
for us, one call for Nsight.

**What to build**:
- `quick_triage.py`: print capture summary (frame, API, draw count by kind,
  unique PSOs, unique resources, capture size)
- `event_histogram.py`: counts by action flag, by RT shape, by PSO

**Effort**: 0.5 day.

---

## MEDIUM-value gaps

### 11. Background process / launch management

| Nsight MCP | Us |
|---|---|
| `ngfx_launch_status` / `list_launches` / `launch_stop` — manage long-running tools spawned in background | We use Bash's `run_in_background` directly |

Not high priority — RenderDoc isn't a long-running tool stack like Nsight.

### 12. Capture summary / metadata extraction

| Nsight MCP | Us |
|---|---|
| `ngfx_capture_summary`, `ngfx_capture_screenshot` (extract embedded final-present), `ngfx_capture_logs` | Have `controller.GetFrameInfo()` but no convenience wrapper |

Light wrapper to add.

### 13. Capture-stream diff (LCS-based)

| Nsight MCP | Us |
|---|---|
| `ngfx_event_stream_diff` — deeper than name-only diff, runs LCS over per-event function streams + per-event arg diffs | `capture_diff.py` does action-count + binding-delta only |

Worth upgrading our capture_diff to LCS-based for better accuracy.

### 14. HDR-precision ROI diff

| Nsight MCP | Us |
|---|---|
| `ngfx_diff_hdr_roi` — PFM/raw-float diff without 8-bit clamping | `eye_image_diff` may clamp — needs verification |

Important for HDR scene-color comparison (which is what fog/water debugging needs).

### 15. Event-signature inference from metadata-only

| Nsight MCP | Us |
|---|---|
| `ngfx_eye_issue_event_signatures` — candidate L/R pairs from just metadata-functions output (no full capture load) | `eye_classifier` requires `SetFrameEvent` per event (expensive) |

We have `eye_classifier_fast` (pure chunk walk) but it's not as polished.

### 16. Producer graph

| Nsight MCP | Us |
|---|---|
| `ngfx_resource_producer_graph` — recursive read/write producer graph for named resources | `resource_lineage` is non-recursive |

Worth extending.

### 17. Eye-pair comparison report

| Nsight MCP | Us |
|---|---|
| `ngfx_compare_eye_passes` — L/R count deltas for draws/dispatches/copies/RT work | `compare_eyes` is more visual-output focused |

Different angle on same problem; worth adding a count-based variant.

### 18. UEVR trace import

| Nsight MCP | Us |
|---|---|
| `ngfx_import_uevr_trace` — NDJSON/JSON/CSV UEVR runtime hook traces into a SQLite DB | `uevr_ingest.py` handles JSON only |

Add NDJSON + CSV input modes.

### 19. CopyRect / write-history pair diff

| Nsight MCP | Us |
|---|---|
| `ngfx_resource_write_history_pair_diff`, `ngfx_copyrect_t0_resolution_report` — LEFT vs RIGHT resource-write timeline diff | Have per-resource history but no L/R-pair convenience |

Easy addition.

---

## Nsight MCP gaps that are N/A (Nsight-specific internal RE)

These exist because Nsight is closed-source and the MCP has to RE its
internals. We don't need them because we have the RenderDoc source.

| Nsight feature | Why N/A for us |
|---|---|
| Protobuf schema reference (`ngfx_proto_*`) | RenderDoc uses its own structured chunk format with full type info |
| `.ngfx-capture` format decoders | `.rdc` format is fully documented in our source |
| Pylon private bridge RE | Only needed because Nsight's saved-capture export is gated behind a UI dialog |
| `ngfx-rpc` PE-patch planner | We don't need to PE-patch closed binaries |
| ETW kernel-file capture of Nsight IPC | We control the RenderDoc IPC source |
| IDA Pro bridge for Nsight binaries | We don't RE RenderDoc binaries |

---

## What WE have that Nsight MCP doesn't

### 1. `SetBufferOverrideGPU` — true replay-time GPU memory patching ★★★

| Us | Nsight |
|---|---|
| Write bytes into captured GPU buffer at replay time; subsequent draws GPU-sample the patched bytes; rendered RT actually changes | Nsight's pixel-history is read-only; no in-place fix prototyping |

This is the single biggest capability gap *in our favor*. UEVR fix
validation is fundamentally easier with this — we can test
"will overriding CB[+880] = 1 fix the right-eye bug?" without rebuilding
UEVR.

### 2. `SetBufferOverride` — analysis-side patching

Patches reads via `GetBufferData()` / `GetCBufferVariableContents()`. Useful
for shader-debugger experiments. Nsight has nothing equivalent.

### 3. `GetDescriptorWrites` — driver-side normalized descriptor write log

Nsight has a similar timeline but requires building a C++ Capture project
first to index. Ours is direct from the structured file.

### 4. `replay_probe.Experiment` — multi-step replay mutation driver

Swap-resource / force-magenta-PS / force-PS-color-by-hash / skip-draw /
skip-dispatch / override-CBV-range / force-resource-clear / bind-neutral /
sample-ROI / min-max / pixel-history-at / compare-ROI — composed into
an experiment driver that captures before/after per step. Nsight has
isolated probes but no orchestrated mutation framework.

### 5. Cross-API support

RenderDoc works on D3D12, D3D11, Vulkan, OpenGL, Metal. Nsight is Nsight's
own driver-side capture layer (D3D12 + Vulkan).

### 6. Open source driver layer

We can modify capture-side code (e.g., add new chunk types, store custom
metadata at capture time). Nsight is binary-only.

### 7. `export_cpp` — Nsight's "Generate C++ Capture" equivalent, but open-source

We emit a buildable C++ project from a `.rdc` capture. Nsight's C++ export
requires a UI dialog or private RPC channel — that's most of the work that
makes Nsight's MCP so complex (Pylon bridge RE etc.). We don't have that
gating problem.

---

## Recommended implementation order

If the goal is to reach Nsight-MCP-feature-parity:

1. **MCP server wrapper** (2-3 days) — unlocks LLM-agent access to
   everything we already have
2. **SQLite event index** (1 day) — enables fast SQL queries from the MCP
3. **CRC32 shader indexing** (0.5 day) — UEVR alignment
4. **Doctor + quick-triage + event-histogram + top-N costs** (1 day) — first-time-user UX
5. **PSO swap harness generator + shader compilation wrappers** (1.5 days) — close the patch-prototyping loop
6. **Fix-claim evidence framework** (1 day) — formal verdict pipeline for UEVR patches
7. **Resource resolver + producer graph extension** (1 day) — better resource investigation UX

Total: **~9 days** to feature-parity, plus the unique advantages we keep
(`SetBufferOverrideGPU`, cross-API, OSS driver).

---

## What to build NEXT (highest-impact 3-day window)

If we have 3 days:

1. **Day 1**: MCP server scaffolding + wrap top 20 functions as
   `@mcp.tool` (index_capture, state_at_event, resource_lineage,
   eye_classifier, replay_probe.*, etc.)
2. **Day 2**: SQLite index + 6 SQL-query tools (`event_query`,
   `find_events`, `event_histogram`, `find_calls_by_arg`, `object_query`,
   `top_n_costs`)
3. **Day 3**: CRC32 shader indexing + PSO swap harness + fix-claim
   evidence bundle

After 3 days the toolkit will be:
- LLM-agent driveable via MCP
- SQL-queryable for ad-hoc analysis
- CRC32-aware for UEVR alignment
- Producing structured fix-claim evidence bundles

…which covers maybe 60% of the Nsight MCP's surface, with our existing
advantages (`SetBufferOverrideGPU` etc.) layered on top.

---

## Reference: full Nsight MCP tool inventory

~252 tools across these categories (line numbers from `README.md`):
- Activity drivers (5 tools)
- Headless capture + recapture (3)
- Capability report (1)
- Object index (8)
- Protobuf schema (5)
- Capture-format probing (3)
- `.ngfx-gfxcap` decoder (8)
- ngfx-rpc client (12)
- ETW kernel capture (4)
- PE-patch planner (2)
- Pylon private-bridge RE (12)
- Saved-capture → C++ autonomy (15)
- C++ Capture indexing + queries (12)
- Capture-stream diff (3)
- PSO/shader hash mapping (7)
- Shader visual-bug triage (11)
- Autonomous shader-fix loop (15)
- Resource lineage + write history (5)
- SN2 worked examples (~18)
- Frame cost (1)
- Aftermath / RPC / Remote (5)
- Layer install (2)
- NGFX in-app SDK (4)
- IDA bridge (6)
- Shader compilation (3)
- Discovery + escape hatch (8)
