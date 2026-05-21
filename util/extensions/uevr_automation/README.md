# UEVR / Nsight Automation — qrenderdoc extension

This extension surfaces the `util/automation/` modules under qrenderdoc's
`Tools > Automation` menu so users can run the same analyses from the UI that
scripts run from the CLI. It implements the §20 UI panel list from
`docs/UEVR_NSIGHT_AUTOMATION_ROADMAP.md`.

## What it adds

Under `Tools > Automation`:

- **Explain Selected Draw** — `state_at_event` against the currently selected event
- **Descriptor Table at Event** — root signature + descriptor heap bindings
- **Resource Lineage…** — `GetUsage` for a resource ID
- **Compare With Other Eye** — pair the selected event with its peer eye and diff
- **Find Final Writer for Pixel…** — `PixelHistory` for a user-supplied (x, y)
- **Events Using This Shader…** — every draw/dispatch whose stage matches a hash
- **Events Reading This Resource…** / **Events Writing This Resource…** — filtered `GetUsage`
- **Export Event State to JSON…** — save the full `state_at_event` snapshot to disk
- **Copy Root Binding Summary** — copies a textual root-binding summary to clipboard
- **Replay-Time Probe…** — single mutation + before/after ROI sampling
- **Multi-Step Experiment…** — chain mutations, automatic revert + before/after capture per step

## Installation

Either:

1. Copy or symlink this whole folder into your RenderDoc extensions dir:
   - Windows: `%APPDATA%\qrenderdoc\extensions\uevr_automation`
   - Linux: `~/.local/share/qrenderdoc/extensions/uevr_automation`

2. Or run it ad-hoc from this checkout by setting
   `RENDERDOC_AUTOMATION_DIR=<repo>\util\automation` before launching qrenderdoc
   and pointing the extension manager at this folder.

Then in qrenderdoc:

1. `Tools > Manage Extensions`
2. Tick **Loaded** for `UEVR / Nsight Automation`
3. Tick **Always Load** to make it persistent

The menu entries appear under `Tools > Automation`.

## How it integrates with `util/automation/`

Wherever possible the callbacks reuse code from `util/automation/*.py`. Some
functions (`pixel_lineage`, `event_diff`) ordinarily open their own
`ReplayController`; the extension instead runs the equivalent work against the
live controller via `pyrenderdoc.Replay().BlockInvoke`, so loaded captures
don't need to be re-opened.

For mutations (`replay_probe`), the extension opens a *separate* short-lived
controller so it can build custom shaders and replace resources without
mutating the live state. That requires a saved capture path; the dialog
reports an error if the current capture is unsaved.
