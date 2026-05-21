# UEVR / Nsight Automation — Roadmap Implementation Status

This tracks which roadmap items (`docs/UEVR_NSIGHT_AUTOMATION_ROADMAP.md`) are
implemented in this branch, and what remains.

## Implemented in this branch

| Roadmap § | Feature | Implementation |
| --- | --- | --- |
| §1 | Deterministic launch profiles | `util/automation/launch_profile.py`, `profiles/*.json` |
| §2 | Nonblocking capture/replay | `util/automation/nonblocking.py` (compat report); RenderDoc's own `ReplayOptions` already covers most modal suppression |
| §3 | D3D12 command argument index | `renderdoccmd index-capture`, `util/automation/index_capture.py` |
| §4 | Descriptor heap timeline / state-at-event | `renderdoccmd state-at-event`, `util/automation/state_at_event.py` |
| §4 (full) | Driver-side normalized descriptor write log | `IReplayController::GetDescriptorWrites()` (declared in `renderdoc/api/replay/renderdoc_replay.h`, implemented in `renderdoc/replay/replay_controller.cpp`, with Python bindings via the `DescriptorWriteRecord` array template instantiation). Walks `GetStructuredFile()` and emits one `DescriptorWriteRecord` per affected slot, with destination (and where applicable, source) already resolved to `(heap, slot)` via the `PortableHandle` form serialised in the chunk. |
| §5 | PSO stream support | Already handled by RenderDoc's D3D12 driver (`d3d12_device_wrap2.cpp::CreatePipelineState`); exposed via `D3D12Pipe::State.pipelineResourceId` |
| §6 | Shader registry / reflection | `index_capture` exports `shaders/<hash>/<hash>.{bin,json}` |
| §7 | Shader patch verification | `util/automation/uevr_ingest.py` (manifest correlation), `sn2_workflows.override-fired` |
| §8 | Eye-aware event classifier | `util/automation/eye_classifier.py` |
| §9 | Pixel writer / composite lineage | `util/automation/pixel_lineage.py` |
| §10 | Resource producer/consumer lineage | `util/automation/resource_lineage.py` (uses `controller.GetUsage`) |
| §11 | Capture diffing | `util/automation/capture_diff.py` (diffs two index dirs) |
| §12 | UE RDG awareness | `util/automation/rdg_classifier.py` |
| §13 | Constant buffer tools | `util/automation/cbv_tools.py` |
| §14 | Resource export + stats | `util/automation/resource_export.py` |
| §15 | Automation server | `util/automation/automation_server.py` (small HTTP/JSON service) |
| §16 | Replay-time mutation probes | `util/automation/replay_probe.py` (swap_resource, force_magenta_ps, force_ps_output_color_by_hash, skip_draw, skip_dispatch, override_cbv_range, force_resource_clear, bind_neutral, sample_roi/min_max/pixel_history_at/compare_roi + `Experiment` multi-step driver). `override_cbv_range` now applies through the new `IReplayController::SetBufferOverride()` driver API so the shader debugger and any tool that reads the CBV via `GetBufferData`/`GetCBufferVariableContents` sees the patched bytes. |
| §17 | UEVR integration | `util/automation/uevr_ingest.py` (manifest, status, eye samples, D3D12 diagnostics) |
| §22 M1-M5 | Headless index, descriptor resolution, shader/PSO registry, resource lineage, eye-aware analysis | All shipped |
| §22 M6 | Patch verification | UEVR ingest + override-fired recipe |
| §22 M7 | Replay mutation | `replay_probe.ProbeSession` full mutation set + `Experiment` driver that captures before/after ROI + pixel history per step |
| §23 | SN2 workflows | `util/automation/sn2_workflows.py` (pso-events, override-fired, first-bad-input, t9-vs-t5, view-cbv-diff) |
| §24 | Acceptance "explain a pixel" | `util/automation/explain_pixel.py` (chains pixel/eye/lineage/uevr/probe) |
| §25 | Practical first step (`renderdoccmd index-capture` etc.) | Implemented in C++ in `renderdoccmd_automation.cpp` + wired in `renderdoccmd.cpp` |

## Nsight-equivalent stereo/eye debugging modules added later

