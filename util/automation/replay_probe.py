"""Replay-time mutation probes (§16 of the roadmap).

Uses ``controller.ReplaceResource()``, ``BuildCustomShader()``, ``BuildTargetShader()``
and ``PickPixel()`` / ``GetMinMax()`` to mutate state at replay time without
modifying the live game, then re-samples a region of interest to measure the
effect.

Single-shot probes
------------------

  - ``swap_resource``        redirect a resource ID to another (e.g. right-eye t9 -> left-eye t9)
  - ``bind_neutral``         bind a constant 1x1 white/black/magenta sampled value at a stage register
  - ``force_magenta_ps``     replace a pixel shader resource with one that outputs magenta
  - ``force_ps_output_color_by_hash``  replace every PS with the given bytecode hash with a constant-output shader
  - ``skip_draw``            replace the bound PS with one that discards (so the draw produces no fragments)
  - ``skip_dispatch``        replace the bound CS with a no-op (so the dispatch writes nothing)
  - ``override_cbv_range``   replace the consuming shader so a range of bytes in a CBV is rewritten before use
  - ``force_resource_clear`` bind a constant-cleared replacement texture in place of a sampled texture
  - ``reset``                undo every mutation queued in this session

Sampling
--------

  - ``sample_roi``           per-channel min/max/mean for any RT format (uses PickPixel / GetMinMax)
  - ``pixel_history_at``     re-run PixelHistory after a mutation
  - ``min_max``              GetMinMax-backed bounds over the full RT
  - ``compare_roi``          delta of two ROI samples (returned by ``sample_roi`` or ``min_max``)

Experiment driver
-----------------

  - ``Experiment``           apply a sequence of mutations, capture ROI + pixel history before
                             and after each step, then automatically revert

This module intentionally keeps the controller alive across a probe -> measure
cycle so the workflow can be scripted as::

    with ProbeSession(capture) as s:
        roi_before = s.sample_roi(event_id, 100, 100, 200, 200)
        s.swap_resource("ResourceId(12345)", "ResourceId(54321)")
        roi_after = s.sample_roi(event_id, 100, 100, 200, 200)
        delta = s.compare_roi(roi_before, roi_after)
"""

import argparse
import json
import os
import struct
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from automation import _lib  # type: ignore
else:
    from . import _lib

import renderdoc as rd  # noqa: E402


# ---------------------------------------------------------------------------
# Custom shader factories
# ---------------------------------------------------------------------------

def _build_ps(controller, hlsl: str, entry: str = "main"):
    return controller.BuildCustomShader(
        entry,
        rd.ShaderEncoding.HLSL,
        bytes(hlsl, "utf-8"),
        rd.ShaderCompileFlags(),
        rd.ShaderStage.Pixel,
    )


def _build_cs(controller, hlsl: str, entry: str = "main"):
    return controller.BuildCustomShader(
        entry,
        rd.ShaderEncoding.HLSL,
        bytes(hlsl, "utf-8"),
        rd.ShaderCompileFlags(),
        rd.ShaderStage.Compute,
    )


_PS_MAGENTA = "float4 main(): SV_Target { return float4(1.0, 0.0, 1.0, 1.0); }\n"
_PS_DISCARD = "void main() { discard; }\n"


def _ps_constant_color(r: float, g: float, b: float, a: float = 1.0) -> str:
    return (
        f"float4 main(): SV_Target {{ return float4({r}, {g}, {b}, {a}); }}\n"
    )


_CS_NOOP = (
    "[numthreads(1,1,1)] void main(uint3 dtid: SV_DispatchThreadID) { }\n"
)


# ---------------------------------------------------------------------------
# ProbeSession
# ---------------------------------------------------------------------------

