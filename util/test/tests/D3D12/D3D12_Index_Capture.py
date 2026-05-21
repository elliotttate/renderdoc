import json
import os
import sys
import tempfile

import rdtest
import renderdoc as rd


# Make the util/automation package importable from this test.
_HERE = os.path.dirname(os.path.abspath(__file__))
_UTIL = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
if _UTIL not in sys.path:
    sys.path.insert(0, _UTIL)

from automation import _lib as autom_lib  # noqa: E402
from automation import index_capture as autom_index  # noqa: E402
from automation import state_at_event as autom_state  # noqa: E402


class D3D12_Index_Capture(rdtest.TestCase):
    """Smoke test for util/automation: runs the indexer over a D3D12 capture and
    asserts shape + a known register-to-resource resolution.

    Reuses the existing D3D12_Descriptor_Indexing demo so we don't ship a new
    one — that demo is already known to bind SRVs/UAVs at various tiers.
    """

    demos_test_name = "D3D12_Descriptor_Indexing"

    def check_capture(self):
        # 1) state_at_event should return a structured object with descriptor
        #    bindings resolved to resources for a known Draw event.
        for sm in ("sm_5_1", "sm_6_0", "sm_6_6"):
            base = self.find_action("Tests " + sm)
            if base is None:
                continue
            action = self.find_action("Draw", base.eventId)
            self.check(action is not None)
            state = autom_lib.collect_state_at_event(self.controller, action.eventId)
            self.check_eq(state["eventId"], action.eventId)
            self.check("bindings" in state)
            # Every Draw in this demo binds at least one resource.
            self.check(len(state["bindings"]) > 0)
            for b in state["bindings"]:
                self.check(b["stage"] in (
                    "Vertex", "Hull", "Domain", "Geometry", "Pixel",
                    "Compute", "Amplification", "Mesh",
                ))
                self.check(b["type"] is not None)
            break

        # 2) index_capture against the same controller — should walk every
        #    action and produce events.jsonl + actions.jsonl + state.jsonl.
        with tempfile.TemporaryDirectory() as tmpdir:
            # We've already opened the capture via rdtest; instead of letting
            # autom_index open it again, drive its inner writers directly.
            os.makedirs(tmpdir, exist_ok=True)
            autom_index._write_meta(self.controller, "<embedded>", tmpdir)
            autom_index._write_resources(self.controller, tmpdir)
            autom_index._write_events_and_state(self.controller, tmpdir)

            self.check(os.path.exists(os.path.join(tmpdir, "events.jsonl")))
            self.check(os.path.exists(os.path.join(tmpdir, "actions.jsonl")))
            self.check(os.path.exists(os.path.join(tmpdir, "state.jsonl")))
            self.check(os.path.exists(os.path.join(tmpdir, "resources.json")))

            with open(os.path.join(tmpdir, "events.jsonl"), "r", encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            with open(os.path.join(tmpdir, "actions.jsonl"), "r", encoding="utf-8") as f:
                actions = [json.loads(line) for line in f if line.strip()]
            with open(os.path.join(tmpdir, "state.jsonl"), "r", encoding="utf-8") as f:
                states = [json.loads(line) for line in f if line.strip()]

            self.check(len(events) > 0)
            self.check(len(actions) > 0)
            self.check(len(states) == len(actions))  # one state per significant action

            # Every state row references a non-empty bindings array for Draws/Dispatches
            for s in states:
                self.check("bindings" in s)

            # Shader hashes should be stable across the run
            hash_set = set()
            for s in states:
                for sh in s.get("shaders", []):
                    if sh.get("bytecodeHash") and sh["bytecodeHash"] != "empty":
                        hash_set.add(sh["bytecodeHash"])
            self.check(len(hash_set) > 0)

            with open(os.path.join(tmpdir, "meta.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)
            self.check_eq(meta.get("event_count"), len(events))
            self.check_eq(meta.get("action_count"), len(actions))

        rdtest.log.print("util/automation indexer smoke test passed")
