# RenderDoc Fork — Feature Overview

This fork (branch `uevr-nsight-automation`) layers a comprehensive
**capture-analysis + replay-mutation toolkit** on top of upstream RenderDoc.
The goal: do the work that Nsight Graphics' GPU Trace, Frame Debugger, and
Generate-C++-Capture activities do, but from headless Python scripts driven
by the public `IReplayController` API — and add a few capabilities upstream
doesn't have at all (notably replay-time GPU buffer overrides for fix
validation).

It was built to debug Unreal Engine 5 stereo rendering bugs in VR-modded
games via UEVR (specifically Subnautica 2's right-eye fog/water/lighting
divergences), but the modules are game-agnostic.

---

## 1. New C++ Replay APIs

Five new `IReplayController` methods, fully wired through the D3D12 driver,
serialization layer, and Python bindings. Available from any RenderDoc
front-end (qrenderdoc Python, renderdoccmd, your own embedding code).

| API | Purpose |
|---|---|
| **`GetDescriptorWrites()`** → `rdcarray<DescriptorWriteRecord>` | Driver-recorded log of every descriptor write. Walks `GetStructuredFile()` and emits one record per affected slot. Destination (and source where applicable) already resolved to `(heap, slot)` via the chunk's `PortableHandle`. The headless equivalent of what Nsight's descriptor-heap timeline shows. |
| **`SetBufferOverride(buffer, offset, data)`** | Replay-side byte patch. `GetBufferData()` / `GetCBufferVariableContents()` / shader-debugger reads of this buffer return the patched bytes. **The shader's actual GPU sample doesn't change** — useful for analysis tooling and for the shader debugger but not for visual replay. |
| **`ClearBufferOverride(buffer)`** | Remove all replay-side patches for a buffer. |
| **`SetBufferOverrideGPU(buffer, offset, data)`** | **GPU-side** byte patch. Bytes are actually written into the captured GPU storage (UPLOAD heap + `CopyBufferRegion` on D3D12). Subsequent draws/dispatches at replay time GPU-sample the patched bytes — so the rendered image changes. Designed for **fix validation**: prototype a UEVR-side memcpy-into-a-cbuffer without rebuilding UEVR. |
| **`ClearBufferOverrideGPU(buffer)`** | Undo `SetBufferOverrideGPU` for a buffer. |

New data type:

```cpp
struct DescriptorWriteRecord
{
  uint32_t eventId;       // event at which the write was recorded
  ResourceId destHeap;
  uint32_t destSlot;
  ResourceId srcHeap;     // for Copy variants; otherwise null
  uint32_t srcSlot;
  DescriptorCategory category;  // SRV/UAV/CBV/Sampler
  ResourceId resourceId;        // resource the descriptor references (for Create*View)
  uint32_t writeKind;           // CopyDescriptors / CopyDescriptorsSimple / CreateShaderResourceView / etc.
};
```

### Code surface

| File | Change |
|---|---|
| `renderdoc/api/replay/data_types.h` | `+170` — new structs (`DescriptorWriteRecord`, override book-keeping) |
| `renderdoc/api/replay/renderdoc_replay.h` | `+85` — pure-virtual declarations + Python-binding decoration |
| `renderdoc/replay/replay_controller.{h,cpp}` | `+19` / `+240` — implementation, buffer-override book-keeping, `ApplyBufferOverrides` hook into `GetBufferData` |
| `renderdoc/replay/replay_driver.{h,cpp}` | `+11` / `+18` — driver-level `SetBufferOverrideGPU` plumbing |
| `renderdoc/driver/d3d12/d3d12_replay.{h,cpp}` | `+1` / `+95` — D3D12 implementation: UPLOAD heap + `CopyBufferRegion` |
| `renderdoc/driver/d3d12/d3d12_device_wrap.cpp` | `+8` — chunk-side hook for descriptor-write logging |

---

## 2. `renderdoccmd` subcommands

Two C++ headless analyzers wired into `renderdoccmd` so you don't need
qrenderdoc to run them.

