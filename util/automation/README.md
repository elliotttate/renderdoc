# util/automation

UEVR / Nsight-style automation built on top of the RenderDoc Python API.
Implements the workflows described in `docs/UEVR_NSIGHT_AUTOMATION_ROADMAP.md`
without depending on Nsight, generated C++ captures, or ad-hoc scripts.

## Modules

| Module | Roadmap section | Purpose |
| --- | --- | --- |
| `index_capture` | §3, §19, §22 (M1) | Walk every event, emit JSONL tables (events, actions, state, bindings, shaders, resources) |
| `state_at_event` | §4, §22 (M1-M2) | Snapshot pipeline state at a specific event (root signature, descriptors, bindings → resources) |
| `resource_lineage` | §10, §22 (M4) | List all reads/writes of a resource, find last writer before an event |
| `pixel_lineage` | §9 (M4) | Pixel history with upstream-binding snapshot at the last writer |
| `eye_classifier` | §8 (M5) | Classify draws/dispatches as left/right eye using viewport/RT size/stereo slice |
| `capture_diff` | §11 (M6) | Diff two index directories: action counts, PSO usage, binding deltas, first divergent event |
| `uevr_ingest` | §7, §17 (M6) | Read UEVR sidecar JSON and correlate to index events |
| `cbv_tools` | §13 | Dump/decode/diff constant buffers at a given event/slot |
| `resource_export` | §14 | Export textures (DDS/PNG/EXR) or buffers (BIN) + per-channel statistics |
| `replay_probe` | §16 (M7) | Replay-time mutation probes (swap resource, magenta-PS, ROI sample) |
| `launch_profile` | §1 (M1) | Deterministic launch profiles (exe, args, env, capture trigger) |
| `explain_pixel` | §24 (acceptance) | End-to-end report for a problematic pixel |
| `rdg_classifier` | §12 | Regex-based Unreal RDG pass classifier |
| `automation_server` | §15 | Small HTTP/JSON-RPC service around the replay controller |
| `nonblocking` | §2 | Compatibility / debug-message report (no modal blocking) |
| `sn2_workflows` | §23 | Concrete SN2 debugging recipes (pso-events, override-fired, first-bad-input, t9-vs-t5, view-cbv-diff) |
| `descriptor_history` | §4 (full) | Per-(heap,slot) timeline derived by polling `GetDescriptors` at every event — equivalent to driver-side CopyDescriptors tracking for analysis |
| `d3d12_copy_descriptors` | §4 (driver-recorded) | Direct dump of every `Device_CopyDescriptors[Simple]` and `Create*View` chunk from the structured file |
| `event_diff` | Nsight Range Compare | Pairwise diff of two events (shaders / bindings / viewports / RT / CBV bytes) |
| `pair_eye_events` | Nsight Stereo | Auto-pair left/right events by PSO + shader hashes + RT |
| `shader_debug` | Nsight Shader Profiler | Wrap `DebugPixel` / `DebugThread`; side-by-side compare same pixel on two events |
| `eye_image_diff` | Nsight Image Compare | Per-channel image diff of an RT between two events; optional left/right PNG snapshots |
| `geometry_diff` | Nsight Geometry Pipeline | `GetPostVSData` per stage + pairwise comparison |
| `barrier_history` | Nsight State Tracking | Per-resource state-transition timeline |
| `perf_counters` | Nsight Range Profiler | Per-event GPU counters via `FetchCounters` |
| `cbv_decode` | Nsight CBV Decoder | Decode CBVs into named struct fields via `GetCBufferVariableContents` + pairwise field diff |
| `compare_eyes` | end-to-end | Pair events, run event_diff + image_diff + geometry_diff (+ optional shader_debug) on each, rank by divergence |
| `export_cpp` | Nsight "Generate C++ Capture" | Walk the SDFile and emit a buildable C++ project (`main.cpp`, `capture_frame.cpp`, `CMakeLists.txt`, `shaders/<hash>.cso`) that reproduces the D3D12 call sequence. Covers device/list/queue chunks; stubs anything that needs CPU/GPU descriptor handle tracking |

The same functionality is exposed by the C++ subcommands in `renderdoccmd`:
`renderdoccmd index-capture`, `renderdoccmd state-at-event`, etc.

## Running

The Python modules need `import renderdoc` to work. Two supported paths:

