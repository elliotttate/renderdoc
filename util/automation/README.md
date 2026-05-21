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
```

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

Shader hash is SHA-1 of `ShaderReflection.rawBytes`. RenderDoc does not expose a
stable per-shader hash directly, so this matches what Nsight/PIX automation
typically uses.

## What's not yet implemented

The modules above cover the Python-API-tractable parts of the roadmap.
The following items need driver-internal changes and are tracked as
follow-ups:

- Persistent descriptor *write history* over time (`CopyDescriptors`,
  `CopyDescriptorsSimple`) — `state-at-event` today snapshots per event,
  which is sufficient for most workflows but doesn't show how a slot's
  contents evolved. Driver work: `renderdoc/driver/d3d12/d3d12_manager.cpp`.
- "Nonblocking incompatibility" suppression at the layer/UI boundary
  beyond what `ReplayOptions` already exposes.
- The full set of qrenderdoc panels listed in §20.