| Subcommand | What it does |
|---|---|
| **`renderdoccmd index-capture <capture.rdc> <out_dir>`** | Walk every event in a capture and emit JSONL tables (events, actions, state snapshots, bindings, shaders, resources, descriptors). Output schema documented in `util/automation/README.md`. Fast — comparable to Nsight's headless decode-events. |
| **`renderdoccmd state-at-event <capture.rdc> <event_id>`** | Snapshot the full pipeline state at a specific event. Root signature, descriptor tables, bindings → resolved resources, viewport/scissor/RT, PSO + shader hashes. |

Implementation in `renderdoccmd/renderdoccmd_automation.cpp` (1,348 lines).

---

## 3. Python automation toolkit (`util/automation/`)

**100+ Python modules** built on the public RenderDoc Python API. Two layers:

### 3a. General-purpose analysis modules (~40 modules)

These are reusable across any D3D12 capture, any game.

#### Indexing & state introspection

| Module | Purpose |
|---|---|
| `index_capture` | Same as the C++ subcommand but driven from Python; useful when you want to filter/transform on the fly |
| `state_at_event` | Pipeline-state snapshot at an event (root sig, bindings, RT/DS, PSO/shaders, viewport) |
| `read_annotations` | Decode marker / debug-name chunks |
| `resource_lineage` | List all reads/writes of a resource; find last writer before an event; cross-reference to PSOs |
| `resource_export` | Export textures (DDS/PNG/EXR) or buffers (BIN) + per-channel min/max/mean stats |
| `barrier_history` | Per-resource state-transition timeline (`COMMON` → `RENDER_TARGET` → `PIXEL_SHADER_RESOURCE` …) |

#### Stereo / per-eye analysis

| Module | Purpose |
|---|---|
| `eye_classifier` | Classify draws/dispatches as LEFT/RIGHT eye by viewport.x / RT size / instanced-stereo slice |
| `eye_classifier_fast` | Pure-chunk-walk version (no `SetFrameEvent` — ~50× faster on large captures) |
| `eye_classifier_temporal` | Heuristic classifier for non-SBS stereo (consecutive groups of events sharing a viewport-size) |
| `pair_eye_events` | Auto-pair LEFT/RIGHT events by PSO + shader hash + RT identity |
| `compare_eyes` | End-to-end: pair events, run `event_diff` + `eye_image_diff` + `geometry_diff` (+ optional `shader_debug`), rank by divergence |
| `stereo_divergence` | Score per-event how much LEFT and RIGHT diverge (CB byte diff, RT min/max diff, geometry diff) |

#### Constant buffer / shader binding tools

| Module | Purpose |
|---|---|
| `cbv_tools` | Dump / decode / diff constant buffers at a given event + root parameter |
| `cbv_decode` | Decode CBVs into named struct fields via `GetCBufferVariableContents`; pairwise field diff |
| `descriptor_history` | Persistent per-(heap,slot) timeline derived by polling `GetDescriptors` at every event — analysis equivalent of driver-side `CopyDescriptors` tracking |
| `d3d12_copy_descriptors` | Direct dump of every `Device_CopyDescriptors[Simple]` and `Create*View` chunk from the SDFile |
| `descriptor_write_log` | Wraps `GetDescriptorWrites()` C++ API — concise CSV of every descriptor write in the capture |
| `descriptor_heap_heatmap` | Per-frame "hotness" map of which descriptor slots were written/read |

#### Pixel / draw / geometry investigation

| Module | Purpose |
|---|---|
| `pixel_lineage` | Pixel history with upstream-binding snapshot at the last writer (= "why does this pixel have THIS color?") |
| `debug_pixel_pair` | Side-by-side `DebugPixel` invocation on the same pixel at two different events |
| `geometry_diff` | `GetPostVSData` per stage + pairwise comparison (VS-input → VS-output → PS-input deltas) |
| `eye_image_diff` | Per-channel image diff of an RT between two events; PNG diff snapshots |
| `event_diff` | Pairwise diff of two events (shaders / bindings / viewports / RT / CBV bytes) |
| `capture_diff` | Diff two index directories: action counts, PSO usage, binding deltas, first divergent event |
| `shader_debug` | Wraps `DebugPixel` / `DebugThread`; side-by-side trace |
| `explain_pixel` | End-to-end report for a problematic pixel — runs pixel/eye/lineage/uevr/probe in sequence |

