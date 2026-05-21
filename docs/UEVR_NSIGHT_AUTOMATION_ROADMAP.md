# RenderDoc Roadmap for UEVR and Nsight-Style Automation

This is a local design note for building the capabilities we keep needing while
debugging stereo rendering issues. It is not written as an upstream RenderDoc
proposal. The goal is a private RenderDoc fork or extension set that can replace
the current mix of UEVR logs, Nsight Graphics captures, generated C++ captures,
ad hoc Python parsers, and manual UI inspection.

The short version: RenderDoc should become the source of truth for "what exactly
happened on the GPU for this eye, this pixel, this resource, and this shader",
and it should expose that truth through a stable scriptable API.

## Goals

1. Launch and capture the target in repeatable modes.
2. Identify left/right eye work from viewport, scissor, render-target, stereo
   array slice, or UEVR state.
3. Index every D3D12 command with exact argument values.
4. Resolve descriptor handles to concrete resources at any draw or dispatch.
5. Trace resource producer and consumer chains across passes.
6. Inspect and diff shader bytecode, root signatures, PSOs, CBVs, SRVs, UAVs,
   RTVs, DSVs, barriers, and dispatch dimensions.
7. Run pixel-history and resource-lineage queries from automation without UI
   clicks.
8. Verify that shader overrides or runtime hooks are actually active.
9. Produce reproducible artifacts: screenshots, JSON reports, SQLite indexes,
   resource dumps, shader disassembly, and concise failure summaries.

## Current Pain Points

### Nsight C++ Capture Is Too Fragile

Nsight's generated C++ capture is useful because it exposes command arguments
as source code that can be indexed. The problem is that the workflow is brittle:

- Saved-capture to C++ export is version dependent.
- Some Nsight versions require replaying a saved capture through
  `ngfx-replay.exe`.
- Incompatibility popups block automation unless every capture and replay path
  passes the right flags.
- The replay can finish before the C++ activity attaches.
- The C++ export may crash or silently fail.
- Pipeline-state-stream PSOs need extra parsing to recover shader bytecode and
  root signature data.

RenderDoc already captures the command stream. We need the same useful data
without depending on an external C++ generator.

### UEVR State Is Separate From GPU State

UEVR can tell us about game-side hooks, shader override status, stereo mode,
and per-eye runtime data. RenderDoc can tell us the GPU state. Right now those
two worlds are correlated manually by timestamps, event IDs, screenshots, and
guesswork.

We need capture metadata channels that can ingest UEVR state and attach it to
RenderDoc events:

- current eye or stereo pass
- active shader override hashes
- whether a shader replacement fired
- UEVR config flags
- per-frame eye sample metrics
- user-defined markers from hooks
- game build and command line

### "No Visual Change" Needs a Mechanical Answer

When a patch produces no visual change, we need immediate answers:

- Did the process launch with the expected DLLs and environment?
- Did the target draw still run?
- Did the target shader bind?
- Did the replacement shader compile and bind?
- Did the replacement draw affect the visible pixels?
- Did the final copy/composite overwrite the changed result?
- Did we patch the wrong eye because event order differs from viewport order?

That should be one report, not a manual investigation.

## Required Feature Areas

## 1. Deterministic Launch Profiles

RenderDoc should have launch profiles that can be committed to disk and reused.
For our workflow they need:

- executable path
- working directory
- command-line arguments, including `-emulatestereo`
- environment block
- explicit "clean run" mode that strips UEVR variables
- explicit "UEVR run" mode that injects or launches with UEVR
- optional capture delay, frame index, hotkey, or programmatic trigger
- post-capture process cleanup
- saved output directory and naming convention

Useful profile examples:

```json
{
  "name": "SN2 clean emulatestereo",
  "exe": "E:/Github/Subnautica 2/Subnautica2/Binaries/Win64/Subnautica2-Win64-Shipping.exe",
  "working_dir": "E:/Github/Subnautica 2/Subnautica2/Binaries/Win64",
  "args": "-emulatestereo -ResX=1280 -ResY=720 -windowed",
  "environment_mode": "clean",
  "capture_trigger": "hotkey",
  "expected_api": "D3D12"
}
```

```json
{
  "name": "SN2 UEVR shader override test",
  "exe": "E:/Github/Subnautica 2/Subnautica2/Binaries/Win64/Subnautica2-Win64-Shipping.exe",
  "args": "-emulatestereo -ResX=1280 -ResY=720 -windowed",
  "environment_mode": "uevr",
  "uevr_profile": "Subnautica2-Win64-Shipping",
  "capture_trigger": "hotkey"
}
```

