"""Classify script paths into section / type / schema / object."""
from __future__ import annotations

import os


TYPE_SUFFIX = {"table": "Table", "userdefinedfunction": "Tvf", "function": "Tvf", "storedprocedure": "Procedure",
               "procedure": "Procedure", "data": "Data", "view": "Other", "trigger": "Other"}
FOLDER_TYPE = {"tables": "Table", "table": "Table", "function": "Tvf", "functions": "Tvf",
               "procedures": "Procedure", "procedure": "Procedure", "storedprocedures": "Procedure", "data": "Data"}


def classify(rel):
    parts = rel.split("/")
    low = [x.lower() for x in parts]
    folder_section = "Deploy" if "deploy" in low else ("Rollback" if "rollback" in low else "Unknown")
    fname = parts[-1]
    stem = fname[:-4] if fname.lower().endswith(".sql") else fname
    segs = stem.split(".")
    stype = None
    if len(segs) >= 2 and segs[-1].lower() in TYPE_SUFFIX:
        stype = TYPE_SUFFIX[segs[-1].lower()]
        segs = segs[:-1]
    if stype is None:
        for x in reversed(low[:-1]):
            if x in FOLDER_TYPE:
                stype = FOLDER_TYPE[x]
                break
    stype = stype or "Other"
    if len(segs) >= 2:
        schema, obj = segs[0], ".".join(segs[1:])
    else:
        schema, obj = "", segs[0]
    base = fname[len(schema) + 1:] if schema else fname
    if folder_section != "Unknown":
        k = low.index(folder_section.lower())
        tail = "/".join(low[k + 1:])
    else:
        tail = "/".join(low)
    return folder_section, stype, schema, obj, fname, base, tail


def norm_rel(path, base):
    p = os.path.normpath(path)
    try:
        r = os.path.relpath(p, base)
    except ValueError:
        r = p
    return r.replace(os.sep, "/")


def lines_str(lines):
    return ", ".join("L%d" % ln for ln in lines[:6]) + (" …" if len(lines) > 6 else "")