#### Replay-time mutation

| Module | Purpose |
|---|---|
| `replay_probe` | Replay-time mutation API — swap a resource for a bright-colored proxy, force a PS to output magenta, skip a draw/dispatch, override CBV range, force a clear, bind neutral textures, sample ROI, do per-pixel min/max, full `Experiment` driver that captures before/after ROI + pixel history per step. Backed by the new `SetBufferOverride` / `SetBufferOverrideGPU` APIs. |
| `fix_proposal` | Generate a UEVR-shaped patch proposal (CRC32 whitelist + slot list + override bytes) from a replay-probe run |

#### Diff & verification

| Module | Purpose |
|---|---|
| `diff_replay` | Replay two captures side-by-side and report per-event RT min/max delta + first event where outputs diverge |
| `pso_recreate` | Reconstruct a PSO's D3D12_PIPELINE_STATE_STREAM_DESC from the structured file |

#### Workflow / orchestration

| Module | Purpose |
|---|---|
| `launch_profile` | Deterministic launch profiles (exe, args, env, capture trigger) — `profiles/*.json` for repeatable game launches |
| `nonblocking` | Compatibility / debug-message report (capture-load doesn't pop modal dialogs in headless runs) |
| `automation_server` | Small HTTP / JSON-RPC service around the replay controller — lets external tools script analysis over the network |
| `perf_counters` | Per-event GPU counters via `FetchCounters` |
| `rdg_classifier` | Regex-based Unreal RDG pass classifier (`SingleLayerWater`, `TranslucentLightingVolume`, `ExponentialFog`, etc.) |
| `uevr_ingest` | Read UEVR sidecar JSON (manifest, status, eye samples, D3D12 diagnostics) and correlate to index events |

### 3b. `export_cpp` — Generate C++ Capture

Walk the SDFile and emit a buildable C++ project that reproduces the D3D12
call sequence:

```
out/
  main.cpp
  capture_frame.cpp
  CMakeLists.txt
  shaders/<hash>.cso
```

Covers device/list/queue chunks; auto-tracks CPU/GPU descriptor handles via
the integrated tracker; wires shader bytecode via `LoadBlob("shaders/<hash>.cso")`;
populates root sigs + PSO descs + view descs + barriers. Stubs anything that
hasn't been wired yet so the project still builds.

Headless equivalent of Nsight Graphics' "Generate C++ Capture" activity —
runs without an Nsight install.

Smoke test: `util/automation/_smoke_test_export_cpp.py`.

### 3c. SN2 investigation case study (`_sn2_*.py` — 60+ scripts)

A complete worked example: hunting Subnautica 2's right-eye fog/water/sky
visible bug through 6 phases of investigation. Each phase has 3-10 focused
scripts. Useful as a reference for how to compose the general-purpose
modules into an end-to-end debugging workflow.

| Phase | Focus | Scripts |
|---|---|---|
| 1. Triage | Quick-scan of the capture, dead-dispatch identification | `_sn2_quick_triage`, `_sn2_query_dead_dispatches`, `_sn2_identify_dead_shaders`, `_sn2_dead_uav_analysis` |
| 2. Root-cause drill (v1/v2/v3) | Three iterations narrowing the suspected bug surface | `_sn2_root_cause_drill[_v2/v3]`, `_sn2_target_buggy_draws`, `_sn2_check_right_basepass_mrt` |
| 3. Aliased-heap analysis | Identify placed-resource aliasing patterns | `_sn2_aliasing_chunk_scan`, `_sn2_aliasing_full_scan`, `_sn2_placed_resource_heap_map`, `_sn2_verify_aliasing_theory` |
| 4. UWE fog chain | Full producer/consumer trace + DXIL register usage | `_sn2_fog_dispatch_dump`, `_sn2_fog_dxil_disassemble`, `_sn2_fog_consumers`, `_sn2_verify_volfog_writers` |
| 5. Per-eye control flow | The eventual diagnosis — per-eye View CB byte divergence | `_sn2_view_cb_byte_diff`, `_sn2_view_cb_post_2408_mapping`, `_sn2_compare_141352_141371`, `_sn2_compare_tonemap_inputs`, `_sn2_control_flow_investigation`, `_sn2_find_underwater_flag`, `_sn2_override_underwater_flag_test` |
| 6. Final compositor + backbuffer trace | Trace what pixels reach the swapchain | `_sn2_trace_140984_to_present`, `_sn2_final_compositor_trace`, `_sn2_left_only_outputs[_v2]` |

Headline finding: the bug is a `1`-vs-`0` uint32 at View CB offset +880 on
the right-eye CB upload — UE5's stereo path leaves a per-eye flag at zero
on RIGHT that's set on LEFT. Three rounds of shared-resource data
substitution all fired correctly but failed to fix the visible bug because
the divergence is in a shader branch decision, not in the sampled data.

---

## 4. qrenderdoc additions

### Configuration extensions

`qrenderdoc/Code/Interface/PersistantConfig.h` adds:

- `AutomationSuppressIncompatModals` — when set, suppresses the
  "incompatible capture" warning dialog on capture load. Required for
  headless / scripted analysis runs.

---

## 5. Quick start

### From Python (qrenderdoc's embedded interpreter)

```python
import sys
sys.path.insert(0, r"E:/Github/renderdoc")  # path to this repo
from util.automation import _lib
import renderdoc as rd

cap, controller = _lib.open_capture(r"path/to/capture.rdc")

# Use the new APIs:
writes = controller.GetDescriptorWrites()
print(f"{len(writes)} descriptor writes in capture")

# Override a CBuffer at replay time so the GPU sees patched bytes:
view_cb = ... # ResourceId
controller.SetBufferOverrideGPU(view_cb, offset=880, bytes_to_write=b"\x01\x00\x00\x00")
controller.SetFrameEvent(target_eid, True)
# ... now subsequent draws sample the patched bytes
controller.ClearBufferOverrideGPU(view_cb)

controller.Shutdown()
cap.Shutdown()
```

### From `renderdoccmd`

```bash
# Index a capture (all events + bindings + state) to JSONL
renderdoccmd index-capture capture.rdc out/

# Snapshot state at one event
renderdoccmd state-at-event capture.rdc 14845 > state.json
```

### Running an automation script

Most `util/automation/_*.py` scripts are designed to be driven by
qrenderdoc's `--python` flag:

```bash
qrenderdoc.exe --python util/automation/_sn2_view_cb_byte_diff.py
```

They use `_lib.open_capture(path)` and write logs/JSON under
`E:/tmp_dir/...` by default (adjust paths inside the script). Headless —
no UI shown.

---

## 6. Building

Same as upstream RenderDoc. The new files are picked up automatically by
the CMake / vcxproj wiring already in this branch:

```bash
# Visual Studio 2022:
cmake -B build -G "Visual Studio 17 2022" -A x64
cmake --build build --config Development
```

The Python automation toolkit requires no build step — it imports
`renderdoc` (the Python module that ships with qrenderdoc / pyrenderdoc)
at runtime.

---

## 7. What's NOT changed from upstream

- The replay drivers for Vulkan, OpenGL, D3D11 — only D3D12 has the new
  `SetBufferOverrideGPU` implementation. Vulkan/GL/D3D11 have stub
  versions that return `false`.
- The capture-side code path. All new functionality is replay-side
  analysis + mutation. Captures from upstream RenderDoc are fully
  compatible with this fork (and vice versa).
- The qrenderdoc UI's pipeline-state / texture viewer / shader debugger
  panels. They benefit automatically from `SetBufferOverride` (analysis
  side) but no new UI controls were added.

---

## 8. License

Same as upstream RenderDoc — MIT. All changes are also MIT.