## 2. Nonblocking Capture and Replay

Any warning that can be safely ignored must be suppressible from launch profiles
and replay automation.

Needed options:

- ignore known incompatibilities
- never block on first incompatibility
- never show replay incompatibility modals during automated replay
- record incompatibilities in capture metadata instead of blocking
- expose incompatibility list through Python/API/JSON

This matters because a modal warning changes the workflow from automated to
manual and can make long capture/export loops stall indefinitely.

## 3. Complete D3D12 Command Argument Index

RenderDoc should expose an event-indexed table of every D3D12 command and its
arguments. This should be available without exporting to C++.

Minimum indexed calls:

- `SetPipelineState`
- `SetGraphicsRootSignature`
- `SetComputeRootSignature`
- `SetDescriptorHeaps`
- `SetGraphicsRootDescriptorTable`
- `SetComputeRootDescriptorTable`
- `SetGraphicsRootConstantBufferView`
- `SetComputeRootConstantBufferView`
- `SetGraphicsRootShaderResourceView`
- `SetComputeRootShaderResourceView`
- `SetGraphicsRootUnorderedAccessView`
- `SetComputeRootUnorderedAccessView`
- `IASetVertexBuffers`
- `IASetIndexBuffer`
- `OMSetRenderTargets`
- `RSSetViewports`
- `RSSetScissorRects`
- `ResourceBarrier`
- `CopyTextureRegion`
- `CopyResource`
- `CopyBufferRegion`
- `ClearRenderTargetView`
- `ClearUnorderedAccessViewFloat`
- `ClearUnorderedAccessViewUint`
- `DrawInstanced`
- `DrawIndexedInstanced`
- `Dispatch`
- `ExecuteIndirect`

The index should be queryable by:

- event ID
- command name
- PSO/resource/root signature handle
- event range
- viewport/scissor rectangle
- resource usage
- descriptor heap and slot
- marker/pass name

## 4. Descriptor Heap Timeline

Most of our work reduces to "what did this descriptor handle point at when this
draw ran?" RenderDoc should build a descriptor heap timeline:

- descriptor heap creation
- descriptor copy operations
- SRV/UAV/CBV/sampler creation into CPU descriptors
- CPU to GPU heap copies
- descriptor tables bound at root parameters
- root signature range interpretation
- shader register to descriptor slot resolution

Required query:

```text
state_at_event(event_id):
  root_param[0] -> descriptor table heap H, base slot N
  shader t5 -> heap H slot N+5 -> Texture3D uid_3446
  shader t8 -> heap H2 slot M+0 -> Texture3D uid_3674
  shader t9 -> heap H2 slot M+1 -> Texture3D uid_3676
```

This needs to handle:

- CBV/SRV/UAV heaps
- sampler heaps
- descriptor tables split across root parameters
- register spaces
- root descriptors
- descriptor copies and ring-buffered heaps
- unbound/null descriptors

## 5. Pipeline State Stream Support

UE5.6 uses D3D12 pipeline state streams. Automation must treat these as first
class PSOs.

Needed parsing:

- graphics PSO stream subobjects
- compute PSO stream subobjects
- root signature subobject
- VS/PS/CS/DS/HS/GS bytecode subobjects
- cached PSO subobjects
- input layout
- render-target formats
- depth/stencil formats
- primitive topology

Needed derived data:

- stable PSO ID
- shader hashes per stage
- shader bytecode CRC/hash
- shader model
- DXIL container reflection
- root signature hash
- render-target format key

This specifically avoids failures like "bound pixel shader unknown because
pipeline-stream PSO path was not handled."

## 6. Shader Registry and Reflection

RenderDoc should maintain a capture-level shader registry:

- bytecode hash
- stage
- entry point if recoverable
- DXIL/bytecode disassembly
- resource binding table
- root signature mapping
- constant buffer layout from DXIL reflection
- known source path/debug name if present
- all PSOs that use the shader
- all draw/dispatch events that use the shader

Queries we need:

- "Find every draw using pixel shader hash `166dba88`."
- "Show t5/t8/t9 resource bindings at each draw."
- "Is the shader replacement active in the current run?"
- "Which PSO variant is visible in the right eye?"

## 7. Shader Patch Verification

RenderDoc does not need to become a shader override system, but it should
verify override systems such as UEVR.

Needed checks:

