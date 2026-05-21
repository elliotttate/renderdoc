"""UEVR / Nsight Automation extension for qrenderdoc.

Wires the modules in ``util/automation/`` into Tools > Automation menu entries
so users can run the same analyses from the UI that scripts run from the CLI.

This implements roadmap §20.

Architecture
------------

For each panel:

- A ``register()``-side ``RegisterWindowMenu`` call adds the menu item under
  ``Tools > Automation > <Name>``.
- The callback runs the corresponding ``util.automation.<module>`` function
  against the *currently loaded capture*. It does *not* spawn a subprocess —
  the controller is already alive inside qrenderdoc, so we obtain it via
  ``pyrenderdoc.Replay().BlockInvoke``.
- Results are displayed via a custom dialog built with ``MiniQtHelper``,
  containing a text box pre-populated with the JSON output.

The extension is robust to a missing capture (panels report "no capture loaded")
and to controller errors (errors surface in the dialog text instead of crashing
qrenderdoc).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Callable, Dict, List, Optional

import qrenderdoc as qrd
import renderdoc as rd

# Make util/automation importable. Extensions live in
# %APPDATA%/qrenderdoc/extensions/uevr_automation, while the source code we want
# to use lives in <repo>/util/automation. Users either symlink this folder into
# the extensions dir or set PYTHONPATH to point at util/. We add a best-effort
# path discovery here.

def _add_automation_to_syspath() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "..", "automation"),
        os.path.join(here, "..", ".."),  # parent of extensions/
        os.environ.get("RENDERDOC_AUTOMATION_DIR", ""),
    ]
    for c in candidates:
        if not c:
            continue
        c = os.path.abspath(c)
        if os.path.isdir(c) and c not in sys.path:
            sys.path.insert(0, c)
        parent = os.path.dirname(c)
        if os.path.isdir(parent) and parent not in sys.path:
            sys.path.insert(0, parent)


_add_automation_to_syspath()

# Try to import all of the automation modules. We don't fail the extension load
# if some are missing — each callback checks for its module.
_automation: Dict[str, Any] = {}
for mod_name in (
    "_lib",
    "state_at_event",
    "pixel_lineage",
    "resource_lineage",
    "eye_classifier",
    "pair_eye_events",
    "compare_eyes",
    "event_diff",
    "shader_debug",
    "descriptor_history",
    "replay_probe",
    "cbv_tools",
    "cbv_decode",
    "resource_export",
):
    try:
        _automation[mod_name] = __import__(f"automation.{mod_name}", fromlist=[mod_name])
    except Exception:
        try:
            _automation[mod_name] = __import__(mod_name)
        except Exception:
            _automation[mod_name] = None


# ---------------------------------------------------------------------------
# Dialog helpers
# ---------------------------------------------------------------------------

def _show_text_dialog(ctx: qrd.CaptureContext, title: str, body: str) -> None:
    helper = ctx.Extensions().GetMiniQtHelper()
    root = helper.CreateVerticalContainer()
    helper.SetWidgetName(root, title)

    header = helper.CreateLabel()
    helper.SetWidgetText(header, title)
    helper.AddWidget(root, header)

    text = helper.CreateTextBox(False, None)
    helper.SetWidgetText(text, body)
    helper.AddWidget(root, text)

    close = helper.CreateButton(lambda *_: ctx.Extensions().GetMiniQtHelper().CloseCurrentDialog(True))
    helper.SetWidgetText(close, "Close")
    helper.AddWidget(root, close)

    ctx.Extensions().GetMiniQtHelper().ShowWidgetAsDialog(root)


def _show_error(ctx: qrd.CaptureContext, message: str) -> None:
    ctx.Extensions().ErrorDialog(message, "Automation Extension")


def _ensure_capture(ctx: qrd.CaptureContext) -> bool:
    if not ctx.IsCaptureLoaded():
        _show_error(ctx, "Load a capture before running automation panels.")
        return False
    return True


def _selected_event(ctx: qrd.CaptureContext) -> int:
    eid = ctx.CurSelectedEvent()
    if eid == 0:
        eid = ctx.CurEvent()
    return int(eid)


def _capture_path(ctx: qrd.CaptureContext) -> Optional[str]:
    """Best-effort: the file backing the live capture. Some modules want a path
    to spin up their own controller; if we can't find one, they'll fall back
    to the in-process controller via BlockInvoke."""
    try:
        return str(ctx.GetCaptureFilename())
    except Exception:
        return None


def _run_with_controller(ctx: qrd.CaptureContext, fn: Callable[[Any], Any]) -> Any:
    """Run ``fn(controller)`` synchronously on the replay thread, surfacing
    exceptions as ``{"error": str}`` dicts so they can be rendered."""
    result_box: List[Any] = [None]
    error_box: List[Optional[str]] = [None]

    def _invoke(controller: rd.ReplayController) -> None:
        try:
            result_box[0] = fn(controller)
        except Exception as exc:
            error_box[0] = traceback.format_exc()

    ctx.Replay().BlockInvoke(_invoke)

    if error_box[0] is not None:
        return {"error": error_box[0]}
    return result_box[0]


def _json(obj: Any) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False)
    except Exception:
        return repr(obj)


# ---------------------------------------------------------------------------
# Callbacks — one per §20 panel
# ---------------------------------------------------------------------------

def explain_selected_draw(ctx: qrd.CaptureContext, data) -> None:
    if not _ensure_capture(ctx):
        return
    eid = _selected_event(ctx)
    lib = _automation.get("_lib")
    if lib is None:
        _show_error(ctx, "automation._lib not importable; check PYTHONPATH.")
        return

    def work(controller):
        return lib.collect_state_at_event(controller, eid)

    out = _run_with_controller(ctx, work)
    _show_text_dialog(ctx, f"Explain Selected Draw — event {eid}", _json(out))


def descriptor_table_at_event(ctx: qrd.CaptureContext, data) -> None:
    if not _ensure_capture(ctx):
        return
    eid = _selected_event(ctx)
    lib = _automation.get("_lib")
    if lib is None:
        _show_error(ctx, "automation._lib not importable; check PYTHONPATH.")
        return

    def work(controller):
        state = lib.collect_state_at_event(controller, eid)
        return {
            "eventId": eid,
            "rootSignature": state.get("rootSignature"),
            "descriptorHeaps": state.get("descriptorHeaps"),
            "bindings": state.get("bindings"),
        }

    out = _run_with_controller(ctx, work)
    _show_text_dialog(ctx, f"Descriptor Table at Event {eid}", _json(out))


def resource_lineage_panel(ctx: qrd.CaptureContext, data) -> None:
    if not _ensure_capture(ctx):
        return
    # Ask the user for a resource ID.
    helper = ctx.Extensions().GetMiniQtHelper()
    container = helper.CreateVerticalContainer()
    label = helper.CreateLabel()
    helper.SetWidgetText(label, "Resource ID or name:")
    helper.AddWidget(container, label)
    edit = helper.CreateTextBox(True, None)
    helper.AddWidget(container, edit)
    ok = helper.CreateButton(lambda *_: ctx.Extensions().GetMiniQtHelper().CloseCurrentDialog(True))
    helper.SetWidgetText(ok, "OK")
    helper.AddWidget(container, ok)
    accepted = ctx.Extensions().GetMiniQtHelper().ShowWidgetAsDialog(container)
    if not accepted:
        return
    ref = helper.GetWidgetText(edit)
    if not ref:
        return
    resource_lineage = _automation.get("resource_lineage")
    if resource_lineage is None:
        _show_error(ctx, "automation.resource_lineage not importable.")
        return

    def work(controller):
        # The module's `lineage()` accepts (capture_path, ref) but we already
        # have a controller — emulate the same logic inline.
        for r in controller.GetResources():
            if str(r.resourceId) == ref or str(r.name) == ref:
                usages = controller.GetUsage(r.resourceId)
                return {
                    "resource": str(r.resourceId),
                    "name": str(r.name),
                    "usages": [
                        {"eventId": int(u.eventId), "usage": str(u.usage).split(".")[-1]}
                        for u in usages
                    ],
                }
        return {"error": f"resource not found: {ref}"}

    out = _run_with_controller(ctx, work)
    _show_text_dialog(ctx, f"Resource Lineage — {ref}", _json(out))


def compare_with_other_eye(ctx: qrd.CaptureContext, data) -> None:
    if not _ensure_capture(ctx):
        return
    eid = _selected_event(ctx)
    eye_classifier = _automation.get("eye_classifier")
    event_diff = _automation.get("event_diff")
    if eye_classifier is None or event_diff is None:
        _show_error(ctx, "eye_classifier / event_diff modules not available.")
        return

    cap_path = _capture_path(ctx)
    if not cap_path or not os.path.exists(cap_path):
        _show_error(ctx, "Save the capture first; comparison opens a second controller against the .rdc file.")
        return

    # NOTE: qrenderdoc already initialised the replay runtime; do not call
    # rd.InitialiseReplay()/ShutdownReplay() here or you'll bring it down.
    try:
        try:
            eye_out = eye_classifier.classify_capture(cap_path, {"mode": "auto"})
        except Exception as exc:
            _show_error(ctx, f"classify_capture failed: {exc}")
            return
        by_eid = {e["eventId"]: e for e in eye_out.get("events", [])}
        ours = by_eid.get(eid)
        if ours is None:
            _show_text_dialog(ctx, f"Compare With Other Eye — event {eid}",
                              _json({"error": f"event {eid} not classified"}))
            return
        my_eye = ours.get("eye")
        peer = None
        for e in eye_out["events"]:
            if e["eventId"] != eid and e.get("eye") != my_eye and e.get("eye") in ("left", "right"):
                peer = e["eventId"]
                break
        if peer is None:
            _show_text_dialog(ctx, f"Compare With Other Eye — event {eid}",
                              _json({"error": "no peer-eye event found", "thisEye": my_eye}))
            return
        diff = event_diff.event_diff(cap_path, eid, peer)
        _show_text_dialog(
            ctx,
            f"Compare With Other Eye — event {eid} ({my_eye}) vs {peer}",
            _json({"thisEye": my_eye, "peer": peer, "diff": diff}),
        )
    finally:
        pass


def find_final_writer_for_pixel(ctx: qrd.CaptureContext, data) -> None:
    if not _ensure_capture(ctx):
        return
    helper = ctx.Extensions().GetMiniQtHelper()
    container = helper.CreateVerticalContainer()
    label = helper.CreateLabel()
    helper.SetWidgetText(label, "Pixel coordinates (x y):")
    helper.AddWidget(container, label)
    edit = helper.CreateTextBox(True, None)
    helper.AddWidget(container, edit)
    ok = helper.CreateButton(lambda *_: ctx.Extensions().GetMiniQtHelper().CloseCurrentDialog(True))
    helper.SetWidgetText(ok, "OK")
    helper.AddWidget(container, ok)
    if not ctx.Extensions().GetMiniQtHelper().ShowWidgetAsDialog(container):
        return
    coords = helper.GetWidgetText(edit).split()
    if len(coords) != 2:
        _show_error(ctx, "expected: x y")
        return
    x, y = int(coords[0]), int(coords[1])
    pixel_lineage = _automation.get("pixel_lineage")
    if pixel_lineage is None:
        _show_error(ctx, "pixel_lineage module not available.")
        return

    eid = _selected_event(ctx)

    def work(controller):
        # Inline equivalent of pixel_lineage.pixel_lineage() that uses our
        # live controller instead of opening a fresh one.
        # We synthesize: pick the first bound RT at `eid`, run PixelHistory.
        controller.SetFrameEvent(eid, True)
        d3d12 = None
        try:
            d3d12 = controller.GetD3D12PipelineState()
        except Exception:
            pass
        if d3d12 is None or len(d3d12.outputMerger.renderTargets) == 0:
            return {"error": "no RT bound at event"}
        rt = d3d12.outputMerger.renderTargets[0]
        sub = rd.Subresource(int(rt.firstMip), int(rt.firstSlice), 0)
        hist = controller.PixelHistory(rt.resource, x, y, sub, rd.CompType.Typeless)
        return {
            "x": x, "y": y, "rt": str(rt.resource), "eventId": eid,
            "history": [
                {"eventId": int(h.eventId), "passed": bool(h.Passed()) if hasattr(h, "Passed") else None}
                for h in hist
            ],
        }

    out = _run_with_controller(ctx, work)
    _show_text_dialog(ctx, f"Final Writer for ({x}, {y}) @ {eid}", _json(out))


def events_using_shader(ctx: qrd.CaptureContext, data) -> None:
    if not _ensure_capture(ctx):
        return
    helper = ctx.Extensions().GetMiniQtHelper()
    container = helper.CreateVerticalContainer()
    label = helper.CreateLabel()
    helper.SetWidgetText(label, "Shader bytecode hash prefix:")
    helper.AddWidget(container, label)
    edit = helper.CreateTextBox(True, None)
    helper.AddWidget(container, edit)
    ok = helper.CreateButton(lambda *_: ctx.Extensions().GetMiniQtHelper().CloseCurrentDialog(True))
    helper.SetWidgetText(ok, "OK")
    helper.AddWidget(container, ok)
    if not ctx.Extensions().GetMiniQtHelper().ShowWidgetAsDialog(container):
        return
    hash_prefix = helper.GetWidgetText(edit).strip().lower()
    if not hash_prefix:
        return
    lib = _automation.get("_lib")
    if lib is None:
        _show_error(ctx, "automation._lib not available.")
        return

    def work(controller):
        hits = []
        for a in lib.walk_actions(controller):
            if not (int(a.flags) & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
                continue
            try:
                state = lib.collect_state_at_event(controller, int(a.eventId))
            except Exception:
                continue
            for sh in state.get("shaders", []):
                if (sh.get("bytecodeHash") or "").lower().startswith(hash_prefix):
                    hits.append(
                        {
                            "eventId": int(a.eventId),
                            "stage": sh.get("stage"),
                            "bytecodeHash": sh.get("bytecodeHash"),
                        }
                    )
        return {"hashPrefix": hash_prefix, "count": len(hits), "events": hits}

    out = _run_with_controller(ctx, work)
    _show_text_dialog(ctx, f"Events Using Shader {hash_prefix}", _json(out))


def events_reading_writing_resource(ctx: qrd.CaptureContext, data, kind: str) -> None:
    if not _ensure_capture(ctx):
        return
    helper = ctx.Extensions().GetMiniQtHelper()
    container = helper.CreateVerticalContainer()
    label = helper.CreateLabel()
    helper.SetWidgetText(label, "Resource ID or name:")
    helper.AddWidget(container, label)
    edit = helper.CreateTextBox(True, None)
    helper.AddWidget(container, edit)
    ok = helper.CreateButton(lambda *_: ctx.Extensions().GetMiniQtHelper().CloseCurrentDialog(True))
    helper.SetWidgetText(ok, "OK")
    helper.AddWidget(container, ok)
    if not ctx.Extensions().GetMiniQtHelper().ShowWidgetAsDialog(container):
        return
    ref = helper.GetWidgetText(edit).strip()
    if not ref:
        return

    def work(controller):
        rid = None
        for r in controller.GetResources():
            if str(r.resourceId) == ref or str(r.name) == ref:
                rid = r.resourceId
                break
        if rid is None:
            return {"error": f"resource not found: {ref}"}
        usages = controller.GetUsage(rid)
        rows = []
        for u in usages:
            usage_name = str(u.usage).split(".")[-1]
            is_write = any(k in usage_name for k in ("Write", "RTV", "DSV", "UAV", "CopyDst", "Resolve"))
            is_read = any(k in usage_name for k in ("Read", "SRV", "CBV", "VS", "PS", "GS", "CS", "Vertex", "Index", "Indirect", "CopySrc"))
            if kind == "read" and not is_read:
                continue
            if kind == "write" and not is_write:
                continue
            rows.append({"eventId": int(u.eventId), "usage": usage_name})
        return {"resource": str(rid), "kind": kind, "count": len(rows), "rows": rows}

    out = _run_with_controller(ctx, work)
    _show_text_dialog(ctx, f"Events {kind.capitalize()}ing {ref}", _json(out))


def events_reading_resource(ctx, data):
    events_reading_writing_resource(ctx, data, "read")


def events_writing_resource(ctx, data):
    events_reading_writing_resource(ctx, data, "write")


def export_event_state_to_json(ctx: qrd.CaptureContext, data) -> None:
    if not _ensure_capture(ctx):
        return
    eid = _selected_event(ctx)
    lib = _automation.get("_lib")
    if lib is None:
        _show_error(ctx, "automation._lib not available.")
        return

    out = _run_with_controller(ctx, lambda c: lib.collect_state_at_event(c, eid))
    # Save dialog
    helper = ctx.Extensions().GetMiniQtHelper()
    container = helper.CreateVerticalContainer()
    label = helper.CreateLabel()
    helper.SetWidgetText(label, f"Save state-at-event {eid} to (full path):")
    helper.AddWidget(container, label)
    edit = helper.CreateTextBox(True, None)
    helper.SetWidgetText(edit, os.path.expanduser(f"~/state_event_{eid}.json"))
    helper.AddWidget(container, edit)
    ok = helper.CreateButton(lambda *_: ctx.Extensions().GetMiniQtHelper().CloseCurrentDialog(True))
    helper.SetWidgetText(ok, "Save")
    helper.AddWidget(container, ok)
    if not ctx.Extensions().GetMiniQtHelper().ShowWidgetAsDialog(container):
        return
    path = helper.GetWidgetText(edit).strip()
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(_json(out))
        ctx.Extensions().MessageDialog(f"Saved to {path}", "Automation Extension")
    except Exception as exc:
        _show_error(ctx, f"save failed: {exc}")


def copy_root_binding_summary(ctx: qrd.CaptureContext, data) -> None:
    if not _ensure_capture(ctx):
        return
    eid = _selected_event(ctx)
    lib = _automation.get("_lib")
    if lib is None:
        _show_error(ctx, "automation._lib not available.")
        return

    out = _run_with_controller(ctx, lambda c: lib.collect_state_at_event(c, eid))
    rs = out.get("rootSignature") or {}
    lines = [f"Root signature {rs.get('id')} @ event {eid}"]
    for p in rs.get("parameters", []) or []:
        if p.get("kind") == "RootTable":
            heap = p.get("heap") or "?"
            ranges = ", ".join(
                f"{r.get('category')}[space={r.get('space')}, t={r.get('baseRegister')}, count={r.get('count')}]"
                for r in p.get("ranges", []) or []
            )
            lines.append(f"  [{p['index']}] Table @ {heap}+{p.get('heapByteOffset')}: {ranges}")
        elif p.get("kind") == "RootConstants":
            lines.append(f"  [{p['index']}] Constants ({len(p.get('bytes', '')) // 2} bytes)")
        else:
            lines.append(f"  [{p['index']}] RootDescriptor -> {p.get('resource')}")
    summary = "\n".join(lines)
    # Copy to clipboard via Qt application if available.
    try:
        from PySide2.QtWidgets import QApplication  # noqa
        QApplication.clipboard().setText(summary)
        ctx.Extensions().MessageDialog("Root binding summary copied to clipboard.", "Automation Extension")
    except Exception:
        _show_text_dialog(ctx, f"Root Binding Summary @ {eid}", summary)


def run_replay_probe(ctx: qrd.CaptureContext, data) -> None:
    if not _ensure_capture(ctx):
        return
    helper = ctx.Extensions().GetMiniQtHelper()
    container = helper.CreateVerticalContainer()
    label = helper.CreateLabel()
    helper.SetWidgetText(label, "Probe spec (JSON):\n"
                                "{ \"mutation\": \"skip_draw|swap_resource|force_magenta_ps|...\",\n"
                                "  \"args\": {...}, \"event_id\": 16042, \"roi\": [0,0,64,64] }")
    helper.AddWidget(container, label)
    edit = helper.CreateTextBox(False, None)
    helper.SetWidgetText(edit, json.dumps({
        "mutation": "skip_draw",
        "args": {"event_id": _selected_event(ctx)},
        "event_id": _selected_event(ctx),
        "roi": [0, 0, 128, 128],
    }, indent=2))
    helper.AddWidget(container, edit)
    ok = helper.CreateButton(lambda *_: ctx.Extensions().GetMiniQtHelper().CloseCurrentDialog(True))
    helper.SetWidgetText(ok, "Run")
    helper.AddWidget(container, ok)
    if not ctx.Extensions().GetMiniQtHelper().ShowWidgetAsDialog(container):
        return
    try:
        spec = json.loads(helper.GetWidgetText(edit))
    except Exception as exc:
        _show_error(ctx, f"JSON parse error: {exc}")
        return

    replay_probe = _automation.get("replay_probe")
    if replay_probe is None:
        _show_error(ctx, "replay_probe module not available.")
        return

    cap_path = _capture_path(ctx)
    if not cap_path or not os.path.exists(cap_path):
        _show_error(ctx, "Replay probes need a live capture path; the current capture is unsaved.")
        return

    # Run the experiment in a worker thread style.
    # NOTE: qrenderdoc already initialised the replay runtime; do not call
    # rd.InitialiseReplay()/ShutdownReplay() here or you'll bring it down.
    try:
        with replay_probe.ProbeSession(cap_path) as session:
            method = getattr(session, spec["mutation"], None)
            if method is None:
                _show_error(ctx, f"unknown mutation: {spec['mutation']}")
                return
            before = session.sample_roi(int(spec["event_id"]), *spec.get("roi", [0, 0, 64, 64]))
            mut = method(**spec.get("args", {}))
            after = session.sample_roi(int(spec["event_id"]), *spec.get("roi", [0, 0, 64, 64]))
            cmp = replay_probe.ProbeSession.compare_roi(before, after)
        body = _json({"mutation": mut, "before": before, "after": after, "compare": cmp})
    finally:
        pass
    _show_text_dialog(ctx, "Replay Probe Result", body)


def run_experiment(ctx: qrd.CaptureContext, data) -> None:
    if not _ensure_capture(ctx):
        return
    cap_path = _capture_path(ctx)
    if not cap_path or not os.path.exists(cap_path):
        _show_error(ctx, "Experiments need a saved capture path.")
        return

    helper = ctx.Extensions().GetMiniQtHelper()
    container = helper.CreateVerticalContainer()
    label = helper.CreateLabel()
    helper.SetWidgetText(label, "Path to steps.json (or paste the step list below):")
    helper.AddWidget(container, label)
    path_edit = helper.CreateTextBox(True, None)
    helper.AddWidget(container, path_edit)
    text_edit = helper.CreateTextBox(False, None)
    helper.SetWidgetText(text_edit, json.dumps([
        {
            "name": "skip-this-draw",
            "mutation": "skip_draw",
            "args": {"event_id": _selected_event(ctx)},
            "event_id": _selected_event(ctx),
            "roi": [0, 0, 128, 128],
        },
    ], indent=2))
    helper.AddWidget(container, text_edit)
    ok = helper.CreateButton(lambda *_: ctx.Extensions().GetMiniQtHelper().CloseCurrentDialog(True))
    helper.SetWidgetText(ok, "Run")
    helper.AddWidget(container, ok)
    if not ctx.Extensions().GetMiniQtHelper().ShowWidgetAsDialog(container):
        return
    raw_path = helper.GetWidgetText(path_edit).strip()
    if raw_path and os.path.exists(raw_path):
        with open(raw_path, "r", encoding="utf-8") as f:
            steps = json.load(f)
    else:
        try:
            steps = json.loads(helper.GetWidgetText(text_edit))
        except Exception as exc:
            _show_error(ctx, f"step list JSON parse error: {exc}")
            return

    replay_probe = _automation.get("replay_probe")
    if replay_probe is None:
        _show_error(ctx, "replay_probe module not available.")
        return

    # NOTE: qrenderdoc already initialised the replay runtime; do not call
    # rd.InitialiseReplay()/ShutdownReplay() here or you'll bring it down.
    try:
        result = replay_probe.Experiment(cap_path).run(steps)
    finally:
        pass
    _show_text_dialog(ctx, "Experiment Result", _json(result))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_PANEL_TABLE = [
    (["Automation", "Explain Selected Draw"], explain_selected_draw),
    (["Automation", "Descriptor Table at Event"], descriptor_table_at_event),
    (["Automation", "Resource Lineage..."], resource_lineage_panel),
    (["Automation", "Compare With Other Eye"], compare_with_other_eye),
    (["Automation", "Find Final Writer for Pixel..."], find_final_writer_for_pixel),
    (["Automation", "Events Using This Shader..."], events_using_shader),
    (["Automation", "Events Reading This Resource..."], events_reading_resource),
    (["Automation", "Events Writing This Resource..."], events_writing_resource),
    (["Automation", "Export Event State to JSON..."], export_event_state_to_json),
    (["Automation", "Copy Root Binding Summary"], copy_root_binding_summary),
    (["Automation", "Replay-Time Probe..."], run_replay_probe),
    (["Automation", "Multi-Step Experiment..."], run_experiment),
]


def register(version: str, pyrenderdoc) -> None:
    extensions = pyrenderdoc.Extensions()
    for submenu, cb in _PANEL_TABLE:
        try:
            extensions.RegisterWindowMenu(qrd.WindowMenu.Tools, submenu, cb)
        except Exception as exc:
            print(f"[uevr_automation] failed to register {submenu}: {exc}")


def unregister() -> None:
    # qrenderdoc doesn't expose unregister APIs for menu entries; they survive
    # until process exit. This stub exists to satisfy the reload protocol.
    pass
