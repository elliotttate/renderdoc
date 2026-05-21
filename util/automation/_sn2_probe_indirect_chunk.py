"""Inspect the actual SDFile chunk for one ExecuteIndirect event."""

import os
import sys

sys.path.insert(0, r"E:\Github\renderdoc")
from util.automation import _lib  # type: ignore

CAPTURE = r"E:\Github\Subnautica 2\captures\sn2_nouevr_20260516_170001_frame610.rdc"
TARGET_EID = 21812  # known IndirectDispatch event

cap, controller = _lib.open_capture(CAPTURE)
try:
    sdfile = controller.GetStructuredFile()
    try:
        n = sdfile.chunks.size()
    except Exception:
        n = len(sdfile.chunks)

    # Find chunks for our target event
    for i in range(n):
        chunk = sdfile.chunks[i]
        try:
            eid = int(chunk.metadata.eventId)
        except Exception:
            continue
        if eid == TARGET_EID:
            print(f"chunk #{i}: {chunk.name}  numChildren={chunk.NumChildren()}")
            for j in range(chunk.NumChildren()):
                c = chunk.GetChild(j)
                cname = str(c.name)
                ctype = str(c.type.basetype) if hasattr(c, "type") else "?"
                print(f"  [{j}] {cname}  type={ctype}  nchildren={c.NumChildren()}")
                if c.NumChildren() > 0 and c.NumChildren() < 10:
                    for k in range(c.NumChildren()):
                        gc = c.GetChild(k)
                        print(f"      [{k}] {gc.name}  type={str(gc.type.basetype) if hasattr(gc,'type') else '?'}")
finally:
    controller.Shutdown()
    cap.Shutdown()