- original shader hash at capture time
- replacement shader hash at draw time
- per-eye override active flag, imported from UEVR logs or runtime markers
- visible marker shaders, such as magenta probes
- before/after pixel statistics over a region of interest
- "draw exists but override inactive" report
- "override active but final image unchanged" report

For UEVR specifically, ingest:

- override manifest path
- `enabled` state
- target hash
- transformed shader hash
- transform operation counts
- reload timestamp
- runtime status JSON
- any "pipeline stream unsupported" or unknown shader notes

## 8. Eye-Aware Event Classification

The capture needs a robust eye classifier. Event order is not enough. The
classifier should use:

- viewport x/y/w/h
- scissor rect
- render target dimensions
- stereo array slice
- render target name or UID
- copy/composite destination rectangle
- UEVR stereo pass marker if available
- view constant buffer hash or matrix offsets

For side-by-side stereo:

```text
viewport x in [0, width/2)        -> left half
viewport x in [width/2, width)    -> right half
```

But the classifier must not assume event order. In the SN2 captures, the
right-half eye can render before the left-half eye.

Useful output:

```json
{
  "event": 16042,
  "eye": "right",
  "reason": "viewport=(640,0,640,720) in 1280x720 SBS target",
  "confidence": 0.99
}
```

## 9. Pixel Writer and Composite Lineage

We need one-click answers to:

- which event last wrote this pixel?
- which shader wrote it?
- which render target did it write?
- what post-process copied it?
- what source texture did the copy read?
- what event produced that source texture?

RenderDoc already has pixel history, but automation should expose a structured
lineage tree:

```text
final backbuffer pixel (900, 250)
  <- CopyRectPS event 18602 reads Texture2D 1005
     <- MainPS PSO 1445 event 14632 writes Texture2D 1005
        <- samples t9 Texture3D/Texture2D uid_3676
           <- producer chain ...
```

The same API should work for:

- draw outputs
- compute UAV writes
- copy destinations
- resolve destinations
- mip generation
- indirect dispatch argument buffers

## 10. Resource Producer and Consumer Lineage

For any resource, RenderDoc should report:

- all writes
- all reads
- all state transitions
- first writer
- last writer before event N
- all consumers after event N
- producer PSO/shader/pass name
- UAV/RTV/DSV/copy role
- dispatch dimensions or draw arguments
- viewport/scissor for draw writes

Required examples:

```text
resource_write_history(Texture3D uid_3446)
resource_producer_lineage(Texture2D uid_3676)
resource_pair_diff(left_uid, right_uid)
```

For volume textures, include:

- slice count
- mip count
- format
- dimensions
- per-slice export
- min/max/mean per channel
- nonzero voxel count

## 11. Capture Diffing

A lot of our diagnosis depends on comparing:

- clean run vs UEVR run
- overrides disabled vs enabled
- left eye vs right eye
- fog on vs fog off
- water lighting on vs off
- before patch vs after patch

RenderDoc should provide capture diff reports:

- PSO counts by shader hash
- draw/dispatch count deltas
- resource binding deltas
- descriptor table size deltas
- resource write-history deltas
- per-eye image region statistics
- event sequence similarity
- first divergent event

Example:

```text
diff_captures(before, after):
  PS 166dba88:
    before right events: 1
    after right events: 1
    shader hash changed: no
    t9 binding changed: no
    output ROI delta: 0.3 percent
    conclusion: override did not affect visible draw
```

## 12. Unreal Engine RDG Awareness

For UE5 titles, marker names and render graph pass names are essential. The
tooling should preserve and index:

- RDG event markers
- GPU debug groups
- pass names
- resource debug names
- shader debug names
- UE-specific resource naming patterns

Useful built-in classifications:

- `VolumetricFog.*`
- `VBufferA`, `VBufferB`
- `IntegratedLightScattering`
- `SingleLayerWater`
- `VirtualShadowMap`
- `Nanite`
- `CopyRectPS`
- `ReconstructVolumetricRenderTargetPS`
- `BasePass`

The tool should not hardcode game-specific assumptions, but it can provide
regex-based pass/resource classifiers.

## 13. Constant Buffer Tools

We repeatedly need to prove that CBVs are or are not per-eye correct.

Needed features:

- resolve CBV GPU VA to buffer resource and byte offset
- dump CBV bytes at event
- decode as float4 rows
- compare left vs right CBVs
- hash selected ranges
- annotate known UE view uniform offsets
- diff fields by float index

Queries:

```text
dump_cbv(event=16042, root_param=4, bytes=0x1200)
diff_cbv(event_right=16042, event_left=16678, root_param=4)
decode_view_uniform(event=16042)
```