| Module | Nsight feature it mirrors |
| --- | --- |
| `descriptor_history` | Persistent descriptor heap change log over time (polled equivalent of CopyDescriptors tracking) |
| `d3d12_copy_descriptors` | Direct extraction of `Device_CopyDescriptors[Simple]` + `Create*View` chunks from the structured file (the driver already records them) |
| `event_diff` | Range Compare — pairwise diff of two events |
| `pair_eye_events` | Stereo pair builder |
| `shader_debug` | Shader Profiler / pixel debugger (with side-by-side compare) |
| `eye_image_diff` | Image compare between two RTs |
| `geometry_diff` | Geometry pipeline inspector (PostVS) |
| `barrier_history` | Resource state / barrier tracking |
| `perf_counters` | Range profiler (event GPU duration etc.) |
| `cbv_decode` | Constant buffer variable decoder + per-field diff |
| `compare_eyes` | End-to-end "right eye looks wrong" triage that chains pair_eye_events + event_diff + image_diff + geometry_diff and ranks pairs by divergence |
| `descriptor_write_log` | Unified timeline that merges `d3d12_copy_descriptors` (raw chunk log) and `descriptor_history` (resolved per-event state) into a single chronological view |

## qrenderdoc UI (§20)

The §20 panels are shipped as a qrenderdoc Python extension in
`util/extensions/uevr_automation/`. Install it by symlinking the folder
into `%APPDATA%\qrenderdoc\extensions\` (or by setting the
`RENDERDOC_AUTOMATION_DIR` env var) and enabling **UEVR / Nsight Automation**
in `Tools > Manage Extensions`. Menu items appear under `Tools > Automation`:

- Explain Selected Draw, Descriptor Table at Event
- Resource Lineage… , Compare With Other Eye
- Find Final Writer for Pixel… , Events Using This Shader…
- Events Reading / Writing This Resource…
- Export Event State to JSON… , Copy Root Binding Summary
- Replay-Time Probe… , Multi-Step Experiment…

## Modal suppression (§18)

`PersistantConfig::AutomationSuppressIncompatModals` (added in
`qrenderdoc/Code/Interface/PersistantConfig.h`) silences the
`SuggestRemoteDialog` / replay-incompat modals during automated flows. Launch
profiles can request this per-run via the JSON field
`suppress_incompat_modals: true` — `launch_profile.py` patches the qrenderdoc
config file before invoking the target. Fatal modals (truly unsupported
drivers, file-not-found) are unaffected by design.

## Not implemented in this branch

The remaining items would require a development build/test cycle to land
safely:

| Roadmap § | Feature | Notes |
| --- | --- | --- |
| §16 (GPU-side CBV injection) | Modify the GPU's view of the buffer itself | `IReplayController::SetBufferOverride()` layers the override on top of `GetBufferData` / `GetCBufferVariableContents` results, so the shader debugger and any analysis tool sees the patched bytes. Modifying what the *real* GPU reads during replay (so visible pixels reflect the patch) still requires a `ReplaceResource` shader swap that rewrites the consuming shader. |

## How to verify

```sh
# Build the C++ subcommands
msbuild renderdoc.sln -p:Configuration=Development -p:Platform=x64 -p:PlatformToolset=v143

# Headless index + descriptor resolver
x64\Development\renderdoccmd.exe index-capture path\to\cap.rdc --out cap.index
x64\Development\renderdoccmd.exe state-at-event path\to\cap.rdc --event 16042

# Python equivalents (same JSON shape)
python -m util.automation.index_capture  path\to\cap.rdc --out cap.index
python -m util.automation.state_at_event path\to\cap.rdc --event 16042

# Higher-level workflows
python -m util.automation.eye_classifier path\to\cap.rdc --out eye.json
python -m util.automation.pixel_lineage  path\to\cap.rdc --x 900 --y 250
python -m util.automation.capture_diff   before.index after.index
python -m util.automation.sn2_workflows pso-events path\to\cap.rdc --shader-hash 166dba88
python -m util.automation.explain_pixel  path\to\cap.rdc --x 900 --y 250 --probe

# §4 deep-dive: descriptor write timeline (chunk log + per-event state merged)
python -m util.automation.descriptor_write_log path\to\cap.rdc --out writelog.json

# §16 advanced probes (§22 M7)
python -m util.automation.replay_probe color-by-hash path\to\cap.rdc --hash 166dba88 --color 1 0 1 1 --event 16042
python -m util.automation.replay_probe skip-draw     path\to\cap.rdc --event 16042
python -m util.automation.replay_probe experiment    path\to\cap.rdc --steps steps.json --out exp.json

# §18 modal suppression (launch profile flag)
# add  "suppress_incompat_modals": true  to your profile JSON; launch_profile.py
# will patch qrenderdoc's UI.config before invoking the target.

# §20 qrenderdoc panels — install the extension:
#   symlink util/extensions/uevr_automation -> %APPDATA%/qrenderdoc/extensions/
#   Tools > Manage Extensions, tick "UEVR / Nsight Automation", "Always Load"

# Smoke test (uses the existing D3D12_Descriptor_Indexing demo)
cd util/test
python run_tests.py --filter D3D12_Index_Capture
```