1. **Via qrenderdoc's embedded Python**: `Tools -> Python Shell` or `Tools -> Run Script`.
2. **Via standalone Python**: ensure the directory containing `renderdoc.pyd`
   (Windows) or `renderdoc.so` (Linux) is on `PYTHONPATH`. This is usually the
   same directory as `qrenderdoc.exe` / `renderdoccmd.exe`.

```sh
# Windows example (PowerShell)
$env:PYTHONPATH = "C:\Program Files\RenderDoc"
python -m util.automation.index_capture C:\captures\sn2.rdc --out C:\captures\sn2.rdc.index

python -m util.automation.state_at_event C:\captures\sn2.rdc --event 16042
python -m util.automation.eye_classifier  C:\captures\sn2.rdc --out C:\captures\sn2.eye.json
python -m util.automation.capture_diff    C:\captures\before.index C:\captures\after.index --out diff.json
python -m util.automation.explain_pixel   C:\captures\sn2.rdc --x 900 --y 250 --probe

# C++ code export (Nsight-style "Generate C++ Capture")
python -m util.automation.export_cpp      C:\captures\sn2.rdc --out C:\captures\sn2_cpp
```

### export_cpp

Generates a buildable C++ project from a `.rdc` capture::

    <out_dir>/
        main.cpp              # creates an ID3D12Device and calls RecordCapture()
        capture_frame.cpp     # the recorded D3D12 call sequence
        capture_frame.h       # shared declarations
        CMakeLists.txt
        README.md
        shaders/<hash>.cso    # extracted shader bytecode
        unhandled.txt         # one line per chunk the exporter didn't handle

What's covered: every common D3D12 chunk that participates in the per-frame
draw/dispatch flow — device creation (CommandQueue, Allocator, List,
DescriptorHeap, RootSignature, PSOs, CommittedResource/Heap/Fence), command
list recording (set state, draws, dispatches, clears, copies, resolve,
discard, ExecuteIndirect, DispatchMesh, DispatchRays, marker), and queue
execution (ExecuteCommandLists, Signal, Wait). Mesh shader (`DispatchMesh`),
VRS (`RSSetShadingRate`), and depth bounds (`OMSetDepthBounds`) are all
covered via `QueryInterface`.

What's stubbed: anything that requires CPU/GPU descriptor handle tracking
(view creation calls, `Set{Graphics,Compute}RootDescriptorTable`,
`OMSetRenderTargets`, `Clear{RenderTarget,DepthStencil}View`), nested-struct
reconstruction (`ResourceBarrier` array, `CopyTextureRegion`), pipeline
state stream PSOs, ray tracing dispatches, and root signature blob loading.
Each stub is a `/* TODO ... */` comment so the output still compiles
structurally. The full list of stubbed chunks for any given capture is in
`unhandled.txt`.

## Output schema (summary)

`index_capture` emits this layout, which everything else consumes:

```
<capture.rdc>.index/
  meta.json
  resources.json
  events.jsonl             one row per event (eid, name, flags, parent, children)
  actions.jsonl            one row per draw/dispatch/copy/clear/resolve/etc.
  state.jsonl              per-event pipeline snapshot (root sig, descriptors, bindings)
  shaders/<hashprefix>/<hash>.bin     raw DXIL/DXBC
  shaders/<hashprefix>/<hash>.json    reflection summary (constant blocks, registers, spaces)
  uevr.jsonl               (added by uevr_ingest) eye + override correlation per event
```

Shader hash is MD5 of `ShaderReflection.rawBytes`. RenderDoc does not expose a
stable per-shader hash directly, so this matches what Nsight/PIX automation
typically uses, and the C++ `renderdoccmd index-capture` subcommand emits the
same hashes by sharing the `3rdparty/md5/` implementation.

## What's not yet implemented

The Python modules above cover everything in the roadmap that can be done on
top of the public replay API and structured file. Remaining items:

- The full set of qrenderdoc Qt panels listed in §20. See
  `qrenderdoc/Windows/AutomationDock*` for the scaffolding shipped alongside
  these scripts. The data is all available; the panels are thin views.
- Profile-level modal suppression (§18) for qrenderdoc — `ReplayOptions`
  covers replay-side suppression but the Qt UI still surfaces some modals.
- Driver-side instrumentation work that *isn't* needed for analysis any more
  but would still be useful for low-level inspection (e.g. per-CopyDescriptors
  call-stacks). The combination of `descriptor_history.py` (consumed-state
  timeline) + `d3d12_copy_descriptors.py` (raw chunk log) already gives full
  coverage of every descriptor mutation that touches a draw.
