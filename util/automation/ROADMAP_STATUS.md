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
| §16 | Replay-time mutation probes | `util/automation/replay_probe.py` |
| §17 | UEVR integration | `util/automation/uevr_ingest.py` |
| §22 M1-M5 | Headless index, descriptor resolution, shader/PSO registry, resource lineage, eye-aware analysis | All shipped |
| §22 M6 | Patch verification | UEVR ingest + override-fired recipe |
| §22 M7 | Replay mutation | `replay_probe.ProbeSession` (swap_resource, force_magenta_ps) |
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

## Not implemented in this branch

These items require deeper changes — mostly in qrenderdoc UI — that don't
fit on top of the public API alone.

| Roadmap § | Feature | Notes |
| --- | --- | --- |
| §4 (driver-side log) | Native `CopyDescriptors` write log emitted by the driver | The driver *already serialises* these chunks (`d3d12_device_wrap.cpp` lines 2169 and 2238); `d3d12_copy_descriptors` reads them from the SDFile. A dedicated replay-controller method exposing a normalised list would be a small follow-up — not required for analysis. |
| §20 | qrenderdoc UI panels (Explain selected draw, Resource lineage graph, Compare with other eye, etc.) | These are Qt UI work in `qrenderdoc/Windows/`; the data already exists via the automation modules, so panels become thin views over the JSONL outputs |
| §22 M7 (advanced) | Re-running pixel history after mutation, multi-mutation experiments | `replay_probe.ProbeSession` supports the primitives but doesn't yet drive multi-step experiments programmatically; trivial to extend |
| §18 row "Modal suppression at profile level" | Profile-level "never block on incompatibility" | RenderDoc's `ReplayOptions` covers most paths; UI-level modals only appear in qrenderdoc, not in `renderdoccmd`-driven flows |

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

# Smoke test (uses the existing D3D12_Descriptor_Indexing demo)
cd util/test
python run_tests.py --filter D3D12_Index_Capture
```