UE-specific optional annotations:

- View matrices
- View rect
- volumetric fog grid parameters
- VolumetricFogScreenToResourceUV
- VolumetricFogUVMax
- temporal history parameters

## 14. Resource Export and Statistics

Automation should be able to export resource contents from any event:

- 2D textures to PNG/DDS/EXR
- 3D textures to DDS and per-slice PNG/EXR
- buffers to BIN/CSV
- descriptor tables to JSON
- shader bytecode to DXIL/DXBC
- root signatures to JSON

Also needed:

- image ROI statistics
- left/right half comparison
- histograms
- min/max/mean per channel
- nonzero counts
- "is this texture blank/uninitialized?" heuristic
- "does this look magenta?" probe detector

## 15. Automation Server

RenderDoc should expose a durable local automation server, not just UI Python.
This can be a small HTTP, JSON-RPC, or MCP-style service around the replay
controller.

Required commands:

```text
open_capture(path)
capture_summary()
index_events()
find_events(filters)
state_at_event(event_id)
descriptor_bindings(event_id)
root_signature_lookup(pso_or_event, register)
resource_write_history(resource_id)
resource_producer_lineage(resource_id)
pixel_history(x, y, event_scope)
save_resource(resource_id, event_id, output_path)
save_screenshot(output_path)
diff_events(event_a, event_b)
diff_captures(capture_a, capture_b)
export_shader(shader_id, output_path)
```

The server should return structured JSON with stable IDs. Text reports can be
generated on top of JSON, but the raw data should be machine-readable.

## 16. Replay-Time Mutation and Probes

For diagnosis, it is useful to alter replay state without modifying the live
game:

- replace a descriptor at a specific event
- bind a dummy black/white/magenta texture
- force a shader output color
- skip a draw or dispatch
- override a constant buffer range
- force a resource clear
- re-run pixel history after mutation

This does not replace UEVR runtime fixes, but it tells us whether a proposed
fix would be visible before we implement it in UEVR.

Example experiments:

- redirect right-eye `t9` to left-eye `t9`
- bind black to `t9`
- bind neutral `(1,1,1)` to `t5`
- force PS output magenta for `166dba88`
- copy left eye volume lighting into right eye resource

## 17. UEVR Integration Points

RenderDoc should not need UEVR to analyze a clean capture, but for UEVR runs it
should ingest and correlate:

- UEVR log file
- active profile path
- shader override manifests
- override cache files
- runtime render status JSON
- eye sample dumps
- hook-mode flags
- D3D12 diagnostic snapshots

Useful UEVR-facing features:

- launch profile can enable or disable UEVR explicitly
- capture metadata records whether UEVR was present
- event annotations show UEVR override status
- shader hash registry maps RenderDoc shader IDs to UEVR target hashes
- "why no visual change?" report includes UEVR override gates

## 18. Nsight Parity Matrix

| Capability | Current Nsight Use | RenderDoc Feature Needed |
| --- | --- | --- |
| Saved frame capture | `ngfx-capture` | existing capture with deterministic profiles |
| C++ command arguments | Generate C++ Capture | native command argument index |
| Descriptor heap state | C++ source parsing | descriptor heap timeline |
| PSO/shader mapping | C++ project plus DXIL extraction | native PSO stream parser and shader registry |
| Resource write history | MCP lineage tools | native resource producer/consumer graph |
| Pixel writer proof | manual UI or scripts | scriptable pixel history lineage |
| Per-eye classification | viewport/scissor scripts | built-in eye classifier |
| Capture diff | custom scripts | structured capture diff API |
| Replay mutation | ad hoc experiments | replay-time descriptor/shader/resource probes |
| Modal suppression | launch flags | profile-level nonblocking incompatibility policy |

## 19. Data Model

Store derived data beside the capture:

```text
capture.rdc
capture.rdc.index/
  events.sqlite
  descriptors.sqlite
  psos.sqlite
  resources.sqlite
  shaders/
    <hash>.dxil
    <hash>.disasm.txt
    <hash>.reflection.json
  reports/
    capture_summary.json
    eye_classification.json
    pso_usage.json
    resource_lineage_*.json
    pixel_history_*.json
```

SQLite tables:

- `events`
- `commands`
- `draws`
- `dispatches`
- `resource_events`
- `descriptor_writes`
- `descriptor_tables`
- `root_signatures`
- `psos`
- `shaders`
- `cbv_bindings`
- `srv_bindings`
- `uav_bindings`
- `rtv_bindings`
- `viewports`
- `scissors`
- `markers`
- `eye_classification`