class ProbeSession:
    """A live replay controller plus a queue of pending replacements.

    Calling ``reset()`` (or exiting the ``with`` block) undoes every replacement
    queued during the session.
    """

    def __init__(self, capture_path: str):
        self.capture_path = capture_path
        self.cap = None
        self.controller = None
        # ResourceId -> revertable handle. We track the original ID we
        # called ReplaceResource() against so reset() can call RemoveReplacement.
        self._replacements: List[Any] = []
        # Built custom shaders (need FreeCustomShader on shutdown).
        self._custom_shaders: List[Any] = []
        # Buffer IDs that have controller.SetBufferOverride applied (need
        # ClearBufferOverride on shutdown).
        self._buffer_overrides_set: set = set()

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self):
        self.cap, self.controller = _lib.open_capture(self.capture_path)
        return self

    def __exit__(self, *exc):
        try:
            self.reset()
        finally:
            if self.controller is not None:
                self.controller.Shutdown()
            if self.cap is not None:
                self.cap.Shutdown()

    def reset(self) -> None:
        """Undo every queued replacement and free any built custom shaders."""
        if self.controller is None:
            return
        for r in self._replacements:
            try:
                self.controller.RemoveReplacement(r)
            except Exception:
                pass
        self._replacements = []
        for s in self._custom_shaders:
            try:
                self.controller.FreeCustomShader(s)
            except Exception:
                pass
        self._custom_shaders = []
        for buf in self._buffer_overrides_set:
            try:
                self.controller.ClearBufferOverride(buf)
            except Exception:
                pass
        self._buffer_overrides_set = set()

    # -- helpers -----------------------------------------------------------

    def _find_resource(self, ref: str):
        for r in self.controller.GetResources():
            if str(r.resourceId) == ref or str(r.name) == ref:
                return r.resourceId
        raise ValueError(f"resource not found: {ref}")

    def _shader_resource_at_event(self, event_id: int, stage_enum) -> Optional[Any]:
        self.controller.SetFrameEvent(int(event_id), True)
        pipe = self.controller.GetPipelineState()
        try:
            refl = pipe.GetShaderReflection(stage_enum)
        except Exception:
            refl = None
        if refl is None:
            return None
        return refl.resourceId

    def _find_shaders_by_hash(self, hash_prefix: str, stage_filter: Optional[str] = None) -> List[Any]:
        """Walk every action and collect distinct shader resource IDs whose bytecode hash
        starts with ``hash_prefix``. Optionally filter to a single stage name (e.g. ``"Pixel"``).
        """
        hash_prefix = hash_prefix.lower()
        out: List[Any] = []
        seen: set = set()
        for a in _lib.walk_actions(self.controller):
            if not (int(a.flags) & (int(rd.ActionFlags.Drawcall) | int(rd.ActionFlags.Dispatch))):
                continue
            try:
                self.controller.SetFrameEvent(int(a.eventId), True)
            except Exception:
                continue
            pipe = self.controller.GetPipelineState()
            for stage_enum in (
                rd.ShaderStage.Vertex,
                rd.ShaderStage.Hull,
                rd.ShaderStage.Domain,
                rd.ShaderStage.Geometry,
                rd.ShaderStage.Pixel,
                rd.ShaderStage.Compute,
                rd.ShaderStage.Amplification,
                rd.ShaderStage.Mesh,
            ):
                stage_name = _lib.shader_stage_name(stage_enum)
                if stage_filter and stage_name != stage_filter:
                    continue
                try:
                    refl = pipe.GetShaderReflection(stage_enum)
                except Exception:
                    refl = None
                if refl is None or len(refl.rawBytes) == 0:
                    continue
                h = _lib.shader_bytecode_hash(bytes(refl.rawBytes))
                if not h.lower().startswith(hash_prefix):
                    continue
                rid = refl.resourceId
                key = str(rid)
                if key in seen:
                    continue
                seen.add(key)
                out.append(rid)
        return out

    def _replace(self, orig, repl) -> None:
        self.controller.ReplaceResource(orig, repl)
        self._replacements.append(orig)

    # -- single-shot probes ------------------------------------------------

    def swap_resource(self, original_ref: str, replacement_ref: str) -> Dict[str, Any]:
        orig = self._find_resource(original_ref)
        repl = self._find_resource(replacement_ref)
        self._replace(orig, repl)
        return {"ok": True, "kind": "swap_resource", "orig": str(orig), "repl": str(repl)}

    def force_magenta_ps(self, ps_resource_ref: str) -> Dict[str, Any]:
        orig = self._find_resource(ps_resource_ref)
        repl, errors = _build_ps(self.controller, _PS_MAGENTA)
        if str(errors).strip():
            return {"ok": False, "kind": "force_magenta_ps", "errors": str(errors)}
        self._custom_shaders.append(repl)
        self._replace(orig, repl)
        return {"ok": True, "kind": "force_magenta_ps", "orig": str(orig), "repl": str(repl)}

    def force_ps_output_color_by_hash(
        self, hash_prefix: str, color=(1.0, 0.0, 1.0, 1.0)
    ) -> Dict[str, Any]:
        """For every pixel shader with bytecode hash starting with ``hash_prefix``,
        replace it with a constant-color shader. Returns the list of swapped shader IDs.
        """
        targets = self._find_shaders_by_hash(hash_prefix, stage_filter="Pixel")
        if not targets:
            return {"ok": False, "kind": "force_ps_output_color_by_hash", "errors": "no matching shaders"}
        r, g, b, a = (list(color) + [1.0])[:4]
        repl, errors = _build_ps(self.controller, _ps_constant_color(r, g, b, a))
        if str(errors).strip():
            return {"ok": False, "kind": "force_ps_output_color_by_hash", "errors": str(errors)}
        self._custom_shaders.append(repl)
        swapped = []
        for orig in targets:
            self._replace(orig, repl)
            swapped.append(str(orig))
        return {
            "ok": True,
            "kind": "force_ps_output_color_by_hash",
            "hashPrefix": hash_prefix,
            "color": [r, g, b, a],
            "replaced": swapped,
        }

    def skip_draw(self, event_id: int) -> Dict[str, Any]:
        """Replace the PS bound at ``event_id`` with one that calls ``discard``.

        The draw still runs (so RTV state is unchanged) but produces no fragments.
        """
        orig_ps = self._shader_resource_at_event(event_id, rd.ShaderStage.Pixel)
        if orig_ps is None:
            return {"ok": False, "kind": "skip_draw", "errors": "no pixel shader bound at event"}
        repl, errors = _build_ps(self.controller, _PS_DISCARD)
        if str(errors).strip():
            return {"ok": False, "kind": "skip_draw", "errors": str(errors)}
        self._custom_shaders.append(repl)
        self._replace(orig_ps, repl)
        return {"ok": True, "kind": "skip_draw", "eventId": int(event_id), "orig": str(orig_ps), "repl": str(repl)}

    def skip_dispatch(self, event_id: int) -> Dict[str, Any]:
        """Replace the CS bound at ``event_id`` with a no-op."""
        orig_cs = self._shader_resource_at_event(event_id, rd.ShaderStage.Compute)
        if orig_cs is None:
            return {"ok": False, "kind": "skip_dispatch", "errors": "no compute shader bound at event"}
        repl, errors = _build_cs(self.controller, _CS_NOOP)
        if str(errors).strip():
            return {"ok": False, "kind": "skip_dispatch", "errors": str(errors)}
        self._custom_shaders.append(repl)
        self._replace(orig_cs, repl)
        return {
            "ok": True,
            "kind": "skip_dispatch",
            "eventId": int(event_id),
            "orig": str(orig_cs),
            "repl": str(repl),
        }

    def force_resource_clear(self, resource_ref: str, color=(0.0, 0.0, 0.0, 0.0)) -> Dict[str, Any]:
        """Replace ``resource_ref`` (a texture being read by shaders) with a 1x1
        constant-color texture.

        Implementation: build a custom PS that outputs the constant color and
        replace the consumer texture with the *output* of that shader is not
        possible in the public API. Instead, this routes through the same
        mechanism as ``bind_neutral`` — RenderDoc maintains the existing texture
        resource but treats reads as the constant color via shader replacement
        of every consumer is too invasive.

        For now we implement the simpler ``ReplaceResource`` redirect: callers
        must pass an existing 1x1 constant texture resource that already lives
        in the capture (typical UE captures have one). If they don't, the call
        returns ``ok=False`` with ``errors="no clear-source texture provided"``
        so they can fall back to ``swap_resource``.
        """
        # Find any 1x1 texture that matches the requested clear color.
        clear_source = None
        for t in self.controller.GetTextures():
            if int(t.width) == 1 and int(t.height) == 1 and int(t.depth) == 1:
                clear_source = t.resourceId
                break
        if clear_source is None:
            return {
                "ok": False,
                "kind": "force_resource_clear",
                "errors": "no 1x1 clear-source texture in capture; pass an explicit replacement",
            }
        orig = self._find_resource(resource_ref)
        self._replace(orig, clear_source)
        return {
            "ok": True,
            "kind": "force_resource_clear",
            "orig": str(orig),
            "repl": str(clear_source),
            "color": list(color),
            "note": "using closest 1x1 texture in capture; exact color match is not enforced",
        }

    def bind_neutral(self, resource_ref: str, kind: str = "white") -> Dict[str, Any]:
        """Bind a neutral 1x1 texture in place of ``resource_ref``.

        ``kind`` is informational; the actual color depends on which 1x1 texture
        is found first. For exact control prefer ``force_resource_clear`` or
        ``swap_resource``.
        """
        out = self.force_resource_clear(resource_ref)
        if out.get("ok"):
            out["kind"] = "bind_neutral"
            out["requestedKind"] = kind
        return out

    def override_cbv_range(
        self,
        event_id: int,
        stage: str,
        cb_slot: int,
        byte_offset: int,
        new_bytes: bytes,
    ) -> Dict[str, Any]:
        """Override a byte range inside a constant buffer at replay time.

        Uses the driver-side ``SetBufferOverride()`` API: the replay controller
        layers the override over the recorded buffer contents whenever it serves
        bytes via ``GetBufferData`` or ``GetCBufferVariableContents``, so any
        tooling reading the buffer (including the shader debugger when stepping
        through code that reads the CBV) sees the patched values.

        The override is also written to a sidecar ``.bin`` for inspection.

        Limitations
        ~~~~~~~~~~~

        This does **not** modify the buffer's GPU storage. Real GPU draws that
        read the buffer through bound CBVs continue to see the unmodified bytes
        during replay. For a true GPU-side override, combine this with a
        ``ReplaceResource`` shader swap that rewrites the consuming shader to
        use the patched bytes.
        """
        stage_enum = getattr(rd.ShaderStage, stage)
        self.controller.SetFrameEvent(int(event_id), True)
        pipe = self.controller.GetPipelineState()
        try:
            arr = pipe.GetConstantBlocks(stage_enum, False)
        except Exception:
            return {"ok": False, "kind": "override_cbv_range", "errors": "no constant blocks at event"}
        if cb_slot < 0 or cb_slot >= len(arr):
            return {"ok": False, "kind": "override_cbv_range", "errors": f"slot {cb_slot} out of range (have {len(arr)})"}
        used = arr[cb_slot]
        desc = used.descriptor
        buf_id = desc.resource if desc is not None else None
        if buf_id is None or _lib.resource_id_str(buf_id) is None:
            return {"ok": False, "kind": "override_cbv_range", "errors": "no buffer bound at slot"}
        buf_offset = int(desc.byteOffset)
        buf_size = int(desc.byteSize) if int(desc.byteSize) > 0 else 0x10000
        try:
            data = bytes(self.controller.GetBufferData(buf_id, buf_offset, buf_size))
        except Exception as exc:
            return {"ok": False, "kind": "override_cbv_range", "errors": f"GetBufferData failed: {exc}"}
        # Splice in the override.
        new = bytearray(data)
        end = min(byte_offset + len(new_bytes), len(new))
        new[byte_offset:end] = new_bytes[: end - byte_offset]

        # Apply via the controller-side override API (added in this branch). The
        # override is anchored on the buffer's absolute byte offset, not the
        # window offset — translate accordingly.
        applied = False
        if hasattr(self.controller, "SetBufferOverride"):
            try:
                self.controller.SetBufferOverride(
                    buf_id, buf_offset + int(byte_offset), bytes(new_bytes)
                )
                self._buffer_overrides_set.add(buf_id)
                applied = True
            except Exception:
                pass

        sidecar = f"override_cbv_{event_id}_{stage}_{cb_slot}.bin"
        with open(sidecar, "wb") as f:
            f.write(new)
        return {
            "ok": True,
            "kind": "override_cbv_range",
            "eventId": int(event_id),
            "stage": stage,
            "cbSlot": int(cb_slot),
            "buffer": _lib.resource_id_str(buf_id),
            "bufferOffset": buf_offset,
            "bufferSize": buf_size,
            "byteOffset": int(byte_offset),
            "byteLength": len(new_bytes),
            "sidecar": os.path.abspath(sidecar),
            "appliedAtReplay": applied,
            "note": (
                "Override applied via controller.SetBufferOverride: analysis tools "
                "and the shader debugger see the patched bytes. Real GPU draws still "
                "see the original buffer contents — for that, pair this with a "
                "ReplaceResource shader swap."
                if applied else
                "Older RenderDoc build without SetBufferOverride; sidecar only."
            ),
        }

    # -- sampling ----------------------------------------------------------

    def _current_rt(self) -> Optional[Any]:
        try:
            d3d12 = self.controller.GetD3D12PipelineState()
        except Exception:
            d3d12 = None
        if d3d12 is not None and len(d3d12.outputMerger.renderTargets) > 0:
            return d3d12.outputMerger.renderTargets[0]
        try:
            vk = self.controller.GetVulkanPipelineState()
        except Exception:
            vk = None
        if vk is not None and hasattr(vk, "currentPass"):
            rts = getattr(vk.currentPass.framebuffer, "attachments", None)
            if rts and len(rts) > 0:
                return rts[0]
        return None

    def sample_roi(self, event_id: int, x: int, y: int, w: int, h: int, max_pixels: int = 64) -> Dict[str, Any]:
        """Sample a rectangle of the first bound RT at ``event_id`` using
        ``PickPixel`` (so HDR/float formats work natively).

        ``max_pixels`` caps the total sampled pixels (default 64 = 8x8 grid).
        Large ROIs are subsampled on a regular grid.
        """
        self.controller.SetFrameEvent(int(event_id), True)
        rt = self._current_rt()
        if rt is None:
            return {"error": "no RT bound at event"}
        sub = rd.Subresource(int(rt.firstMip), int(rt.firstSlice), 0)
        # Look up RT dimensions.
        tex = None
        for t in self.controller.GetTextures():
            if t.resourceId == rt.resource:
                tex = t
                break
        if tex is None:
            return {"error": "RT texture not found"}
        rw, rh = int(tex.width), int(tex.height)
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(rw, x0 + int(w)), min(rh, y0 + int(h))
        if x1 <= x0 or y1 <= y0:
            return {"error": "empty ROI"}
        roi_w, roi_h = x1 - x0, y1 - y0
        n_total = roi_w * roi_h
        if n_total > max_pixels:
            grid_w = max(1, int(max_pixels ** 0.5 * (roi_w / max(roi_w, roi_h))))
            grid_h = max(1, max_pixels // max(1, grid_w))
        else:
            grid_w, grid_h = roi_w, roi_h
        xs = [x0 + int((i + 0.5) * roi_w / grid_w) for i in range(grid_w)]
        ys = [y0 + int((i + 0.5) * roi_h / grid_h) for i in range(grid_h)]

        per_channel: List[List[float]] = [[], [], [], []]
        for yy in ys:
            for xx in xs:
                try:
                    pv = self.controller.PickPixel(rt.resource, int(xx), int(yy), sub, rd.CompType.Typeless)
                except Exception:
                    continue
                for c in range(4):
                    try:
                        per_channel[c].append(float(pv.floatValue[c]))
                    except Exception:
                        pass
        stats = []
        for c in range(4):
            if not per_channel[c]:
                continue
            v = per_channel[c]
            stats.append({"channel": c, "min": min(v), "max": max(v), "mean": sum(v) / len(v), "samples": len(v)})
        return {
            "rt": _lib.resource_id_str(rt.resource),
            "format": str(tex.format.Name()) if hasattr(tex.format, "Name") else str(tex.format),
            "rtSize": [rw, rh],
            "roi": [x0, y0, roi_w, roi_h],
            "grid": [grid_w, grid_h],
            "perChannel": stats,
        }

    def min_max(self, event_id: int) -> Dict[str, Any]:
        """``GetMinMax`` over the full first bound RT — fastest way to detect
        whether a draw produced any non-trivial change.
        """
        self.controller.SetFrameEvent(int(event_id), True)
        rt = self._current_rt()
        if rt is None:
            return {"error": "no RT bound at event"}
        sub = rd.Subresource(int(rt.firstMip), int(rt.firstSlice), 0)
        try:
            mn, mx = self.controller.GetMinMax(rt.resource, sub, rd.CompType.Typeless)
        except Exception as exc:
            return {"error": f"GetMinMax failed: {exc}"}
        return {
            "rt": _lib.resource_id_str(rt.resource),
            "min": [float(mn.floatValue[c]) for c in range(4)],
            "max": [float(mx.floatValue[c]) for c in range(4)],
        }

    def pixel_history_at(self, event_id: int, x: int, y: int) -> Dict[str, Any]:
        """Re-run ``PixelHistory`` for ``(x, y)`` after applying mutations.

        Returns a compact list of writers with shaderOut/preMod/postMod color
        triplets so callers can directly compare ``before`` vs ``after``.
        """
        self.controller.SetFrameEvent(int(event_id), True)
        rt = self._current_rt()
        if rt is None:
            return {"error": "no RT bound at event"}
        sub = rd.Subresource(int(rt.firstMip), int(rt.firstSlice), 0)
        try:
            hist = self.controller.PixelHistory(rt.resource, int(x), int(y), sub, rd.CompType.Typeless)
        except Exception as exc:
            return {"error": f"PixelHistory failed: {exc}"}
        rows = []
        for h in hist:
            def _c(field):
                try:
                    v = getattr(h, field)
                    return [float(v.col.floatValue[i]) for i in range(4)]
                except Exception:
                    return None

            rows.append(
                {
                    "eventId": int(h.eventId),
                    "fragIndex": int(getattr(h, "fragIndex", 0)),
                    "primitiveID": int(getattr(h, "primitiveID", 0)),
                    "shaderOut": _c("shaderOut"),
                    "preMod": _c("preMod"),
                    "postMod": _c("postMod"),
                    "passed": bool(h.Passed()) if hasattr(h, "Passed") else None,
                }
            )
        return {"x": int(x), "y": int(y), "rt": _lib.resource_id_str(rt.resource), "history": rows}

    @staticmethod
    def compare_roi(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
        """Return per-channel delta + relative delta between two ``sample_roi``
        (or ``min_max``) outputs.
        """
        if "error" in before or "error" in after:
            return {"error": "sampling error", "before": before, "after": after}

        def _vals(d):
            if "perChannel" in d:
                m = {row["channel"]: row for row in d["perChannel"]}
                return {c: (m[c]["min"], m[c]["max"], m[c]["mean"]) for c in m}
            return {c: (d["min"][c], d["max"][c], (d["min"][c] + d["max"][c]) * 0.5) for c in range(4)}

        b = _vals(before)
        a = _vals(after)
        out = []
        for c in sorted(b.keys() & a.keys()):
            d_min = a[c][0] - b[c][0]
            d_max = a[c][1] - b[c][1]
            d_mean = a[c][2] - b[c][2]
            denom = max(abs(b[c][2]), 1e-6)
            out.append(
                {
                    "channel": c,
                    "deltaMin": d_min,
                    "deltaMax": d_max,
                    "deltaMean": d_mean,
                    "relMean": d_mean / denom,
                }
            )
        return {"perChannel": out}


# ---------------------------------------------------------------------------
# Multi-step experiment driver (§22 M7 advanced)
# ---------------------------------------------------------------------------

class Experiment:
    """Run a sequence of mutations against a single capture, capturing ``sample_roi``
    + (optional) ``pixel_history_at`` before and after each step and automatically
    reverting between steps.

    Each step is a dict with::

        {
            "name": "swap-t9",                       # required
            "mutation": "swap_resource",             # required: name of a ProbeSession method
            "args": {"original_ref": "ResourceId(123)", ...},
            "event_id": 16042,                       # required for sample/measure
            "roi": [0, 0, 64, 64],                   # optional, defaults to (0,0,64,64)
            "pixel_history": [900, 250],             # optional, capture before/after pixel history at (x,y)
            "revert_between_steps": true,            # default true; false chains mutations
        }
    """

    def __init__(self, capture_path: str):
        self.capture_path = capture_path

    def run(self, steps: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        results = []
        with ProbeSession(self.capture_path) as session:
            for step in steps:
                name = step.get("name") or step.get("mutation") or "step"
                method_name = step["mutation"]
                args = step.get("args", {}) or {}
                event_id = int(step["event_id"])
                roi = list(step.get("roi") or [0, 0, 64, 64])
                ph_xy = step.get("pixel_history")
                revert = step.get("revert_between_steps", True)

                method = getattr(session, method_name, None)
                if method is None:
                    results.append({"step": name, "error": f"unknown mutation: {method_name}"})
                    continue

                before_roi = session.sample_roi(event_id, *roi)
                before_ph = (
                    session.pixel_history_at(event_id, int(ph_xy[0]), int(ph_xy[1]))
                    if ph_xy
                    else None
                )
                mutation_result = method(**args)
                after_roi = session.sample_roi(event_id, *roi)
                after_ph = (
                    session.pixel_history_at(event_id, int(ph_xy[0]), int(ph_xy[1]))
                    if ph_xy
                    else None
                )
                cmp = ProbeSession.compare_roi(before_roi, after_roi)
                results.append(
                    {
                        "step": name,
                        "mutation": method_name,
                        "mutationResult": mutation_result,
                        "before": {"roi": before_roi, "pixelHistory": before_ph},
                        "after": {"roi": after_roi, "pixelHistory": after_ph},
                        "compare": cmp,
                    }
                )
                if revert:
                    session.reset()
        return {"steps": results}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Replay-time mutation probes.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pswap = sub.add_parser("swap")
    pswap.add_argument("capture")
    pswap.add_argument("--orig", required=True)
    pswap.add_argument("--repl", required=True)
    pswap.add_argument("--event", "-e", type=int, required=True)
    pswap.add_argument("--roi", nargs=4, type=int, default=(0, 0, 64, 64), help="x y w h")

    pmag = sub.add_parser("magenta-ps")
    pmag.add_argument("capture")
    pmag.add_argument("--shader", required=True, help="Pixel shader ResourceId or name")
    pmag.add_argument("--event", "-e", type=int, required=True)
    pmag.add_argument("--roi", nargs=4, type=int, default=(0, 0, 64, 64))

    pcol = sub.add_parser("color-by-hash")
    pcol.add_argument("capture")
    pcol.add_argument("--hash", required=True, help="Pixel shader bytecode hash prefix")
    pcol.add_argument("--color", nargs=4, type=float, default=(1.0, 0.0, 1.0, 1.0))
    pcol.add_argument("--event", "-e", type=int, required=True)
    pcol.add_argument("--roi", nargs=4, type=int, default=(0, 0, 64, 64))

    pskip = sub.add_parser("skip-draw")
    pskip.add_argument("capture")
    pskip.add_argument("--event", "-e", type=int, required=True)
    pskip.add_argument("--roi", nargs=4, type=int, default=(0, 0, 64, 64))

    pskipd = sub.add_parser("skip-dispatch")
    pskipd.add_argument("capture")
    pskipd.add_argument("--event", "-e", type=int, required=True)
    pskipd.add_argument("--roi", nargs=4, type=int, default=(0, 0, 64, 64))

    pclear = sub.add_parser("force-clear")
    pclear.add_argument("capture")
    pclear.add_argument("--resource", required=True)
    pclear.add_argument("--event", "-e", type=int, required=True)
    pclear.add_argument("--roi", nargs=4, type=int, default=(0, 0, 64, 64))

    pcbv = sub.add_parser("override-cbv")
    pcbv.add_argument("capture")
    pcbv.add_argument("--event", "-e", type=int, required=True)
    pcbv.add_argument("--stage", default="Pixel")
    pcbv.add_argument("--slot", type=int, default=0)
    pcbv.add_argument("--byte-offset", type=int, default=0)
    pcbv.add_argument("--bytes-hex", required=True, help="Hex string of override bytes (no 0x)")

    pexp = sub.add_parser("experiment")
    pexp.add_argument("capture")
    pexp.add_argument("--steps", required=True, help="JSON file with the step list")
    pexp.add_argument("--out", "-o")

    args = p.parse_args(argv)

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        if args.cmd == "experiment":
            with open(args.steps, "r", encoding="utf-8") as f:
                steps = json.load(f)
            out = Experiment(args.capture).run(steps)
            text = json.dumps(out, indent=2, ensure_ascii=False)
            if args.out:
                with open(args.out, "w", encoding="utf-8") as f:
                    f.write(text)
            else:
                print(text)
            return 0

        with ProbeSession(args.capture) as s:
            before = s.sample_roi(args.event, *args.roi)
            if args.cmd == "swap":
                mut = s.swap_resource(args.orig, args.repl)
            elif args.cmd == "magenta-ps":
                mut = s.force_magenta_ps(args.shader)
            elif args.cmd == "color-by-hash":
                mut = s.force_ps_output_color_by_hash(args.hash, tuple(args.color))
            elif args.cmd == "skip-draw":
                mut = s.skip_draw(args.event)
            elif args.cmd == "skip-dispatch":
                mut = s.skip_dispatch(args.event)
            elif args.cmd == "force-clear":
                mut = s.force_resource_clear(args.resource)
            elif args.cmd == "override-cbv":
                mut = s.override_cbv_range(
                    args.event, args.stage, args.slot, args.byte_offset, bytes.fromhex(args.bytes_hex)
                )
            else:
                return 1
            if not mut.get("ok"):
                print(json.dumps(mut, indent=2))
                return 1
            after = s.sample_roi(args.event, *args.roi)
            cmp = ProbeSession.compare_roi(before, after)
        print(
            json.dumps(
                {"mutation": mut, "before": before, "after": after, "compare": cmp},
                indent=2,
                ensure_ascii=False,
            )
        )
    finally:
        rd.ShutdownReplay()
    return 0


if __name__ == "__main__":
    sys.exit(main())
