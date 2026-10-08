"""Changelog XML parsing (expat, keeps line numbers)."""
from __future__ import annotations

import xml.parsers.expat


class Changelog:
    def __init__(self, path):
        self.path = path
        self.ok = False
        self.error = None
        self.root_tag = ""
        self.root_attrs = {}
        self.changesets = []      # (id, author, line)
        self.sqlfiles = []        # dict(path, attrs, line, changeset, placement)
        self.includes = []        # (tag, attrs, line)
        self.has_rollback = False


def parse_changelog(path):
    cl = Changelog(path)
    with open(path, "rb") as f:
        data = f.read()
    p = xml.parsers.expat.ParserCreate()
    stack = []
    state = {"cs": None, "rb": 0}

    def start(name, attrs):
        local = name.split(":")[-1]
        line = p.CurrentLineNumber
        if not stack:
            cl.root_tag, cl.root_attrs = local, dict(attrs)
        if local == "changeSet":
            state["cs"] = attrs.get("id", "")
            cl.changesets.append((attrs.get("id"), attrs.get("author"), line))
        elif local == "rollback":
            state["rb"] += 1
            cl.has_rollback = True
        elif local == "sqlFile":
            parent = stack[-1] if stack else ""
            if state["rb"] > 0:
                placement = "Rollback"
            elif parent == "changeSet":
                placement = "Deploy"
            else:
                placement = "NotRun"
            cl.sqlfiles.append(dict(path=attrs.get("path") or "", attrs=dict(attrs), line=line,
                                    changeset=state["cs"] or "", placement=placement))
        elif local in ("include", "includeAll"):
            cl.includes.append((local, dict(attrs), line))
        stack.append(local)

    def end(name):
        local = stack.pop() if stack else ""
        if local == "rollback":
            state["rb"] -= 1
        elif local == "changeSet":
            state["cs"] = None

    p.StartElementHandler = start
    p.EndElementHandler = end
    try:
        p.Parse(data, True)
        cl.ok = True
    except xml.parsers.expat.ExpatError as e:
        cl.error = (e.lineno, xml.parsers.expat.ErrorString(e.code))
    return cl