## 20. UI Features

The UI should expose automation-derived answers without requiring scripts:

- "Explain selected draw" panel
- "Descriptor table at event" panel
- "Resource lineage" graph
- "Compare with other eye" button
- "Find final writer for pixel" workflow
- "Show all events using this shader"
- "Show all events reading this resource"
- "Show all events writing this resource"
- "Export this event state to JSON"
- "Copy root binding summary"

## 21. Implementation Touch Points in RenderDoc

Likely areas to extend:

- D3D12 serialisation code for richer command argument indexing
- D3D12 descriptor tracking and descriptor copy tracking
- D3D12 pipeline state stream parsing
- shader reflection and disassembly helpers
- replay controller APIs
- Python bindings for event state and descriptors
- capture metadata schema
- pixel history API output
- resource export API for 3D textures and slices
- qrenderdoc panels for lineage and descriptor state
- renderdoccmd commands for headless indexing and report generation

## 22. Milestones

### Milestone 1: Headless Index

- Add `renderdoccmd index-capture capture.rdc --out capture.rdc.index`.
- Emit event, command, viewport, scissor, PSO, and marker tables.
- Support `state_at_event` for root params and render targets.

### Milestone 2: Descriptor Resolution

- Track descriptor writes and copies.
- Resolve shader registers to resources at any draw/dispatch.
- Export descriptor state to JSON.

### Milestone 3: Shader and PSO Registry

- Parse pipeline state streams.
- Hash all shader bytecode.
- Reflect DXIL bindings.
- Link PSOs to draw/dispatch events.

### Milestone 4: Resource Lineage

- Build resource read/write histories.
- Support producer-chain queries.
- Support resource pair diffs.
- Export texture and volume statistics.

### Milestone 5: Eye-Aware Analysis

- Classify left/right events.
- Compare eye event streams.
- Compare bindings and CBVs per eye.
- Generate "first divergent eye state" reports.

### Milestone 6: Patch Verification

- Import UEVR override status.
- Detect shader override activation.
- Run magenta/neutral probe validation.
- Generate "no visual change" reports.

### Milestone 7: Replay Mutation

- Add controlled replay-time descriptor/resource/shader probes.
- Re-run ROI stats and pixel history after mutations.
- Export a patch experiment report.

## 23. SN2 Debugging Workflows This Should Enable

### Is pso3069 Still Active?

```text
find_events(shader_hash=166dba88, stage=PS)
classify_eye(events)
report viewport, RT, root params, t5/t8/t9 resources
```

### Did the Right-Eye Override Fire?

```text
import_uevr_status(log, shader_overrides)
find right-eye events using target hash
compare bound shader hash against replacement hash
sample output ROI for magenta or expected color delta
```

### What Is the First Bad Input?

```text
pixel_history(right_eye_pixel)
walk backward through copy/resolve/basepass
for each sampled texture:
  compare left vs right binding
  inspect producer lineage
  diff resource stats
```

### Is t9 Bad or Is t5 Math Bad?

```text
state_at_event(right_pso3069)
resolve t5/t8/t9
dump resource stats
mutate t9 to left-eye texture
mutate t5 to neutral texture
compare ROI output
```

### Did View CBVs Diverge Correctly?

```text
dump_cbv(right_event, root_param=View)
dump_cbv(left_event, root_param=View)
decode UE view uniform offsets
diff matrices and volumetric fog fields
```

## 24. Acceptance Criteria

A RenderDoc build has enough capability for this workflow when we can answer
the following from one command line:

```text
Given a capture and a right-eye pixel:
  identify the final visible writer
  identify the shader hash and PSO
  classify the event as left/right
  resolve t5/t8/t9 to concrete resources
  list each resource's last writer
  export the relevant resource slices
  compare the equivalent left-eye event
  state whether a UEVR override was active
  explain why a patch did or did not change pixels
```

The answer should be a machine-readable report plus a short human-readable
summary.

## 25. Practical First Step

The first useful implementation is not UI work. It is a headless indexer:

```text
renderdoccmd index-capture capture.rdc --out capture.rdc.index
renderdoccmd state-at-event capture.rdc --event 16042 --json
renderdoccmd resource-lineage capture.rdc --resource uid_3676 --json
renderdoccmd pixel-lineage capture.rdc --x 900 --y 250 --json
```

Once those commands are stable, the UI can simply display the same data.

