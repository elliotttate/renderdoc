"""Find the indirect-arg buffer for one specific sub-action by walking
the action tree up to the parent ExecuteIndirect call.
"""

import os
import sys

sys.path.insert(0, r"E:\Github\renderdoc")
from util.automation import _lib  # type: ignore
import renderdoc as rd

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
TARGETS = [21812, 21766, 24286, 24315]  # L=21812 (IndirectDispatch<0,1,1>), R=24286, etc.

cap, controller = _lib.open_capture(CAPTURE)
try:
    # Build eid -> action lookup; ALSO record parent eid
    action_by_eid = {}
    children_by_parent = {}

    def walk(actions, parent_eid=None):
        for a in actions:
            eid = int(a.eventId)
            action_by_eid[eid] = a
            if parent_eid is not None:
                children_by_parent.setdefault(parent_eid, []).append(eid)
            if a.children:
                walk(a.children, eid)

    walk(controller.GetRootActions())
    print(f"indexed {len(action_by_eid)} actions in tree")

    sdfile = controller.GetStructuredFile()
    try:
        n_chunks = sdfile.chunks.size()
    except Exception:
        n_chunks = len(sdfile.chunks)
    eid_to_chunk = {}
    for i in range(n_chunks):
        ch = sdfile.chunks[i]
        try:
            ce = int(ch.metadata.eventId)
        except Exception:
            continue
        eid_to_chunk.setdefault(ce, i)

    for target in TARGETS:
        print(f"\n=== Target event {target} ===")
        act = action_by_eid.get(target)
        if act is None:
            print(f"  not found in action tree")
            continue
        print(f"  name: {act.GetName(sdfile)}")
        # Walk up via parent traversal — parents in events list
        # Find which sub-event this is
        # Try the chunk for this eid first
        for probe_eid in (target, target - 1, target - 2, target - 3, target - 4, target - 5):
            idx = eid_to_chunk.get(probe_eid)
            if idx is None:
                continue
            chunk = sdfile.chunks[idx]
            cname = str(chunk.name)
            if "ExecuteIndirect" in cname:
                print(f"  found chunk at eid={probe_eid} (offset {probe_eid - target}): {cname}")
                print(f"  chunk numChildren = {chunk.NumChildren()}")
                for j in range(chunk.NumChildren()):
                    c = chunk.GetChild(j)
                    cn = str(c.name)
                    try:
                        if cn == "pArgumentBuffer":
                            print(f"    pArgumentBuffer = {c.AsResourceId()}")
                        elif cn == "pCommandSignature":
                            print(f"    pCommandSignature = {c.AsResourceId()}")
                        elif cn == "ArgumentBufferOffset":
                            print(f"    ArgumentBufferOffset = {c.AsInt()}")
                        elif cn == "MaxCommandCount":
                            print(f"    MaxCommandCount = {c.AsInt()}")
                        else:
                            print(f"    [{j}] {cn}")
                    except Exception as e:
                        print(f"    [{j}] {cn}  err={e}")
                break
        else:
            # No chunk found within 5 events back
            print(f"  no ExecuteIndirect chunk within 5 events back")
            # Try GetUsage on the bound resources to find the indirect arg buffer
            try:
                controller.SetFrameEvent(target, True)
                # Get usage of "Indirect" type events near this eid
                # Actually scan a few buffer resources
                for r in list(controller.GetResources())[:5]:
                    if r.type != rd.ResourceType.Buffer:
                        continue
                    usage = controller.GetUsage(r.resourceId)
                    for u in usage:
                        if int(u.eventId) == target and "Indirect" in str(u.usage):
                            print(f"    via GetUsage: arg buffer = {r.resourceId} ({r.name})")
            except Exception:
                pass
finally:
    controller.Shutdown()
    cap.Shutdown()
