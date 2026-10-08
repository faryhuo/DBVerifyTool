#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ace_db_verify.py - ACE DB database scripts verification (static, no Liquibase).

Reads the changelog XML the same way run_changelog_sql.bat / run_rollback_sql.bat do:
  * Deploy   = <sqlFile> elements that are direct children of <changeSet>, in document order
  * Rollback = <sqlFile> elements anywhere inside <rollback>, in document order
  * path     = resolved relative to the changelog file's own directory
and checks every script the way `sqlcmd -b -f 65001 -i <file>` would see it
(GO batch separators, QUOTED_IDENTIFIER OFF, $(var) substitution, UTF-8 input).

Output: one self-contained HTML report (+ JSON). Nothing is executed against a database
unless --compile is given; --compile runs each batch through sqlcmd under SET NOEXEC ON
(compile only, no changes to the database).

Usage (from the folder that holds the bat files):
    python ace_db_verify.py
    python ace_db_verify.py --changelog changelogs/changelog_ACE_GWP_ID.xml --config ace_db_verify.config.json
    python ace_db_verify.py --compile            # also compile with sqlcmd (SET NOEXEC ON)

Exit code: 0 = OK, 1 = blocking issues found (see --fail-on), 2 = tool error.
Python 3.8+, standard library only.
"""
from __future__ import annotations

import argparse
import codecs
import datetime as dt
import getpass
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.parsers.expat
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

VERSION = "1.0.0"
WORKFLOW = "ace-db-database-scripts-verification"

DEFAULT_CONFIG = {
    "project": None,
    "release": None,
    # Entity set rule: every object in one of these schemas must exist in all of them.
    "expected_schemas": ["cmy", "csg", "dbo"],
    "entity_set_types": ["Table", "Tvf", "Procedure", "Data"],
    "entity_set_exclude": [],             # regexes on the file name without schema
    # Cross-schema references that are allowed, e.g. {"from": "staging", "to": "dbo", "file_regex": "Migration"}
    "cross_schema_allow": [],
    # Objects that already exist in PROD: a CREATE OR ALTER deploy may be rolled back with ALTER.
    # Same effect as a "-- verify:preexisting" comment in the deploy script.
    "preexisting_objects": [],
    "create_or_alter_rollback": ["DROP"],
    "deploy_category_order": ["Table", "Tvf", "Procedure", "Data"],
    "rollback_category_order": ["Data", "Procedure", "Tvf", "Table"],
    "allowed_dbms": ["mssql"],
    "expected_encoding": "UTF-8",
    "severity_overrides": {},             # {"inv.entity_set": "WARNING"}
    "suppress": [],                       # [{"check": "ver.schema_mismatch", "file_regex": "...", "detail_regex": "..."}]
    "sqlcmd": {
        "exe": "sqlcmd",
        "server": None,
        "database": None,
        "trusted": True,
        "user": None,
        "password_env": "ACE_DB_PASSWORD",
        "login_timeout": 15,
        "file_timeout": 300,
    },
}

# check id -> (phase, title, issue text, recommended fix)
CHECKS = {
    "xml.malformed": ("P7", "XML well-formedness", "Changelog XML is not well-formed.",
                      "Fix the XML syntax error at the reported line."),
    "xml.structure": ("P7", "XML structure", "Changelog root / changeSet declaration problem.",
                      "Fix the changelog structure."),
    "xml.duplicate": ("P7", "Duplicate XML entry", "Duplicate changeSet id or duplicate sqlFile path.",
                      "Remove or rename the duplicate entry; the bat would run the file twice."),
    "xml.attributes": ("P7", "sqlFile attributes", "sqlFile attribute is missing or unexpected.",
                       "Set dbms / encoding consistently on every sqlFile."),
    "xml.placement": ("P7", "Deploy/Rollback placement",
                      "Script placement in the XML does not match its Deploy/Rollback folder.",
                      "Deploy scripts go directly under <changeSet>; rollback scripts go inside <rollback>."),
    "xml.not_run": ("P7", "sqlFile not executed",
                    "sqlFile is neither a direct child of <changeSet> nor inside <rollback>; neither bat runs it.",
                    "Move the entry under <changeSet> or into <rollback>."),
    "xml.order": ("P7", "XML execution order", "sqlFile appears out of XML category order.",
                  "Move the entry into its category block in the changelog."),
    "xml.include": ("P7", "Included changelog", "Included changelog file does not exist.",
                    "Fix the include path."),
    "inv.missing": ("P2", "Missing file", "File referenced in the changelog does not exist on disk.",
                    "Add the file or fix the sqlFile path (paths resolve relative to the changelog folder)."),
    "inv.extra": ("P2", "Extra file", "SQL file exists on disk but is not referenced in the changelog.",
                  "Add a sqlFile entry for it or remove it from the release folder."),
    "inv.entity_set": ("P2", "Entity set incomplete", "Expected schema variant(s) missing for this entity set.",
                       "Add the missing schema scripts, or adjust expected_schemas / entity_set_exclude if the variant is intentionally absent."),
    "inv.content": ("P2", "Cross-schema content mismatch",
                    "Schema variants differ after normalization (comments, whitespace and schema prefixes ignored).",
                    "Align the variants so they differ only by schema prefix."),
    "ver.pairing": ("P3", "Deploy/rollback pairing", "Script has no counterpart in the other section.",
                    "Add the matching Deploy/Rollback script and changelog entry."),
    "ver.rollback_logic": ("P3", "Rollback logic mismatch", "Rollback operation does not reverse the deploy operation.",
                           "Update rollback to match the deploy (CREATE → DROP, ALTER → restore previous definition). "
                           "If the object already exists in PROD, add it to preexisting_objects or put '-- verify:preexisting' in the deploy script."),
    "ver.op_unknown": ("P3", "Operation not detected",
                       "No CREATE/ALTER/DROP for the file's object was found in the script.",
                       "Check that the file name matches the object the script defines."),
    "ver.schema_mismatch": ("P3", "Schema mismatch", "Object reference uses a different schema than the file's schema.",
                            "Change the reference to the file's schema, rename the file, or allowlist the pair in cross_schema_allow."),
    "cmp.encoding": ("P3A", "File encoding", "File is not plain UTF-8 text as read by sqlcmd -f 65001.",
                     "Save the file as UTF-8 (SSMS: Save With Encoding → UTF-8)."),
    "cmp.hidden_chars": ("P3A", "Hidden characters", "Non-breaking / zero-width characters found.",
                         "Replace them with normal spaces."),
    "cmp.unterminated": ("P3A", "Simulated execution", "Unterminated string, quoted identifier or comment.",
                         "Close the literal / comment."),
    "cmp.parens": ("P3A", "Simulated execution", "Unmatched ( or ) would prevent the batch from executing.",
                   "Correct the SQL so the batch parses."),
    "cmp.begin_end": ("P3A", "BEGIN/END pairing", "BEGIN/END, BEGIN TRY/CATCH or CASE/END blocks are not balanced in the batch.",
                      "Close every block inside its own batch (a GO inside a block splits it)."),
    "cmp.dup_declare": ("P3A", "Duplicate variable declaration", "Variable is declared more than once in this batch.",
                        "Remove the duplicate DECLARE or separate the batches with GO."),
    "cmp.dup_create": ("P3A", "Duplicate CREATE TABLE / temp table", "Same table or temp table is created more than once in this batch.",
                       "Use different names or separate the batches with GO."),
    "cmp.batch_first": ("P3A", "Batch structure", "CREATE/ALTER PROCEDURE/FUNCTION/VIEW/TRIGGER must be the first statement in its batch.",
                        "Add GO before the CREATE/ALTER statement."),
    "cmp.go_format": ("P3A", "GO separator", "GO line is not handled the way the script expects.",
                      "Put GO alone on its own line, outside comments and strings."),
    "cmp.alter_drop": ("P3A", "ALTER TABLE ... DROP", "'ALTER TABLE ... DROP <name>' without COLUMN is parsed as DROP CONSTRAINT.",
                       "Write ALTER TABLE ... DROP COLUMN <name>."),
    "cmp.sqlcmd_var": ("P3A", "sqlcmd variable", "$(name) is substituted by sqlcmd; an undefined variable aborts the run.",
                       "Remove the $( ) text or escape it; the bat calls sqlcmd without -x."),
    "cmp.quoted_ident": ("P3A", "QUOTED_IDENTIFIER", "sqlcmd runs with QUOTED_IDENTIFIER OFF (no -I), so \"x\" is a string literal, not an identifier.",
                         "Use [brackets] instead of double quotes, or add SET QUOTED_IDENTIFIER ON before the statement."),
    "cmp.use_db": ("P3A", "USE statement", "Script switches database; the bat already connects with -d.",
                   "Remove the USE statement or make sure it targets the deployment database."),
    "cmp.sqlcmd": ("P3A", "sqlcmd compile (NOEXEC)", "SQL Server reported an error while compiling the batch.",
                   "Fix the reported error; this is what sqlcmd -b would fail on."),
    "dep.order": ("P45", "Deploy dependency order", "Script references an in-release object that is deployed later.",
                  "Move the referenced object's script earlier in the changelog."),
    "rb.order": ("P46", "Rollback dependency order", "A dependency is removed before the scripts that depend on it are rolled back.",
                 "Roll back dependents first (reverse of the deploy order)."),
}

PHASES = OrderedDict([
    ("P2", ("Phase 2 – File Inventory", "XML vs disk counts, entity sets, cross-schema content", "inventory")),
    ("P3", ("Phase 3 – Script Verification", "Script pairing, rollback logic, schema references", "verification")),
    ("P3A", ("Phase 3A – SQL Compilation", "Static compilation and simulated execution", "compilation")),
    ("P45", ("Phase 4 & 5 – Deploy Sequence", "Dependency ordering of deploy scripts", "deploy")),
    ("P46", ("Phase 4.1 & 6 – Rollback Sequence", "Dependency ordering of rollback scripts", "rollback")),
    ("P7", ("Phase 7 – XML Integrity", "Changelog structure and sqlFile attributes", "xml")),
])

SEV_RANK = {"CRITICAL": 0, "WARNING": 1, "INFO": 2, "OK": 3}
TYPE_LABEL = {"Table": "Table", "Tvf": "TVF", "Procedure": "Procedure", "Data": "Data", "Other": "Other"}
TYPE_FOLDER_LABEL = {"Table": "Tables", "Tvf": "TVFs", "Procedure": "Procedures", "Data": "Data", "Other": "Other"}

# --------------------------------------------------------------------------------------------
# Lexer
# --------------------------------------------------------------------------------------------


class Tok:
    __slots__ = ("kind", "text", "line", "up", "val")

    def __init__(self, kind, text, line, val=None):
        self.kind = kind      # W word, I [ident], Q "ident", S 'string', V @var, T #temp, N number, P punct
        self.text = text
        self.line = line
        self.val = text if val is None else val
        self.up = self.val.upper() if kind == "W" else ""


_WS = re.compile("[ \t\r\f\v\u00a0\u200b\u200c\u200d\u2060\ufeff\u3000]+")
_WORD = re.compile(r"[^\W\d][\w@$#]*")
_NUM = re.compile(r"0[xX][0-9A-Fa-f]*|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?")
_VAR = re.compile(r"@@?[\w@$#]*")
_TEMP = re.compile(r"##?[\w@$#]+")


def _scan_quoted(text, i, close):
    """Scan a literal starting at text[i] (opening char) with doubled-close escaping."""
    n = len(text)
    j = i + 1
    while j < n:
        k = text.find(close, j)
        if k < 0:
            return n, False
        if k + 1 < n and text[k + 1] == close:
            j = k + 2
            continue
        return k + 1, True
    return n, False


def lex(text):
    toks, spans, errors = [], [], []
    i, n, line = 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        m = _WS.match(text, i)
        if m:
            i = m.end()
            continue
        if c == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and text.startswith("/*", i):
            start, depth, j = line, 0, i
            while j < n:
                if text.startswith("/*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*/", j):
                    depth -= 1
                    j += 2
                    if depth == 0:
                        break
                else:
                    if text[j] == "\n":
                        line += 1
                    j += 1
            if depth > 0:
                errors.append((start, "Unterminated /* comment */ starting at L%d." % start))
            spans.append(("C", start, line))
            i = j
            continue
        if c == "'" or (c in "Nn" and i + 1 < n and text[i + 1] == "'"):
            q = i if c == "'" else i + 1
            j, closed = _scan_quoted(text, q, "'")
            s = text[i:j]
            start = line
            if not closed:
                errors.append((start, "Unterminated string literal starting at L%d." % start))
            toks.append(Tok("S", s, start))
            line += s.count("\n")
            spans.append(("S", start, line))
            i = j
            continue
        if c == "[" or c == '"':
            close = "]" if c == "[" else '"'
            j, closed = _scan_quoted(text, i, close)
            s = text[i:j]
            if not closed:
                errors.append((line, "Unterminated %s identifier starting at L%d." % ("[...]" if c == "[" else '"..."', line)))
                inner = s[1:]
            else:
                inner = s[1:-1].replace(close * 2, close)
            toks.append(Tok("I" if c == "[" else "Q", s, line, inner))
            line += s.count("\n")
            i = j
            continue
        if c == "@":
            m = _VAR.match(text, i)
            toks.append(Tok("V", m.group(), line))
            i = m.end()
            continue
        if c == "#":
            m = _TEMP.match(text, i)
            if m:
                toks.append(Tok("T", m.group(), line))
                i = m.end()
                continue
        if c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            m = _NUM.match(text, i)
            toks.append(Tok("N", m.group(), line))
            i = m.end()
            continue
        m = _WORD.match(text, i)
        if m:
            toks.append(Tok("W", m.group(), line))
            i = m.end()
            continue
        toks.append(Tok("P", c, line))
        i += 1
    return toks, spans, errors


def kw(t, *words):
    return t is not None and t.kind == "W" and t.up in words


def punct(t, ch):
    return t is not None and t.kind == "P" and t.text == ch


def tok_at(toks, i):
    return toks[i] if 0 <= i < len(toks) else None


def read_name(toks, i):
    """Read a dotted multipart name starting at i. Returns (parts, next_index)."""
    t = tok_at(toks, i)
    if t is None or t.kind not in ("W", "I", "Q", "T"):
        return None, i
    parts, j = [t.val], i + 1
    while punct(tok_at(toks, j), "."):
        nx = tok_at(toks, j + 1)
        if nx is not None and nx.kind in ("W", "I", "Q"):
            parts.append(nx.val)
            j += 2
        elif punct(nx, "."):
            parts.append("")
            j += 1
        else:
            break
    return parts, j


STMT_START = {"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "IF", "ELSE", "BEGIN", "END", "DECLARE", "SET",
              "EXEC", "EXECUTE", "WHILE", "RETURN", "PRINT", "RAISERROR", "THROW", "CREATE", "ALTER", "DROP",
              "TRUNCATE", "OPEN", "FETCH", "CLOSE", "DEALLOCATE", "GOTO", "BREAK", "CONTINUE", "COMMIT",
              "ROLLBACK", "SAVE", "USE", "GRANT", "REVOKE", "DENY", "WAITFOR", "GO"}
DECL_STOP = STMT_START - {"END", "ELSE"}
OBJ_KINDS = {"PROCEDURE": "PROCEDURE", "PROC": "PROCEDURE", "FUNCTION": "FUNCTION", "TABLE": "TABLE",
             "VIEW": "VIEW", "TRIGGER": "TRIGGER", "SYNONYM": "SYNONYM", "TYPE": "TYPE"}
ROUTINE_KINDS = ("PROCEDURE", "PROC", "FUNCTION", "VIEW", "TRIGGER")
TYPE_KINDS = {"Tvf": ("FUNCTION",), "Procedure": ("PROCEDURE",), "Table": ("TABLE",)}
GO_LINE = re.compile(r"^\s*go(?:\s+\d+)?\s*(?:--.*)?$", re.I)
HIDDEN = {"\u00a0": "NBSP", "\u200b": "zero-width space", "\u200c": "ZWNJ", "\u200d": "ZWJ", "\u2060": "word joiner",
          "\ufeff": "BOM/ZWNBSP", "\u2028": "line separator", "\u2029": "paragraph separator", "\u3000": "ideographic space"}

# --------------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------------


@dataclass
class Finding:
    check: str
    severity: str
    section: str
    stype: str
    file: str = ""
    lines: List[int] = field(default_factory=list)
    detail: str = ""


@dataclass
class Batch:
    start: int
    end: int
    toks: list


@dataclass
class Op:
    op: str
    kind: str
    parts: list
    line: int
    end: int


@dataclass
class Entry:
    xml_path: str
    rel: str
    abspath: str
    line: int
    section: str           # Deploy / Rollback / NotRun (XML placement, as the bats see it)
    folder_section: str    # Deploy / Rollback / Unknown (from the path)
    stype: str
    schema: str
    obj: str
    fname: str
    base: str
    tail: str
    attrs: Dict[str, str] = field(default_factory=dict)
    changeset: str = ""
    exists: bool = False
    fa: Optional["FileAnalysis"] = None
    deps: list = field(default_factory=list)   # [(Entry, line)]
    compile_status: str = ""


class FileAnalysis:
    def __init__(self):
        self.text = ""
        self.lines: List[str] = []
        self.toks: List[Tok] = []
        self.spans = []
        self.batches: List[Batch] = []
        self.ops: List[Op] = []
        self.main_op = "NONE"
        self.main_kind = ""
        self.op_display = "—"
        self.dml: List[str] = []
        self.refs = []
        self.added_cols: Dict[str, List[str]] = defaultdict(list)
        self.dropped_cols: Dict[str, List[str]] = defaultdict(list)
        self.preexisting_marker = False
        self.compile_unsafe = ""
        self.readable = True


class Ctx:
    def __init__(self, cfg):
        self.cfg = cfg
        self.findings: List[Finding] = []
        self.suppressed = 0

    def add(self, check, severity, section="", stype="", file="", lines=None, detail=""):
        self.findings.append(Finding(check, severity, section, stype, file,
                                     sorted(set(l for l in (lines or []) if l)), detail))


# --------------------------------------------------------------------------------------------
# Changelog parsing (expat, keeps line numbers)
# --------------------------------------------------------------------------------------------


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


# --------------------------------------------------------------------------------------------
# Classification helpers
# --------------------------------------------------------------------------------------------

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
    return ", ".join("L%d" % l for l in lines[:6]) + (" …" if len(lines) > 6 else "")


# --------------------------------------------------------------------------------------------
# File analysis
# --------------------------------------------------------------------------------------------


def read_sql(path, encoding, ctx, e, report=True):
    with open(path, "rb") as f:
        raw = f.read()
    sec, st, rel = (e.section, e.stype, e.rel) if e else ("", "", "")
    add = ctx.add if report else (lambda *a, **k: None)
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        add("cmp.encoding", "CRITICAL", sec, st, rel, [1],
            "File is UTF-16 (SSMS 'Unicode'); the bat reads input as UTF-8 (-f 65001).")
        return raw.decode("utf-16"), False
    if b"\x00" in raw[:4000]:
        add("cmp.encoding", "CRITICAL", sec, st, rel, [1],
            "File contains NUL bytes (looks like UTF-16 without BOM); sqlcmd -f 65001 cannot read it.")
        return raw.decode("utf-16-le", errors="replace"), False
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[3:]
    enc = encoding or "utf-8"
    try:
        return raw.decode(enc), True
    except LookupError:
        return raw.decode("utf-8", errors="replace"), True
    except UnicodeDecodeError as ex:
        line = raw[:ex.start].count(b"\n") + 1
        add("cmp.encoding", "WARNING", sec, st, rel, [line],
            "Bytes at L%d are not valid %s (probably ANSI/cp1252); non-ASCII text will be garbled." % (line, enc))
        return raw.decode(enc, errors="replace"), True


def split_batches(fa, ctx, e):
    toks, lines = fa.toks, fa.lines
    by_line = defaultdict(list)
    for idx, t in enumerate(toks):
        by_line[t.line].append(idx)
    sep = set()
    sep_lines = set()
    for ln in sorted(by_line):
        idxs = by_line[ln]
        t0 = toks[idxs[0]]
        if kw(t0, "GO"):
            rest = [toks[k] for k in idxs[1:]]
            if not rest or (len(rest) == 1 and rest[0].kind == "N"):
                sep.update(idxs)
                sep_lines.add(ln)
                if not GO_LINE.match(lines[ln - 1]):
                    ctx.add("cmp.go_format", "WARNING", e.section, e.stype, e.rel, [ln],
                            "GO at L%d shares its line with other text; sqlcmd may not treat it as a separator." % ln)
            else:
                ctx.add("cmp.go_format", "CRITICAL", e.section, e.stype, e.rel, [ln],
                        "'GO %s' at L%d is not a valid batch separator; sqlcmd fails to parse it." % (rest[0].text, ln))
        else:
            last = toks[idxs[-1]]
            if kw(last, "GO") and len(idxs) > 1:
                ctx.add("cmp.go_format", "CRITICAL", e.section, e.stype, e.rel, [ln],
                        "GO at the end of L%d is not on its own line, so it does not separate batches." % ln)
    # GO lines hidden inside block comments / multi-line strings
    for ln, text in enumerate(lines, 1):
        if ln in sep_lines or not GO_LINE.match(text):
            continue
        for kind, s, en in fa.spans:
            if s < ln <= en:
                if kind == "C":
                    ctx.add("cmp.go_format", "CRITICAL", e.section, e.stype, e.rel, [s, ln],
                            "GO at L%d is inside a /* */ comment opened at L%d; sqlcmd still splits the batch there "
                            "(Msg 113 'Missing end comment mark')." % (ln, s))
                else:
                    ctx.add("cmp.go_format", "WARNING", e.section, e.stype, e.rel, [s, ln],
                            "GO at L%d is inside a multi-line string starting at L%d; sqlcmd may split the batch there." % (ln, s))
                break
    batches, cur, start = [], [], 1
    for idx, t in enumerate(toks):
        if idx in sep:
            if kw(t, "GO"):
                batches.append(Batch(start, t.line - 1, cur))
                cur, start = [], t.line + 1
            continue
        cur.append(t)
    batches.append(Batch(start, max(len(lines), start), cur))
    return batches


def find_routine_headers(toks):
    res = []
    n = len(toks)
    for i, t in enumerate(toks):
        if kw(t, "CREATE", "ALTER") and not punct(tok_at(toks, i - 1), ".") \
                and not (t.up == "ALTER" and kw(tok_at(toks, i - 1), "OR")):
            j, op = i + 1, t.up
            if t.up == "CREATE" and kw(tok_at(toks, j), "OR") and kw(tok_at(toks, j + 1), "ALTER"):
                j, op = j + 2, "CREATE OR ALTER"
            if j < n and kw(toks[j], *ROUTINE_KINDS):
                kind = "PROCEDURE" if toks[j].up == "PROC" else toks[j].up
                parts, e = read_name(toks, j + 1)
                res.append((i, e, kind, op, parts or []))
    return res


def check_batch(b, e, ctx):
    toks = b.toks
    n = len(toks)
    sec, st, rel = e.section, e.stype, e.rel
    heads = find_routine_headers(toks)

    # batch structure
    if heads:
        h = heads[0]
        pre = [t for t in toks[:h[0]] if not punct(t, ";")]
        if pre:
            ctx.add("cmp.batch_first", "CRITICAL", sec, st, rel, [pre[0].line, toks[h[0]].line],
                    "%s %s at L%d is not the first statement in its batch (batch starts with '%s' at L%d)."
                    % (h[3], h[2], toks[h[0]].line, pre[0].text, pre[0].line))
        for h2 in heads[1:]:
            ctx.add("cmp.batch_first", "CRITICAL", sec, st, rel, [toks[h[0]].line, toks[h2[0]].line],
                    "%s %s at L%d is in the same batch as the %s at L%d; GO is missing between them."
                    % (h2[3], h2[2], toks[h2[0]].line, h[2], toks[h[0]].line))

    # duplicate variable declarations (incl. routine parameters)
    declared = {}

    def decl(t):
        k = t.text.lower()
        if k in declared:
            ctx.add("cmp.dup_declare", "CRITICAL", sec, st, rel, [declared[k], t.line],
                    "Variable %s is declared more than once in this batch (L%d and L%d)." % (t.text, declared[k], t.line))
        else:
            declared[k] = t.line

    if heads and heads[0][2] in ("PROCEDURE", "FUNCTION"):
        j, depth, prev = heads[0][1], 0, None
        while j < n:
            t = toks[j]
            if punct(t, "("):
                depth += 1
            elif punct(t, ")"):
                depth -= 1
            elif depth == 0 and kw(t, "AS", "RETURNS", "WITH", "BEGIN"):
                if t.up == "RETURNS" and tok_at(toks, j + 1) is not None and toks[j + 1].kind == "V":
                    decl(toks[j + 1])
                break
            elif t.kind == "V" and (prev is None or punct(prev, "(") or punct(prev, ",")):
                decl(t)
            prev = t
            j += 1
    for i, t in enumerate(toks):
        if not kw(t, "DECLARE"):
            continue
        j, depth, expect = i + 1, 0, True
        while j < n:
            u = toks[j]
            if punct(u, "("):
                depth += 1
            elif punct(u, ")"):
                depth -= 1
                if depth < 0:
                    break
            elif depth == 0:
                if punct(u, ","):
                    expect = True
                    j += 1
                    continue
                if punct(u, ";"):
                    break
                if u.kind == "V" and expect:
                    decl(u)
                elif u.kind == "W" and u.up in DECL_STOP:
                    break
                expect = False
            j += 1

    # duplicate CREATE TABLE / SELECT INTO #temp
    created = {}
    last_kw, depth = {}, 0

    def reg(parts, line):
        key = ".".join(x.lower() for x in parts if x)
        temp = parts[-1].startswith("#")
        if key in created:
            ctx.add("cmp.dup_create", "CRITICAL" if temp else "WARNING", sec, st, rel, [created[key], line],
                    "%s %s is created more than once in this batch (L%d and L%d)."
                    % ("Temp table" if temp else "Table", ".".join(parts), created[key], line))
        else:
            created[key] = line

    for i, t in enumerate(toks):
        if punct(t, "("):
            depth += 1
            last_kw.pop(depth, None)
            continue
        if punct(t, ")"):
            last_kw.pop(depth, None)
            depth = max(0, depth - 1)
            continue
        if kw(t, "SELECT", "OUTPUT", "INSERT", "MERGE", "FETCH", "UPDATE", "DELETE"):
            last_kw[depth] = t.up
        if kw(t, "CREATE") and kw(tok_at(toks, i + 1), "TABLE"):
            parts, _ = read_name(toks, i + 2)
            if parts:
                reg(parts, t.line)
        elif kw(t, "INTO") and tok_at(toks, i + 1) is not None and toks[i + 1].kind == "T" \
                and last_kw.get(depth) == "SELECT":
            reg([toks[i + 1].text], toks[i + 1].line)

    # BEGIN/END pairing
    stack = []
    for i, t in enumerate(toks):
        if t.kind != "W" or punct(tok_at(toks, i - 1), "."):
            continue
        nx = tok_at(toks, i + 1)
        if t.up == "BEGIN":
            if kw(nx, "TRAN", "TRANSACTION", "DISTRIBUTED", "CONVERSATION", "DIALOG"):
                continue
            stack.append(("TRY" if kw(nx, "TRY") else "CATCH" if kw(nx, "CATCH") else "BEGIN", t.line))
        elif t.up == "CASE":
            stack.append(("CASE", t.line))
        elif t.up == "END":
            if kw(nx, "CONVERSATION"):
                continue
            want = nx.up if kw(nx, "TRY", "CATCH") else None
            if not stack:
                ctx.add("cmp.begin_end", "CRITICAL", sec, st, rel, [t.line],
                        "END at L%d has no matching BEGIN/CASE in this batch." % t.line)
                continue
            kind, ln = stack.pop()
            if want and kind != want:
                ctx.add("cmp.begin_end", "CRITICAL", sec, st, rel, [ln, t.line],
                        "END %s at L%d closes %s opened at L%d." % (want, t.line, kind, ln))
            elif not want and kind in ("TRY", "CATCH"):
                ctx.add("cmp.begin_end", "CRITICAL", sec, st, rel, [ln, t.line],
                        "END at L%d closes BEGIN %s (L%d); expected END %s." % (t.line, kind, ln, kind))
    for kind, ln in stack:
        label = "CASE" if kind == "CASE" else ("BEGIN " + kind if kind in ("TRY", "CATCH") else "BEGIN")
        ctx.add("cmp.begin_end", "CRITICAL", sec, st, rel, [ln, b.end],
                "%s at L%d has no matching END (batch ends at L%d)." % (label, ln, b.end))

    # parentheses
    pst = []
    for t in toks:
        if punct(t, "("):
            pst.append(t.line)
        elif punct(t, ")"):
            if pst:
                pst.pop()
            else:
                ctx.add("cmp.parens", "CRITICAL", sec, st, rel, [t.line], "Unmatched ) at L%d." % t.line)
    for ln in pst:
        ctx.add("cmp.parens", "CRITICAL", sec, st, rel, [ln, b.end],
                "Unmatched ( opened at L%d is never closed (batch ends at L%d)." % (ln, b.end))


def extract_ops(toks):
    ops, n, i = [], len(toks), 0
    while i < n:
        t = toks[i]
        if kw(t, "CREATE", "ALTER", "DROP") and not punct(tok_at(toks, i - 1), "."):
            j, op = i + 1, t.up
            if op == "CREATE" and kw(tok_at(toks, j), "OR") and kw(tok_at(toks, j + 1), "ALTER"):
                op, j = "CREATE OR ALTER", j + 2
            if j < n and kw(toks[j], *OBJ_KINDS):
                kind = OBJ_KINDS[toks[j].up]
                j += 1
                if op == "DROP" and kw(tok_at(toks, j), "IF") and kw(tok_at(toks, j + 1), "EXISTS"):
                    j += 2
                while True:
                    parts, e = read_name(toks, j)
                    if not parts:
                        break
                    ops.append(Op(op, kind, parts, t.line, e))
                    j = e
                    if op == "DROP" and punct(tok_at(toks, j), ","):
                        j += 1
                        continue
                    break
                i = max(j, i + 1)
                continue
        i += 1
    return ops


CONSTRAINT_WORDS = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "DEFAULT", "INDEX", "PERIOD"}


def parse_add_columns(toks, j):
    cols, depth, expect, n = [], 0, True, len(toks)
    while j < n:
        t = toks[j]
        if punct(t, "("):
            depth += 1
        elif punct(t, ")"):
            depth -= 1
            if depth < 0:
                break
        elif depth == 0:
            if punct(t, ","):
                expect = True
                j += 1
                continue
            if punct(t, ";"):
                break
            if expect:
                if t.kind in ("W", "I", "Q") and not (t.kind == "W" and t.up in CONSTRAINT_WORDS):
                    cols.append(t.val)
                expect = False
            elif t.kind == "W" and t.up in STMT_START:
                break
        j += 1
    return cols


def parse_drop_columns(toks, j):
    cols, n = [], len(toks)
    if kw(tok_at(toks, j), "IF") and kw(tok_at(toks, j + 1), "EXISTS"):
        j += 2
    while j < n:
        t = toks[j]
        if t.kind in ("W", "I", "Q") and not kw(t, "COLUMN", "CONSTRAINT"):
            cols.append(t.val)
            j += 1
        else:
            break
        if punct(tok_at(toks, j), ","):
            j += 1
            if kw(tok_at(toks, j), "COLUMN"):
                j += 1
            if kw(tok_at(toks, j), "CONSTRAINT"):
                break
            continue
        break
    return cols


def main_op(ops, obj, kinds):
    seq = [o for o in ops if o.kind in kinds and o.parts and o.parts[-1].lower() == obj.lower()]
    if not seq:
        return "NONE", ""
    names = [o.op for o in seq]
    kind = seq[0].kind
    if names[-1] == "DROP":
        return "DROP", kind     # final state: object removed (e.g. DROP COLUMN ... then DROP TABLE)
    if "DROP" in names:
        di = names.index("DROP")
        if any(x.startswith("CREATE") for x in names[di + 1:]):
            return "RECREATE", kind
    for cand in ("CREATE OR ALTER", "CREATE", "ALTER", "DROP"):
        if cand in names:
            return cand, kind
    return "NONE", kind


def analyze_file(e, cfg, ctx, checks=True):
    fa = FileAnalysis()
    sink = ctx if checks else Ctx(cfg)
    text, fa.readable = read_sql(e.abspath, e.attrs.get("encoding") or cfg["expected_encoding"], sink, e)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    fa.text = text
    fa.lines = text.split("\n")
    fa.toks, fa.spans, lex_errors = lex(text)
    fa.preexisting_marker = bool(re.search(r"verify\s*:\s*preexisting", text, re.I))
    sec, st, rel = e.section, e.stype, e.rel
    for ln, msg in lex_errors:
        sink.add("cmp.unterminated", "CRITICAL", sec, st, rel, [ln], msg)

    # hidden characters
    hidden = defaultdict(list)
    for ln, line in enumerate(fa.lines, 1):
        for ch, name in HIDDEN.items():
            if ch in line and not (ln == 1 and ch == "\ufeff" and line.startswith(ch)):
                hidden[name].append(ln)
    for name, lns in hidden.items():
        sink.add("cmp.hidden_chars", "WARNING", sec, st, rel, lns[:10],
                 "%s found at %s." % (name, lines_str(lns)))

    # sqlcmd scripting-variable and command lines
    var_lines = [ln for ln, line in enumerate(fa.lines, 1) if re.search(r"\$\([A-Za-z_][\w]*\)", line)]
    if var_lines:
        sink.add("cmp.sqlcmd_var", "CRITICAL", sec, st, rel, var_lines[:10],
                 "$(...) at %s is a sqlcmd scripting variable; sqlcmd stops with 'scripting variable not defined'."
                 % lines_str(var_lines))
    cmd_lines = [ln for ln, line in enumerate(fa.lines, 1)
                 if re.match(r"^\s*(:[A-Za-z]|!!|(exit|quit)\b)", line, re.I)]
    if cmd_lines:
        fa.compile_unsafe = "contains sqlcmd command lines (%s)" % lines_str(cmd_lines)
    if any(kw(t, "NOEXEC") for t in fa.toks):
        fa.compile_unsafe = "script toggles SET NOEXEC"

    fa.batches = split_batches(fa, sink, e)
    if checks:
        for b in fa.batches:
            check_batch(b, e, ctx)

    toks = fa.toks
    fa.ops = extract_ops(toks)

    # ALTER TABLE actions (added / dropped columns, DROP without COLUMN)
    for o in fa.ops:
        if o.op != "ALTER" or o.kind != "TABLE":
            continue
        j = o.end
        if kw(tok_at(toks, j), "WITH") and kw(tok_at(toks, j + 1), "CHECK", "NOCHECK"):
            j += 2
        a = tok_at(toks, j)
        key = o.parts[-1].lower()
        if kw(a, "ADD"):
            fa.added_cols[key] += parse_add_columns(toks, j + 1)
        elif kw(a, "DROP"):
            nx = tok_at(toks, j + 1)
            if kw(nx, "COLUMN"):
                fa.dropped_cols[key] += parse_drop_columns(toks, j + 2)
            elif kw(nx, "CONSTRAINT", "PERIOD", "INDEX"):
                pass
            elif nx is not None:
                sev = "WARNING" if kw(nx, "IF") else "CRITICAL"
                nm = tok_at(toks, j + 3).val if kw(nx, "IF") and tok_at(toks, j + 3) else nx.val
                sink.add("cmp.alter_drop", sev, sec, st, rel, [a.line],
                         "ALTER TABLE %s DROP %s at L%d drops a CONSTRAINT named '%s', not a column; "
                         "write DROP COLUMN." % (".".join(o.parts), nm, a.line, nm))

    kinds = TYPE_KINDS.get(e.stype)
    if kinds:
        fa.main_op, fa.main_kind = main_op(fa.ops, e.obj, kinds)
        if fa.main_op == "NONE":
            fa.op_display = "—"
        elif fa.main_op == "RECREATE":
            fa.op_display = "DROP + CREATE " + fa.main_kind
        else:
            fa.op_display = fa.main_op + " " + fa.main_kind
        if e.stype == "Table" and fa.main_op == "ALTER":
            k = e.obj.lower()
            bits = []
            if fa.added_cols.get(k):
                bits.append("ADD " + ", ".join(fa.added_cols[k]))
            if fa.dropped_cols.get(k):
                bits.append("DROP COLUMN " + ", ".join(fa.dropped_cols[k]))
            if bits:
                fa.op_display += " (" + "; ".join(bits) + ")"
    else:
        seen = []
        for i, t in enumerate(toks):
            if kw(t, "INSERT", "UPDATE", "DELETE", "MERGE") or (kw(t, "TRUNCATE") and kw(tok_at(toks, i + 1), "TABLE")):
                if t.up not in seen:
                    seen.append(t.up)
        fa.dml = seen
        fa.op_display = ", ".join(seen) if seen else "—"

    # references to multipart / single names
    refs, n, i = [], len(toks), 0
    while i < n:
        t = toks[i]
        if t.kind in ("W", "I", "Q") and not punct(tok_at(toks, i - 1), "."):
            parts, j = read_name(toks, i)
            schema = parts[-2] if len(parts) >= 2 else None
            refs.append((schema, parts[-1], t.line))
            i = j
            continue
        i += 1
    fa.refs = refs

    if checks:
        # QUOTED_IDENTIFIER OFF under sqlcmd
        qi_on_line = None
        for i, t in enumerate(toks):
            if kw(t, "QUOTED_IDENTIFIER") and kw(tok_at(toks, i - 1), "SET") and kw(tok_at(toks, i + 1), "ON"):
                qi_on_line = t.line
                break
        q_lines = [t.line for t in toks if t.kind == "Q" and (qi_on_line is None or t.line < qi_on_line)]
        if q_lines:
            ctx.add("cmp.quoted_ident", "WARNING", sec, st, rel, q_lines[:10],
                    "Double-quoted identifier(s) at %s; sqlcmd (no -I) treats them as string literals." % lines_str(q_lines))
        # USE <db>
        target_db = (cfg.get("sqlcmd") or {}).get("database")
        for i, t in enumerate(toks):
            if kw(t, "USE") and (i == 0 or toks[i - 1].line != t.line or punct(toks[i - 1], ";")):
                nx = tok_at(toks, i + 1)
                if nx is not None and nx.kind in ("W", "I", "Q"):
                    if target_db and nx.val.lower() == str(target_db).lower():
                        continue
                    ctx.add("cmp.use_db", "WARNING", sec, st, rel, [t.line],
                            "USE %s at L%d switches database (bat connects to %s)." % (nx.val, t.line, target_db or "-d"))
    return fa


def normalized_hash(fa, schemas):
    pat = re.compile(r"\[?\b(?:%s)\b\]?\s*\." % "|".join(re.escape(s) for s in schemas), re.I)
    out, toks = [], fa.toks
    for i, t in enumerate(toks):
        if t.kind in ("W", "I", "Q") and t.val.lower() in schemas and punct(tok_at(toks, i + 1), "."):
            out.append("<SCHEMA>")
        elif t.kind == "S":
            out.append(pat.sub("<SCHEMA>.", t.text))
        elif t.kind in ("W", "I", "Q"):
            out.append(t.val)
        else:
            out.append(t.text)
    return hashlib.sha256("\x1f".join(out).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------------------------


def phase7_xml(ctx, cl, entries, changelog_dir, cfg):
    if not cl.ok:
        return
    if cl.root_tag != "databaseChangeLog":
        ctx.add("xml.structure", "CRITICAL", "XML", "", cl.path, [1],
                "Root element is <%s>, expected <databaseChangeLog>." % cl.root_tag)
    if not any(k == "xmlns" or k.startswith("xmlns") for k in cl.root_attrs):
        ctx.add("xml.structure", "WARNING", "XML", "", cl.path, [1], "Root element has no xmlns declaration.")
    if not cl.changesets:
        ctx.add("xml.structure", "CRITICAL", "XML", "", cl.path, [1], "No <changeSet> element found.")
    seen = {}
    for cid, author, line in cl.changesets:
        if not cid or not author:
            ctx.add("xml.structure", "CRITICAL", "XML", "", cl.path, [line],
                    "changeSet at L%d is missing id or author." % line)
        key = (cid, author)
        if key in seen:
            ctx.add("xml.duplicate", "CRITICAL", "XML", "", cl.path, [seen[key], line],
                    "changeSet id '%s' (author '%s') is declared twice (L%d, L%d)." % (cid, author, seen[key], line))
        else:
            seen[key] = line
    paths = {}
    for e in entries:
        if not e.xml_path:
            ctx.add("xml.structure", "CRITICAL", e.section, e.stype, cl.path, [e.line], "sqlFile at L%d has no path." % e.line)
            continue
        key = (e.section, e.rel.lower())
        if key in paths:
            ctx.add("xml.duplicate", "CRITICAL", e.section, e.stype, e.rel, [paths[key], e.line],
                    "%s sqlFile path is listed twice (XML L%d and L%d); the bat runs it twice." % (e.section, paths[key], e.line))
        else:
            paths[key] = e.line
        dbms = e.attrs.get("dbms")
        if dbms and cfg["allowed_dbms"] and dbms.lower() not in [d.lower() for d in cfg["allowed_dbms"]]:
            ctx.add("xml.attributes", "WARNING", e.section, e.stype, e.rel, [e.line],
                    "dbms='%s' at XML L%d (expected %s)." % (dbms, e.line, "/".join(cfg["allowed_dbms"])))
        enc = e.attrs.get("encoding")
        if enc and enc.replace("-", "").lower() != cfg["expected_encoding"].replace("-", "").lower():
            ctx.add("xml.attributes", "WARNING", e.section, e.stype, e.rel, [e.line],
                    "encoding='%s' at XML L%d, but the bat always reads files as UTF-8 (-f 65001)." % (enc, e.line))
        if e.section == "NotRun":
            ctx.add("xml.not_run", "WARNING", "XML", e.stype, e.rel, [e.line],
                    "sqlFile at XML L%d is not run by run_changelog_sql.bat or run_rollback_sql.bat." % e.line)
        elif e.folder_section != "Unknown" and e.folder_section != e.section:
            ctx.add("xml.placement", "CRITICAL", e.section, e.stype, e.rel, [e.line],
                    "%s-folder script at XML L%d is placed in the %s part of the changelog, so it runs during %s."
                    % (e.folder_section, e.line, e.section.lower(), e.section.lower()))
    for tag, attrs, line in cl.includes:
        f = attrs.get("file") or attrs.get("path") or ""
        cand = [os.path.join(changelog_dir, f)]
        if not any(os.path.exists(c) for c in cand):
            ctx.add("xml.include", "CRITICAL", "XML", "", cl.path, [line], "<%s> at L%d points to '%s', which does not exist." % (tag, line, f))
    # category order per section
    for section, order_key in (("Deploy", "deploy_category_order"), ("Rollback", "rollback_category_order")):
        order = cfg[order_key]
        rank = {t: i for i, t in enumerate(order)}
        max_rank, max_entry = -1, None
        for e in sorted([x for x in entries if x.section == section], key=lambda x: x.line):
            r = rank.get(e.stype)
            if r is None:
                continue
            if r < max_rank:
                ctx.add("xml.order", "WARNING", section, e.stype, e.rel, [e.line],
                        "%s sqlFile (%s) at XML L%d comes after %s entries (first at XML L%d); expected order %s."
                        % (section, TYPE_LABEL[e.stype], e.line, TYPE_LABEL[max_entry.stype], max_entry.line,
                           " → ".join(TYPE_FOLDER_LABEL[t] for t in order)))
            elif r > max_rank:
                max_rank, max_entry = r, e


def category_ranges(entries, section, order):
    res = []
    for t in order:
        ls = [e.line for e in entries if e.section == section and e.stype == t]
        if ls:
            res.append((t, min(ls), max(ls), len(ls)))
    return res


def phase2_inventory(ctx, cfg, entries, disk_entries):
    xml_keys = {e.rel.lower() for e in entries}
    for e in entries:
        if not e.exists:
            ctx.add("inv.missing", "CRITICAL", e.section, e.stype, e.rel, [e.line],
                    "Referenced at XML L%d; resolved to %s" % (e.line, e.abspath))
    for d in disk_entries:
        if d.rel.lower() not in xml_keys:
            ctx.add("inv.extra", "WARNING", d.folder_section, d.stype, d.rel, [], "Not referenced in the changelog.")

    expected = [s.lower() for s in cfg["expected_schemas"]]
    excl = [re.compile(p, re.I) for p in cfg["entity_set_exclude"]]
    groups = OrderedDict()
    for d in disk_entries:
        if d.stype not in cfg["entity_set_types"] or not d.schema:
            continue
        key = (d.folder_section, d.stype, d.base.lower())
        groups.setdefault(key, []).append(d)
    results = []
    for (section, stype, _), items in groups.items():
        present = sorted({d.schema.lower() for d in items})
        if not set(present) & set(expected):
            continue
        base = items[0].base
        if any(p.search(base) for p in excl):
            continue
        missing = [s for s in expected if s not in present]
        variants = [d for d in items if d.schema.lower() in expected and d.fa is not None]
        content = "N/A"
        diff_detail = ""
        if missing:
            content = "N/A (missing %s)" % ", ".join(missing)
        if len(variants) >= 2:
            hashes = defaultdict(list)
            for d in variants:
                hashes[normalized_hash(d.fa, expected)].append(d.schema.lower())
            if len(hashes) == 1:
                content = "MATCH" if not missing else content + "; present variants match"
            else:
                grp = sorted(hashes.values(), key=len, reverse=True)
                diff_detail = " ≠ ".join("/".join(sorted(g)) for g in grp)
                content = "DIFFERENT"
                ctx.add("inv.content", "WARNING", section, stype, base, [],
                        "%s: normalized content differs between schema variants (%s)." % (base, diff_detail))
        if missing:
            ctx.add("inv.entity_set", "CRITICAL", section, stype, base, [],
                    "%s — missing schema(s): %s" % (base, ", ".join(missing)))
        results.append(dict(base=base, section=section, stype=stype, expected=expected, present=present,
                            missing=missing, content=content, diff=diff_detail))
    return results


def phase3_verification(ctx, cfg, entries):
    dep = {e.tail: e for e in entries if e.section == "Deploy" and e.folder_section == "Deploy"}
    rb = {e.tail: e for e in entries if e.section == "Rollback" and e.folder_section == "Rollback"}

    def counterpart(e, to):
        parts = e.rel.split("/")
        low = [p.lower() for p in parts]
        k = low.index(e.folder_section.lower())
        return "/".join(parts[:k] + [to] + parts[k + 1:])

    for t, e in dep.items():
        if t not in rb:
            ctx.add("ver.pairing", "CRITICAL", "Deploy", e.stype, e.rel, [e.line],
                    "Deploy script (XML L%d) has no rollback counterpart %s in the <rollback> section."
                    % (e.line, counterpart(e, "Rollback")))
    for t, e in rb.items():
        if t not in dep:
            ctx.add("ver.pairing", "WARNING", "Rollback", e.stype, e.rel, [e.line],
                    "Rollback script (XML L%d) has no deploy counterpart %s." % (e.line, counterpart(e, "Deploy")))
    pre_objs = {o.lower() for o in cfg["preexisting_objects"]}
    coa = set(x.upper() for x in cfg["create_or_alter_rollback"])
    restore = {"ALTER", "CREATE OR ALTER", "RECREATE"}
    pairs = []
    for t, d in dep.items():
        r = rb.get(t)
        if not r or not d.fa or not r.fa:
            continue
        pairs.append((d, r))
        if d.stype in ("Tvf", "Procedure"):
            pre = d.fa.preexisting_marker or d.obj.lower() in pre_objs or ("%s.%s" % (d.schema, d.obj)).lower() in pre_objs
            allowed = {
                "CREATE": {"DROP"},
                "CREATE OR ALTER": restore if pre else coa,
                "ALTER": restore,
                "RECREATE": {"DROP"} | restore,
                "DROP": {"CREATE", "CREATE OR ALTER", "RECREATE"},
            }
            if d.fa.main_op == "NONE":
                ctx.add("ver.op_unknown", "WARNING", "Deploy", d.stype, d.rel, [],
                        "No CREATE/ALTER/DROP %s %s found in the deploy script." % (TYPE_KINDS[d.stype][0], d.obj))
                continue
            if r.fa.main_op == "NONE":
                ctx.add("ver.op_unknown", "WARNING", "Rollback", r.stype, r.rel, [],
                        "No CREATE/ALTER/DROP %s %s found in the rollback script." % (TYPE_KINDS[r.stype][0], r.obj))
                continue
            if r.fa.main_op not in allowed[d.fa.main_op]:
                exp = " / ".join(sorted(allowed[d.fa.main_op]))
                ctx.add("ver.rollback_logic", "CRITICAL", "Deploy", d.stype, d.rel, [],
                        "Deploy operation is %s but rollback is %s; expected rollback to %s."
                        % (d.fa.main_op, r.fa.op_display, exp))
        elif d.stype == "Table":
            key = d.obj.lower()
            if d.fa.main_op in ("CREATE", "CREATE OR ALTER") and r.fa.main_op not in ("DROP", "RECREATE"):
                ctx.add("ver.rollback_logic", "CRITICAL", "Deploy", d.stype, d.rel, [],
                        "Deploy creates table %s but rollback does not drop it (rollback: %s)." % (d.obj, r.fa.op_display))
            added = [c for c in d.fa.added_cols.get(key, [])]
            if added:
                dropped = {c.lower() for c in r.fa.dropped_cols.get(key, [])}
                miss = [c for c in added if c.lower() not in dropped]
                if miss and r.fa.main_op not in ("DROP", "RECREATE"):
                    ctx.add("ver.rollback_logic", "WARNING", "Deploy", d.stype, d.rel, [],
                            "Deploy adds column(s) %s to %s but rollback has no DROP COLUMN for them."
                            % (", ".join(miss), d.obj))
        elif d.stype == "Data":
            if ("INSERT" in d.fa.dml or "MERGE" in d.fa.dml) and not set(r.fa.dml) & {"DELETE", "TRUNCATE", "UPDATE", "MERGE"}:
                ctx.add("ver.rollback_logic", "WARNING", "Deploy", d.stype, d.rel, [],
                        "Deploy inserts data but the rollback script has no DELETE/UPDATE/MERGE/TRUNCATE.")
    # schema mismatch
    expected = {s.lower() for s in cfg["expected_schemas"]}
    allow = cfg["cross_schema_allow"]
    for e in entries:
        if not e.fa or not e.schema:
            continue
        fs = e.schema.lower()
        found = OrderedDict()
        for schema, name, line in e.fa.refs:
            if not schema:
                continue
            s = schema.lower()
            if s in expected and s != fs:
                ok = False
                for a in allow:
                    if a.get("from", "").lower() == fs and a.get("to", "").lower() == s \
                            and re.search(a.get("file_regex", ""), e.rel, re.I) \
                            and re.search(a.get("object_regex", ""), name, re.I):
                        ok = True
                        break
                if not ok:
                    found.setdefault((schema, name), []).append(line)
        for (schema, name), lns in found.items():
            ctx.add("ver.schema_mismatch", "CRITICAL", e.section, e.stype, e.rel, lns,
                    "Object reference [%s].[%s] in a [%s] file (expected schema: [%s])." % (schema, name, e.schema, e.schema))
    return pairs


def build_index(entries, section):
    idx = defaultdict(list)
    by_name = defaultdict(set)
    for e in entries:
        if e.section == section and e.stype in ("Table", "Tvf", "Procedure", "Data") and e.schema:
            idx[(e.schema.lower(), e.obj.lower())].append(e)
            by_name[e.obj.lower()].add(e.schema.lower())
    return idx, by_name


def resolve(schema, name, file_schema, by_name):
    nm = name.lower()
    if nm not in by_name:
        return None
    schemas = by_name[nm]
    if schema is not None:
        s = schema.lower()
        return (s, nm) if s in schemas else None
    for s in (file_schema.lower(), "dbo"):
        if s in schemas:
            return (s, nm)
    if len(schemas) == 1:
        return (next(iter(schemas)), nm)
    return None


def deps_of(e, idx, by_name):
    out = OrderedDict()
    if not e.fa:
        return []
    for schema, name, line in e.fa.refs:
        key = resolve(schema, name, e.schema, by_name)
        if not key:
            continue
        for x in idx[key]:
            if x is e:
                continue
            if e.stype == "Data":
                if x.stype not in ("Table", "Data"):
                    continue
            elif x.stype not in ("Table", "Tvf", "Procedure"):
                continue
            if e.stype != "Data" and x.obj.lower() == e.obj.lower() and x.schema.lower() == e.schema.lower():
                continue
            if x.rel not in out:
                out[x.rel] = (x, line)
    return list(out.values())


def phase45_dependencies(ctx, entries):
    d_idx, d_names = build_index(entries, "Deploy")
    r_idx, r_names = build_index(entries, "Rollback")
    deploy = [e for e in entries if e.section == "Deploy"]
    rollback = [e for e in entries if e.section == "Rollback"]
    for y in deploy:
        y.deps = deps_of(y, d_idx, d_names)
        for x, line in y.deps:
            if x.line > y.line:
                weak = y.stype == "Procedure" or (x.fa is not None and x.fa.main_op == "ALTER")
                ctx.add("dep.order", "WARNING" if weak else "CRITICAL", "Deploy", y.stype, y.rel, [line],
                        "%s (XML L%d) references %s.%s (%s) at L%d, but that script is deployed later (XML L%d)."
                        % (y.obj, y.line, x.schema, x.obj, TYPE_LABEL[x.stype], line, x.line))
    by_tail_rb = {e.tail: e for e in rollback}
    for y in rollback:
        deps = OrderedDict((x.rel, (x, line)) for x, line in deps_of(y, r_idx, r_names))
        # dependencies known from the deploy side
        dy = next((d for d in deploy if d.tail == y.tail), None)
        if dy:
            for x, line in dy.deps:
                rx = by_tail_rb.get(x.tail)
                if rx is not None and rx is not y and rx.rel not in deps:
                    deps[rx.rel] = (rx, 0)
        y.deps = list(deps.values())
        for x, line in y.deps:
            if x.line >= y.line or x.fa is None:
                continue
            destructive = x.stype == "Data" or x.fa.main_op == "DROP"
            if not destructive:
                continue
            what = "removes its rows" if x.stype == "Data" else "drops it"
            ctx.add("rb.order", "WARNING" if y.stype == "Procedure" else "CRITICAL", "Rollback", y.stype, y.rel,
                    [line] if line else [],
                    "Rollback of %s (XML L%d) depends on %s.%s (%s), whose rollback %s earlier at XML L%d."
                    % (y.obj, y.line, x.schema, x.obj, TYPE_LABEL[x.stype], what, x.line))


# --------------------------------------------------------------------------------------------
# Optional: sqlcmd compile under SET NOEXEC ON
# --------------------------------------------------------------------------------------------

MSG_RE = re.compile(r"^Msg (\d+), Level (\d+), State (\d+)(?:, Server [^,]*)?(?:, Procedure ([^,]*))?, Line (\d+)")
MARK = "##VERIFY_BATCH_"


def sqlcmd_compile(ctx, cfg, entries, release_objects, log):
    sc = cfg["sqlcmd"]
    exe = sc.get("exe") or "sqlcmd"
    exe_path = shutil.which(exe) or (exe if os.path.isfile(exe) else None)
    if not exe_path:
        return "sqlcmd not found on PATH; compile step skipped."
    if not sc.get("server") or not sc.get("database"):
        return "sqlcmd.server / sqlcmd.database not configured; compile step skipped."
    cmd = [exe_path, "-S", sc["server"], "-d", sc["database"], "-f", "65001", "-l", str(sc.get("login_timeout") or 15)]
    if sc.get("trusted", True):
        cmd.append("-E")
    else:
        pw = os.environ.get(sc.get("password_env") or "") or getpass.getpass("SQL password for %s: " % sc.get("user"))
        cmd += ["-U", sc.get("user") or "", "-P", pw]
    done = {}
    for e in entries:
        if not e.exists or not e.fa or e.rel in done:
            continue
        if e.fa.compile_unsafe:
            e.compile_status = "SKIP"
            ctx.add("cmp.sqlcmd", "INFO", e.section, e.stype, e.rel, [], "Not compiled: %s." % e.fa.compile_unsafe)
            continue
        parts = []
        for k, b in enumerate(e.fa.batches):
            body = "\n".join(e.fa.lines[b.start - 1:b.end])
            if not body.strip():
                continue
            parts.append("SET NOEXEC OFF\nGO\nPRINT '%s%d'\nGO\nSET NOEXEC ON\nGO\n%s\nGO\n" % (MARK, k, body))
        parts.append("SET NOEXEC OFF\nGO\n")
        fd, tmp = tempfile.mkstemp(suffix=".sql", prefix="verify_")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write("".join(parts))
        log("  compiling %s" % e.rel)
        try:
            out = subprocess.run(cmd + ["-i", tmp], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=sc.get("file_timeout") or 300).stdout.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            e.compile_status = "FAIL"
            ctx.add("cmp.sqlcmd", "WARNING", e.section, e.stype, e.rel, [], "sqlcmd timed out.")
            continue
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if "Login failed" in out or "Login timeout" in out or "network-related" in out:
            return "sqlcmd could not connect: " + out.strip().splitlines()[0][:300]
        status, cur, pending = "PASS", None, None
        lines_out = out.splitlines() + [MARK + "-1"]
        errors = []
        for line in lines_out:
            s = line.strip()
            if s.startswith(MARK):
                if pending:
                    errors.append(pending)
                    pending = None
                try:
                    cur = int(s[len(MARK):])
                except ValueError:
                    cur = None
                continue
            m = MSG_RE.match(s)
            if m:
                if pending:
                    errors.append(pending)
                pending = dict(num=int(m.group(1)), level=int(m.group(2)), line=int(m.group(5)), batch=cur, text="")
            elif pending is not None and s:
                pending["text"] = (pending["text"] + " " + s).strip()
        for err in errors:
            if err["level"] <= 10 or err["batch"] is None or err["batch"] >= len(e.fa.batches):
                continue
            b = e.fa.batches[err["batch"]]
            fline = b.start + err["line"] - 1
            sev = "CRITICAL"
            if err["num"] in (208, 2812, 4121, 207, 4104):
                names = re.findall(r"'([^']+)'", err["text"])
                if any(nm.split(".")[-1].strip("[]").lower() in release_objects for nm in names):
                    sev = "INFO"
            if sev == "CRITICAL":
                status = "FAIL"
            ctx.add("cmp.sqlcmd", sev, e.section, e.stype, e.rel, [fline],
                    "Msg %d (Level %d) at L%d: %s%s" % (err["num"], err["level"], fline, err["text"],
                                                         " (object is created earlier in this release)" if sev == "INFO" else ""))
        e.compile_status = status
        done[e.rel] = status
    for e in entries:
        if e.rel in done:
            e.compile_status = done[e.rel]
    return ""


# --------------------------------------------------------------------------------------------
# Issue aggregation
# --------------------------------------------------------------------------------------------


@dataclass
class Issue:
    num: int
    severity: str
    check: str
    phase: str
    section: str
    stype: str
    title: str
    message: str
    fix: str
    items: List[Finding]


def apply_rules(ctx):
    over = ctx.cfg["severity_overrides"]
    sup = ctx.cfg["suppress"]
    kept = []
    for f in ctx.findings:
        if f.check in over:
            f.severity = over[f.check].upper()
        drop = False
        for s in sup:
            if s.get("check") and s["check"] != f.check:
                continue
            if s.get("file_regex") and not re.search(s["file_regex"], f.file, re.I):
                continue
            if s.get("detail_regex") and not re.search(s["detail_regex"], f.detail, re.I):
                continue
            drop = True
            break
        if drop:
            ctx.suppressed += 1
        else:
            kept.append(f)
    ctx.findings = kept


PHASE_ORDER = {p: i for i, p in enumerate(PHASES)}


def aggregate(findings):
    groups = OrderedDict()
    for f in findings:
        key = (f.severity, f.check, f.section, f.stype)
        groups.setdefault(key, []).append(f)
    keys = sorted(groups, key=lambda k: (SEV_RANK[k[0]], PHASE_ORDER[CHECKS[k[1]][0]], k[1], k[2], k[3]))
    issues = []
    for n, k in enumerate(keys, 1):
        phase, title, msg, fix = CHECKS[k[1]]
        items = groups[k]
        files = sorted({f.file for f in items})
        if len(files) > 1 and k[1] in ("ver.rollback_logic", "dep.order", "rb.order", "inv.entity_set", "inv.content"):
            msg = "%d file(s) in %s/%s: %s" % (len(files), k[2], TYPE_LABEL.get(k[3], k[3]), msg[0].lower() + msg[1:])
        issues.append(Issue(n, k[0], k[1], phase, k[2], k[3], title, msg, fix, items))
    return issues


def phase_status(findings, phase):
    sevs = [f.severity for f in findings if CHECKS[f.check][0] == phase]
    if "CRITICAL" in sevs:
        return "CRITICAL"
    if "WARNING" in sevs:
        return "WARNING"
    return "OK"


# --------------------------------------------------------------------------------------------
# HTML rendering
# --------------------------------------------------------------------------------------------

CSS = r"""
:root{--bg:#0d1117;--panel:#161b22;--panel2:#1c2128;--grp:#11161d;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;
--blue:#58a6ff;--red:#f85149;--yellow:#d29922;--green:#3fb950;--chip:#1f2a37}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:13px/1.55 "Cascadia Mono",Consolas,"SFMono-Regular",Menlo,monospace}
header{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--border);padding:16px 28px 12px}
h1{margin:0 0 6px;font-size:20px;color:var(--blue)}
.meta{color:var(--muted);font-size:12px}.meta b{color:var(--text);font-weight:600}.meta span{margin-right:14px}
nav{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px}
nav a{color:var(--blue);text-decoration:none;border:1px solid var(--border);border-radius:4px;padding:2px 10px;font-size:12px;background:var(--panel2)}
nav a:hover{border-color:var(--blue)}
main{padding:22px 28px;max-width:1600px}
section.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;margin-bottom:22px;scroll-margin-top:175px}
.card>h2{margin:0;padding:13px 18px;font-size:15px;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.body{padding:16px 18px}
.badge{display:inline-block;padding:0 8px;border-radius:10px;font-size:11px;border:1px solid;white-space:nowrap;line-height:18px;font-weight:600}
.b-crit{color:var(--red);border-color:var(--red);background:rgba(248,81,73,.12)}
.b-warn{color:var(--yellow);border-color:var(--yellow);background:rgba(210,153,34,.12)}
.b-ok{color:var(--green);border-color:var(--green);background:rgba(63,185,80,.12)}
.b-info{color:var(--blue);border-color:var(--blue);background:rgba(88,166,255,.12)}
.b-mut{color:var(--muted);border-color:var(--border)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}
.stat{background:var(--panel2);border:1px solid var(--border);border-radius:6px;padding:16px 10px;text-align:center}
.stat .n{font-size:26px;font-weight:700}.stat .l{color:var(--muted);font-size:11px}
.c-blue{color:var(--blue)}.c-red{color:var(--red)}.c-yellow{color:var(--yellow)}.c-green{color:var(--green)}.muted{color:var(--muted)}
.legend{display:flex;gap:18px;flex-wrap:wrap;padding:12px 0;border-top:1px solid var(--border);border-bottom:1px solid var(--border);margin:16px 0;font-size:12px}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:-1px}
.label{color:var(--muted);font-size:11px;letter-spacing:.08em;text-transform:uppercase;margin:18px 0 8px}
.tbl{overflow-x:auto;border:1px solid var(--border);border-radius:6px;margin-bottom:6px}
table{border-collapse:collapse;width:100%}
th,td{padding:7px 10px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
th{color:var(--muted);font-size:11px;text-transform:uppercase;background:var(--panel2);white-space:nowrap}
th.s{cursor:pointer;user-select:none}th.s:after{content:" \2195";opacity:.45}
th.s[data-dir]:after{opacity:1;color:var(--blue)}
tr.grp td{background:var(--grp);color:var(--blue);font-weight:700}
tbody tr:hover td{background:rgba(88,166,255,.04)}
.chip{display:inline-block;background:var(--chip);border:1px solid var(--border);border-radius:4px;padding:0 6px;color:var(--blue);font-size:12px;word-break:break-all}
.wide{min-width:460px}
.path{color:var(--muted);font-size:11px;word-break:break-all}
.item{margin:2px 0}
details{border:1px solid var(--border);border-radius:6px;margin:10px 0;background:var(--panel2)}
details>summary{cursor:pointer;padding:10px 14px;font-weight:700;list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary:before{content:"\25B8  ";color:var(--muted)}details[open]>summary:before{content:"\25BE  "}
details .tbl{margin:0 12px 12px}
ul.checks{margin:6px 0 10px;padding-left:20px}ul.checks li{margin:3px 0}
.flow{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:15px;margin:6px 0 12px}
.rng{font-size:11px;color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:0 5px}
.prio{list-style:none;margin:0;padding:0}
.prio li{display:flex;gap:10px;align-items:baseline;padding:7px 10px;margin:4px 0;border-left:3px solid var(--red);background:rgba(248,81,73,.06)}
.prio li.w{border-left-color:var(--yellow);background:rgba(210,153,34,.06)}
.prio li.i{border-left-color:var(--blue);background:rgba(88,166,255,.06)}
.prio .n{color:var(--muted);min-width:28px;text-align:right}
.note{color:var(--muted);font-size:12px}
footer{color:var(--muted);font-size:11px;padding:0 28px 30px}
"""

JS = r"""
(function(){
function key(c){if(!c)return'';var v=c.getAttribute('data-v');if(v===null)v=c.textContent.trim();
 return /^-?\d+(\.\d+)?$/.test(v)?parseFloat(v):v.toLowerCase();}
document.querySelectorAll('table th.s').forEach(function(th){
 th.addEventListener('click',function(){
  var table=th.closest('table'),idx=th.cellIndex,asc=th.getAttribute('data-dir')!=='asc';
  table.querySelectorAll('th.s').forEach(function(h){h.removeAttribute('data-dir');});
  th.setAttribute('data-dir',asc?'asc':'desc');
  table.querySelectorAll('tbody').forEach(function(tb){
   var rows=[].slice.call(tb.rows).filter(function(r){return !r.classList.contains('grp');});
   rows.sort(function(a,b){var x=key(a.cells[idx]),y=key(b.cells[idx]);
    if(typeof x==='number'&&typeof y==='number')return asc?x-y:y-x;
    return asc?String(x).localeCompare(String(y)):String(y).localeCompare(String(x));});
   rows.forEach(function(r){tb.appendChild(r);});
  });
 });
});
})();
"""


def esc(s):
    return html.escape(str(s), quote=True)


def badge(text, kind=None):
    kind = kind or text
    cls = {"CRITICAL": "b-crit", "FAIL": "b-crit", "WARNING": "b-warn", "WARN": "b-warn", "INFO": "b-info",
           "OK": "b-ok", "PASS": "b-ok", "MATCH": "b-ok", "SKIP": "b-mut", "N/A": "b-mut"}.get(kind, "b-mut")
    return '<span class="badge %s">%s</span>' % (cls, esc(text))


def sev_cell(sev):
    return '<td data-v="%d">%s</td>' % (SEV_RANK.get(sev, 9), badge(sev))


def th(cols, sortable=True):
    return "<tr>" + "".join('<th class="s">%s</th>' % esc(c) if sortable else "<th>%s</th>" % esc(c) for c in cols) + "</tr>"


def chip(text):
    return '<span class="chip">%s</span>' % esc(text)


def status_of(findings_by_file, rel):
    sevs = findings_by_file.get(rel, [])
    for s in ("CRITICAL", "WARNING", "INFO"):
        if s in sevs:
            return s
    return "OK"


def issue_items_html(issue):
    out = []
    for f in issue.items:
        loc = chip(f.file) if f.file else ""
        if f.lines:
            loc += ' <span class="muted">(%s)</span>' % esc(lines_str(f.lines))
        if f.detail:
            out.append('<div class="item">– %s<br>%s</div>' % (esc(f.detail), loc))
        else:
            out.append('<div class="item">%s</div>' % loc)
    return "".join(out)


def issues_table(issues, with_phase=False, with_num=False):
    if not issues:
        return '<p>%s No issues.</p>' % badge("OK")
    groups = OrderedDict()
    for i in issues:
        groups.setdefault("%s / %s" % (i.section, TYPE_LABEL.get(i.stype, i.stype) or "—"), []).append(i)
    cols = (["#"] if with_num else []) + ["Severity", "Type"] + (["Phase"] if with_phase else []) + \
           ["Category", "Location / Files", "Issue", "Recommended fix"]
    body = []
    for g, items in groups.items():
        rows = ['<tr class="grp"><td colspan="%d">%s <span class="badge b-mut">%d issue%s</span></td></tr>'
                % (len(cols), esc(g), len(items), "" if len(items) == 1 else "s")]
        for i in items:
            rows.append("<tr>%s%s<td>%s</td>%s<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                '<td data-v="%d">%d</td>' % (i.num, i.num) if with_num else "",
                sev_cell(i.severity), esc(TYPE_LABEL.get(i.stype, i.stype) or i.section),
                "<td>%s</td>" % esc(PHASES[i.phase][0].split(" – ")[0]) if with_phase else "",
                esc(i.title), issue_items_html(i), esc(i.message), esc(i.fix)))
        body.append("<tbody>%s</tbody>" % "".join(rows))
    return '<div class="tbl"><table><thead>%s</thead>%s</table></div>' % (th(cols), "".join(body))


def script_table(entries, section, order, fbf, title):
    out = []
    for t in order:
        items = sorted([e for e in entries if e.section == section and e.stype == t], key=lambda e: e.line)
        if not items:
            continue
        worst = "OK"
        for e in items:
            s = status_of(fbf, e.rel)
            if SEV_RANK[s] < SEV_RANK[worst]:
                worst = s
        rows = []
        for n, e in enumerate(items, 1):
            deps = " ".join(chip("%s.%s" % (x.schema, x.obj)) for x, _ in e.deps) or '<span class="muted">-</span>'
            st = status_of(fbf, e.rel)
            rows.append('<tr><td data-v="%d">%d</td><td data-v="%d">L%d</td><td>%s<div class="path">%s</div></td>'
                        '<td>%s</td><td>%s</td><td>%s</td>%s</tr>'
                        % (n, n, e.line, e.line, chip(e.fname), esc(e.rel), chip("%s.%s" % (e.schema, e.obj)) if e.schema else esc(e.obj),
                           esc(e.fa.op_display if e.fa else "MISSING"), deps, sev_cell(st)))
        out.append('<details%s><summary>%s <span class="badge b-mut">%d</span> %s</summary><div class="tbl"><table>'
                   '<thead>%s</thead><tbody>%s</tbody></table></div></details>'
                   % (" open" if worst in ("CRITICAL", "WARNING") else "", esc(TYPE_LABEL[t]), len(items),
                      badge("OK" if worst in ("OK", "INFO") else "issues", "OK" if worst in ("OK", "INFO") else worst),
                      th(["#", "XML line", "File", "Object", "Operation", "In-release dependencies", "Status"]),
                      "".join(rows)))
    return "".join(out)


def render(meta, cfg, entries, disk_entries, findings, issues, entity_results, ctx, compile_note):
    fbf = defaultdict(list)
    for f in findings:
        fbf[f.file].append(f.severity)
    crit = sum(1 for i in issues if i.severity == "CRITICAL")
    warn = sum(1 for i in issues if i.severity == "WARNING")
    info = sum(1 for i in issues if i.severity == "INFO")
    deploy = [e for e in entries if e.section == "Deploy"]
    rollback = [e for e in entries if e.section == "Rollback"]
    cmp_fail_files = sorted({f.file for f in findings if CHECKS[f.check][0] == "P3A" and f.severity == "CRITICAL"})
    missing = [f for f in findings if f.check == "inv.missing"]
    extra = [f for f in findings if f.check == "inv.extra"]
    overall = "FAIL" if crit else ("WARN" if warn else "PASS")
    pstat = {p: phase_status(findings, p) for p in PHASES}
    H = []
    a = H.append

    a('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">')
    a("<title>%s DB Verification</title><style>%s</style></head><body>" % (esc(meta["release"]), CSS))
    a('<header><h1>%s ACE DB Database Scripts Verification Report</h1>' % ("&#9989;" if overall == "PASS" else "&#10060;" if overall == "FAIL" else "&#9888;&#65039;"))
    a('<div class="meta"><span><b>Project:</b> %s</span><span><b>Release:</b> %s</span><span><b>ChangeSet:</b> %s</span></div>'
      % (esc(meta["project"]), esc(meta["release"]), esc(meta["changesets"])))
    a('<div class="meta"><span><b>Changelog:</b> %s</span><span><b>Script Root:</b> %s</span></div>'
      % (esc(meta["changelog"]), esc(meta["script_root"])))
    a('<div class="meta"><span><b>Generated:</b> %s</span><span><b>Workflow:</b> %s</span><span><b>Mode:</b> %s</span></div>'
      % (esc(meta["generated"]), WORKFLOW, esc(meta["mode"])))
    nav = [("summary", "Summary"), ("inventory", "File Inventory"), ("verification", "Script Verification"),
           ("compilation", "Compilation"), ("deploy", "Deploy Dependencies"), ("rollback", "Rollback Dependencies"),
           ("xml", "XML Integrity"), ("issues", "All Issues"), ("priority", "Fix Priority"), ("matrix", "Result Matrix")]
    a("<nav>%s</nav></header><main>" % "".join('<a href="#%s">%s</a>' % x for x in nav))

    # ---- summary
    sev_badges = ""
    if crit:
        sev_badges += " " + badge("%d CRITICAL" % crit, "CRITICAL")
    if warn:
        sev_badges += " " + badge("%d WARNING%s" % (warn, "" if warn == 1 else "S"), "WARNING")
    if not crit and not warn:
        sev_badges = " " + badge("PASS")
    a('<section class="card" id="summary"><h2>&#128203; Executive Summary%s</h2><div class="body"><div class="cards">' % sev_badges)
    cards = [(len(entries), "Total SQL Files", "c-blue"), (len(deploy), "Deploy Scripts", "c-blue"),
             (len(rollback), "Rollback Scripts", "c-blue"),
             (len(cmp_fail_files), "Compilation FAIL – files", "c-red" if cmp_fail_files else "c-green"),
             (len(missing) + len(extra), "Missing / Extra Files", "c-red" if missing else ("c-yellow" if extra else "c-green")),
             (crit, "CRITICAL Issues", "c-red" if crit else "c-green"), (warn, "WARNING Issues", "c-yellow" if warn else "c-green")]
    for n, l, c in cards:
        a('<div class="stat"><div class="n %s">%d</div><div class="l">%s</div></div>' % (c, n, esc(l)))
    a("</div>")
    a('<div class="legend"><span><span class="dot" style="background:var(--red)"></span><b>CRITICAL</b> deployment or rollback will fail or produce incorrect results</span>'
      '<span><span class="dot" style="background:var(--yellow)"></span><b>WARNING</b> may cause issues in certain environments</span>'
      '<span><span class="dot" style="background:var(--blue)"></span><b>INFO</b> informational</span>'
      '<span><span class="dot" style="background:var(--green)"></span><b>OK</b> verified correct</span></div>')
    a('<div class="label">Overall result</div><p>%s — %s</p>' % (
        badge(overall), esc("%d CRITICAL and %d WARNING issues were found%s." % (crit, warn, (", %d INFO" % info) if info else ""))))
    a("<p><b>Workflow timing:</b> Started %s, ended %s (duration %s).</p>" % (esc(meta["started"]), esc(meta["ended"]), esc(meta["duration"])))
    if meta.get("base_note"):
        a('<p class="note">%s</p>' % esc(meta["base_note"]))
    if ctx.suppressed:
        a('<p class="note">%d finding(s) suppressed by config.</p>' % ctx.suppressed)
    a("</div></section>")

    # ---- phase 2
    a('<section class="card" id="inventory"><h2>&#128196; %s %s</h2><div class="body">' % (esc(PHASES["P2"][0]), badge(pstat["P2"])))
    a('<div class="label">File counts (XML vs disk)</div>')
    types = ["Table", "Tvf", "Procedure", "Data"]
    if any(e.stype == "Other" for e in entries + disk_entries):
        types.append("Other")
    rows = []
    for label, items in (("Deploy XML", [e for e in entries if e.section == "Deploy"]),
                         ("Deploy Disk", [d for d in disk_entries if d.folder_section == "Deploy"]),
                         ("Rollback XML", [e for e in entries if e.section == "Rollback"]),
                         ("Rollback Disk", [d for d in disk_entries if d.folder_section == "Rollback"])):
        cnt = [sum(1 for e in items if e.stype == t) for t in types]
        rows.append("<tr><td>%s</td>%s<td>%d</td></tr>" % (label, "".join("<td>%d</td>" % c for c in cnt), sum(cnt)))
    a('<div class="tbl"><table><thead>%s</thead><tbody>%s</tbody></table></div>'
      % (th(["Section"] + [TYPE_LABEL[t] for t in types] + ["Total"], False), "".join(rows)))
    if missing or extra:
        a('<div class="label">Missing / extra files</div>')
        a(issues_table([i for i in issues if i.check in ("inv.missing", "inv.extra")]))
    a('<div class="label">Entity set completeness</div><ul class="checks">'
      "<li>Grouped scripts by base name (ignoring schema prefix), per Deploy/Rollback folder and type.</li>"
      "<li>For each group with at least one of [%s], verified all expected schema variants exist on disk.</li>"
      "<li>Normalized content (schema prefixes replaced; SQL comments, spaces, whitespace and line endings ignored) and compared SHA-256 hashes across variants.</li></ul>"
      % esc(", ".join(cfg["expected_schemas"])))
    inc = [r for r in entity_results if r["missing"]]
    dif = [r for r in entity_results if r["content"] == "DIFFERENT"]
    a("<p>%s %s %s</p>" % (badge("%d entity set(s) checked" % len(entity_results), "INFO"),
                           badge("%d incomplete set(s)" % len(inc), "CRITICAL" if inc else "OK"),
                           badge("%d different content set(s)" % len(dif), "WARNING" if dif else "OK")))
    if entity_results:
        rows = []
        for r in sorted(entity_results, key=lambda r: (not r["missing"] and r["content"] != "DIFFERENT", r["section"], r["stype"], r["base"].lower())):
            st = "CRITICAL" if r["missing"] else ("WARNING" if r["content"] == "DIFFERENT" else "OK")
            detail = ("Missing schema(s): " + ", ".join(r["missing"])) if r["missing"] else (
                "Differs: " + r["diff"] if r["diff"] else "Complete, content matches")
            rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>%s</tr>" % (
                chip(r["base"]), esc(r["section"]), esc(TYPE_LABEL[r["stype"]]), esc(", ".join(r["expected"])),
                esc(", ".join(r["present"])), badge(", ".join(r["missing"]), "CRITICAL") if r["missing"] else '<span class="muted">-</span>',
                badge(r["content"], "N/A" if r["content"].startswith("N/A") else ("WARNING" if r["content"] == "DIFFERENT" else "OK")),
                esc(detail), sev_cell(st)))
        a('<details%s><summary>Entity set details <span class="badge b-mut">%d</span></summary><div class="tbl"><table><thead>%s</thead><tbody>%s</tbody></table></div></details>'
          % (" open" if inc or dif else "", len(entity_results),
             th(["Entity", "Folder", "Type", "Expected schemas", "Present", "Missing", "Content check", "Details", "Status"]), "".join(rows)))
    a("</div></section>")

    # ---- phase 3
    p3 = [i for i in issues if i.phase == "P3"]
    pairs_d = sum(1 for e in entries if e.section == "Deploy")
    pairing_issues = [i for i in p3 if i.check == "ver.pairing"]
    a('<section class="card" id="verification"><h2>&#128295; %s %s</h2><div class="body">' % (esc(PHASES["P3"][0]), badge(pstat["P3"])))
    a('<div class="label">Script pairing</div><ul class="checks"><li>Deploy/Rollback scripts paired by their path below the Deploy/ and Rollback/ folders (%d deploy scripts).</li><li>%s</li></ul>'
      % (pairs_d, "All deploy/rollback script pairs present." if not pairing_issues else "%d pairing issue(s), see below." % len(pairing_issues)))
    a('<div class="label">Rollback logic rules</div><ul class="checks">'
      "<li>TVF / Procedure: CREATE → DROP; CREATE OR ALTER → %s (or ALTER if marked pre-existing); ALTER → ALTER / CREATE OR ALTER (restore previous definition).</li>"
      "<li>Table: CREATE TABLE → DROP TABLE; ALTER TABLE ADD col → ALTER TABLE DROP COLUMN col.</li>"
      "<li>Data: INSERT / MERGE → DELETE / UPDATE / MERGE / TRUNCATE.</li>"
      "<li>Schema references: a file in schema S must not reference another of [%s].</li></ul>"
      % (esc(" / ".join(cfg["create_or_alter_rollback"])), esc(", ".join(cfg["expected_schemas"]))))
    a('<div class="label">Issues</div>')
    a(issues_table(p3))
    a("</div></section>")

    # ---- phase 3A
    a('<section class="card" id="compilation"><h2>&#128736;&#65039; %s %s</h2><div class="body">'
      % (esc(PHASES["P3A"][0]), badge("%d FILES FAILED" % len(cmp_fail_files), "CRITICAL") if cmp_fail_files else badge(pstat["P3A"])))
    a('<div class="label">Per-script compilation screen</div>')
    a("<p>Static simulation splits each file into batches exactly like <code>sqlcmd -b -f 65001</code> (GO on its own line) and validates "
      "batch structure without modifying or executing anything. %s</p>"
      % ("Each batch was also compiled with sqlcmd under SET NOEXEC ON." if meta["compiled"] else "Run with --compile to also compile every batch with sqlcmd under SET NOEXEC ON."))
    if compile_note:
        a('<p class="note">sqlcmd: %s</p>' % esc(compile_note))
    a("<p>%s %d failed file(s) of %d</p>" % (badge("FAIL" if cmp_fail_files else "PASS"), len(cmp_fail_files), len(entries)))
    colmap = OrderedDict([("Encoding", ("cmp.encoding", "cmp.hidden_chars")),
                          ("Batch / GO", ("cmp.batch_first", "cmp.go_format", "cmp.sqlcmd_var", "cmp.use_db")),
                          ("Duplicate DECLARE", ("cmp.dup_declare",)),
                          ("Duplicate CREATE TABLE / #temp", ("cmp.dup_create",)),
                          ("BEGIN/END pairing", ("cmp.begin_end",)),
                          ("Simulated execution", ("cmp.parens", "cmp.unterminated", "cmp.quoted_ident", "cmp.alter_drop"))])
    if meta["compiled"]:
        colmap["sqlcmd NOEXEC"] = ("cmp.sqlcmd",)
    per = defaultdict(lambda: defaultdict(list))
    for f in findings:
        if CHECKS[f.check][0] == "P3A":
            for col, checks in colmap.items():
                if f.check in checks:
                    per[f.file][col].append(f.severity)
    rows = []
    for e in entries:
        if not e.exists:
            continue
        cells = []
        worst = "PASS"
        for col in colmap:
            sevs = per[e.rel][col]
            v = "FAIL" if "CRITICAL" in sevs else ("WARN" if "WARNING" in sevs else "PASS")
            if col == "sqlcmd NOEXEC" and e.compile_status == "SKIP":
                v = "SKIP"
            if v == "FAIL":
                worst = "FAIL"
            elif v == "WARN" and worst == "PASS":
                worst = "WARN"
            cells.append('<td data-v="%s">%s</td>' % ({"FAIL": 0, "WARN": 1, "SKIP": 2, "PASS": 3}[v], badge(v)))
        rows.append((0 if worst == "FAIL" else 1 if worst == "WARN" else 2, e.rel,
                     '<tr><td class="wide">%s</td><td data-v="%d">%s</td>%s</tr>' % (chip(e.rel), {"FAIL": 0, "WARN": 1, "PASS": 3}[worst], badge(worst), "".join(cells))))
    rows.sort(key=lambda r: (r[0], r[1].lower()))
    a('<details%s><summary>All scripts <span class="badge b-mut">%d</span></summary><div class="tbl"><table><thead>%s</thead><tbody>%s</tbody></table></div></details>'
      % (" open" if cmp_fail_files else "", len(rows), th(["File", "Script status"] + list(colmap)), "".join(r[2] for r in rows)))
    a('<div class="label">Compilation findings</div>')
    cf = sorted([f for f in findings if CHECKS[f.check][0] == "P3A"], key=lambda f: (SEV_RANK[f.severity], f.file, f.lines[:1]))
    if cf:
        rows = "".join("<tr>%s<td>%s %s</td><td>%s</td><td>%s</td></tr>" % (
            sev_cell(f.severity), chip(f.file), '<span class="muted">(%s)</span>' % esc(lines_str(f.lines)) if f.lines else "",
            esc(CHECKS[f.check][1]), esc(f.detail)) for f in cf)
        a('<div class="tbl"><table><thead>%s</thead><tbody>%s</tbody></table></div>' % (th(["Severity", "File / Lines", "Check", "Finding"]), rows))
    else:
        a("<p>%s No compilation findings.</p>" % badge("OK"))
    a("</div></section>")

    # ---- phase 4 & 5 / 4.1 & 6
    for pid, section, order_key, anchor, icon in (("P45", "Deploy", "deploy_category_order", "deploy", "&#128640;"),
                                                   ("P46", "Rollback", "rollback_category_order", "rollback", "&#128260;")):
        order = cfg[order_key]
        ph_issues = [i for i in issues if i.phase == pid]
        a('<section class="card" id="%s"><h2>%s %s %s</h2><div class="body">' % (anchor, icon, esc(PHASES[pid][0]), badge(pstat[pid])))
        a('<div class="label">%s category order</div><div class="flow">' % section)
        rng = category_ranges(entries, section, order)
        a(" &rarr; ".join('%s <span class="rng">L%d–%d</span>' % (esc(TYPE_FOLDER_LABEL[t]), lo, hi) for t, lo, hi, _ in rng))
        order_issues = [f for f in findings if f.check == "xml.order" and f.section == section]
        a(" — %s</div>" % ("correct order." if not order_issues else '<span class="c-yellow">%d sqlFile(s) out of order (see Phase 7).</span>' % len(order_issues)))
        a('<div class="label">Key dependencies validated</div>')
        a(issues_table(ph_issues) if ph_issues else "<p>%s No %s dependency issues found.</p>" % (badge("OK"), section.lower()))
        logic = [i for i in issues if i.check == "ver.rollback_logic"]
        if logic:
            a("<p>%s</p>" % badge("%d rollback logic issue(s) affect deploy/rollback pairs; see Phase 3." % len(logic),
                                  "CRITICAL" if any(i.severity == "CRITICAL" for i in logic) else "WARNING"))
        a('<div class="label">All %s scripts (execution order) – %d file(s)</div>' % (section.lower(), sum(1 for e in entries if e.section == section)))
        a(script_table(entries, section, order, fbf, section))
        a("</div></section>")

    # ---- phase 7
    p7 = [i for i in issues if i.phase == "P7"]
    a('<section class="card" id="xml"><h2>&#128220; %s %s</h2><div class="body"><ul class="checks">' % (esc(PHASES["P7"][0]), badge(pstat["P7"])))
    a("<li>Well-formed XML with root &lt;%s&gt;.</li>" % esc(meta["root_tag"]))
    a("<li>%d changeSet element(s): %s. Duplicate IDs: %d.</li>" % (meta["n_changesets"], " ".join(chip(c) for c in meta["changeset_ids"]),
                                                                    sum(1 for f in findings if f.check == "xml.duplicate" and "changeSet" in f.detail)))
    a("<li>%d sqlFile references (%d deploy = direct children of &lt;changeSet&gt;, %d rollback = inside &lt;rollback&gt;, %d not run). Duplicate paths: %d.</li>"
      % (len(entries), len(deploy), len(rollback), sum(1 for e in entries if e.section == "NotRun"),
         sum(1 for f in findings if f.check == "xml.duplicate" and "sqlFile" in f.detail)))
    a("<li>Paths resolved relative to the changelog folder, exactly as the run_*.bat files do.</li>")
    a("<li>sqlFile attributes validated (dbms, encoding); Deploy/Rollback folder matches XML placement.</li></ul>")
    a(issues_table(p7))
    a("</div></section>")

    # ---- all issues
    a('<section class="card" id="issues"><h2>&#9888;&#65039; All Issues %s</h2><div class="body">'
      % badge("%d Critical, %d Warnings%s" % (crit, warn, (", %d Info" % info) if info else ""), "CRITICAL" if crit else "WARNING" if warn else "OK"))
    a(issues_table(issues, with_phase=True, with_num=True))
    a("</div></section>")

    # ---- fix priority
    a('<section class="card" id="priority"><h2>&#128204; Fix Priority %s</h2><div class="body">'
      % badge("%d P1, %d P2, %d P3/P4" % (crit, warn, info), "CRITICAL" if crit else "WARNING" if warn else "OK"))
    a('<p><span class="badge b-crit">P1 Critical / will fail</span> <span class="badge b-warn">P2 Warning / may cause issues</span> <span class="badge b-info">P3/P4 Optional / informational</span></p>')
    for label, sev, cls in (("P1 – will fail deployment/rollback", "CRITICAL", ""), ("P2 – may cause issues", "WARNING", "w"),
                            ("P3/P4 – optional / informational", "INFO", "i")):
        items = [i for i in issues if i.severity == sev]
        a('<div class="label">%s</div>' % label)
        if not items:
            a('<p class="note">None.</p>')
            continue
        a('<ol class="prio">%s</ol>' % "".join(
            '<li class="%s"><span class="n">%d.</span>%s <span><b>%s:</b> %s <span class="muted">[%s / %s, %d finding%s]</span></span></li>'
            % (cls, i.num, badge(sev), esc(i.title), esc(i.message), esc(i.section), esc(TYPE_LABEL.get(i.stype, i.stype) or "-"),
               len(i.items), "" if len(i.items) == 1 else "s") for i in items))
    a("</div></section>")

    # ---- matrix
    fails = sum(1 for p in PHASES if pstat[p] == "CRITICAL")
    a('<section class="card" id="matrix"><h2>&#128202; Result Matrix %s</h2><div class="body">' % (
        badge("%d FAIL phase%s" % (fails, "" if fails == 1 else "s"), "CRITICAL") if fails else badge("PASS")))
    rows = "".join("<tr><td>%s</td><td>%s</td>%s</tr>" % (esc(PHASES[p][0].replace(" – ", " - ")), esc(PHASES[p][1]), sev_cell(pstat[p])) for p in PHASES)
    a('<div class="tbl"><table><thead>%s</thead><tbody>%s</tbody></table></div>' % (th(["Phase", "Check", "Status"], False), rows))
    a("</div></section></main>")
    a("<footer>ace_db_verify.py v%s · config: %s · expected schemas: %s</footer>" % (VERSION, esc(meta["config"]), esc(", ".join(cfg["expected_schemas"]))))
    a("<script>%s</script></body></html>" % JS)
    return "".join(H)


# --------------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------------


def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        with open(path, "r", encoding="utf-8-sig") as f:
            user = json.load(f)
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if k == "sqlcmd" and isinstance(v, dict):
                cfg["sqlcmd"].update({kk: vv for kk, vv in v.items() if not kk.startswith("_")})
            else:
                cfg[k] = v
    return cfg


def main(argv=None):
    ap = argparse.ArgumentParser(description="ACE DB deploy/rollback SQL scripts verification (static).")
    ap.add_argument("--changelog", default="changelogs/changelog_ACE_GWP_ID.xml",
                    help="Changelog XML (default: changelogs/changelog_ACE_GWP_ID.xml, same as the bats)")
    ap.add_argument("--config", help="JSON config file (default: ace_db_verify.config.json next to this script, if present)")
    ap.add_argument("--base-dir", help="Resolve sqlFile paths against this folder instead of the changelog folder")
    ap.add_argument("--out", help="HTML report path (default: reports/<release>_db_verification_<timestamp>.html)")
    ap.add_argument("--json", help="JSON result path (default: same as --out with .json)")
    ap.add_argument("--project", help="Project name shown in the header")
    ap.add_argument("--compile", action="store_true", help="Also compile every batch with sqlcmd under SET NOEXEC ON (no changes to the DB)")
    ap.add_argument("--server", help="Override sqlcmd.server")
    ap.add_argument("--database", help="Override sqlcmd.database")
    ap.add_argument("--fail-on", choices=["critical", "warning", "never"], default="critical")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    def log(msg):
        if not args.quiet:
            print(msg)

    started = dt.datetime.now()
    t0 = time.time()
    cfg_path = args.config
    if not cfg_path:
        cand = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ace_db_verify.config.json")
        if os.path.isfile(cand):
            cfg_path = cand
    cfg = load_config(cfg_path)
    if args.server:
        cfg["sqlcmd"]["server"] = args.server
    if args.database:
        cfg["sqlcmd"]["database"] = args.database
    ctx = Ctx(cfg)

    changelog_abs = os.path.abspath(args.changelog)
    if not os.path.isfile(changelog_abs):
        print("Changelog not found: %s" % changelog_abs, file=sys.stderr)
        return 2
    changelog_dir = os.path.dirname(changelog_abs)
    log("Reading %s" % changelog_abs)
    cl = parse_changelog(changelog_abs)
    if not cl.ok:
        ctx.add("xml.malformed", "CRITICAL", "XML", "", os.path.basename(changelog_abs), [cl.error[0]],
                "Line %d: %s" % cl.error)

    base = os.path.abspath(args.base_dir) if args.base_dir else changelog_dir
    base_note = ""
    if not args.base_dir and cl.sqlfiles:
        found = sum(1 for s in cl.sqlfiles if os.path.isfile(os.path.join(base, s["path"])))
        if found == 0:
            for c in (os.getcwd(), os.path.dirname(changelog_dir), os.path.dirname(os.path.dirname(changelog_dir))):
                if sum(1 for s in cl.sqlfiles if os.path.isfile(os.path.join(c, s["path"]))) > 0:
                    base_note = ("No sqlFile path resolves relative to the changelog folder (which is what the bats do); "
                                 "this report resolved them against %s instead." % c)
                    ctx.add("xml.structure", "CRITICAL", "XML", "", os.path.basename(changelog_abs), [],
                            "No sqlFile path exists relative to the changelog folder %s; the bats would report every file as MISSING." % changelog_dir)
                    base = c
                    break

    entries: List[Entry] = []
    for s in cl.sqlfiles:
        absp = os.path.normpath(os.path.join(base, s["path"]))
        rel = norm_rel(absp, base)
        fsec, stype, schema, obj, fname, bname, tail = classify(rel)
        e = Entry(s["path"], rel, absp, s["line"], s["placement"], fsec, stype, schema, obj, fname, bname, tail,
                  s["attrs"], s["changeset"], os.path.isfile(absp))
        entries.append(e)

    rel_dirs = [os.path.dirname(e.rel) for e in entries if e.rel]
    script_root_rel = ""
    if rel_dirs:
        try:
            script_root_rel = os.path.commonpath(rel_dirs).replace(os.sep, "/")
        except ValueError:
            script_root_rel = ""
    parts = script_root_rel.split("/")
    low = [p.lower() for p in parts]
    for sec in ("deploy", "rollback"):
        if sec in low:
            parts = parts[:low.index(sec)]
            low = low[:len(parts)]
    script_root_rel = "/".join(parts)
    script_root_abs = os.path.normpath(os.path.join(base, script_root_rel))
    release = cfg["release"]
    if not release:
        if "releases" in low and low.index("releases") + 1 < len(parts):
            release = parts[low.index("releases") + 1]
        else:
            release = os.path.basename(script_root_abs) or "release"
    project = args.project or cfg["project"]
    if not project:
        d = changelog_dir
        for _ in range(4):
            if os.path.isdir(os.path.join(d, ".git")):
                break
            d = os.path.dirname(d)
        project = os.path.basename(d) if os.path.isdir(os.path.join(d, ".git")) else os.path.basename(os.path.dirname(changelog_dir)) or "project"

    # disk scan
    disk_entries: List[Entry] = []
    by_rel = {e.rel.lower(): e for e in entries}
    if os.path.isdir(script_root_abs) and script_root_rel not in ("", "."):
        for root, _, files in os.walk(script_root_abs):
            for fn in files:
                if not fn.lower().endswith(".sql"):
                    continue
                absp = os.path.join(root, fn)
                rel = norm_rel(absp, base)
                x = by_rel.get(rel.lower())
                if x is not None:
                    disk_entries.append(x)
                    continue
                fsec, stype, schema, obj, fname, bname, tail = classify(rel)
                disk_entries.append(Entry("", rel, absp, 0, fsec, fsec, stype, schema, obj, fname, bname, tail, {}, "", True))

    log("Analyzing %d changelog entries (%d deploy, %d rollback), %d files on disk"
        % (len(entries), sum(1 for e in entries if e.section == "Deploy"), sum(1 for e in entries if e.section == "Rollback"), len(disk_entries)))
    analyzed = {}
    for e in entries:
        if not e.exists:
            continue
        if e.rel.lower() in analyzed:
            e.fa = analyzed[e.rel.lower()]
            continue
        e.fa = analyze_file(e, cfg, ctx, checks=True)
        analyzed[e.rel.lower()] = e.fa
    for d in disk_entries:
        if d.fa is None and d.exists:
            d.fa = analyzed.get(d.rel.lower()) or analyze_file(d, cfg, ctx, checks=False)

    phase7_xml(ctx, cl, entries, changelog_dir, cfg)
    entity_results = phase2_inventory(ctx, cfg, entries, disk_entries)
    phase3_verification(ctx, cfg, entries)
    phase45_dependencies(ctx, entries)

    compile_note = ""
    if args.compile:
        log("Compiling with sqlcmd (SET NOEXEC ON) against %s / %s ..." % (cfg["sqlcmd"]["server"], cfg["sqlcmd"]["database"]))
        release_objects = {e.obj.lower() for e in entries if e.section == "Deploy"}
        compile_note = sqlcmd_compile(ctx, cfg, [e for e in entries if e.section in ("Deploy", "Rollback")], release_objects, log)
        if compile_note:
            log("  " + compile_note)

    apply_rules(ctx)
    issues = aggregate(ctx.findings)
    ended = dt.datetime.now()
    dur = time.time() - t0
    stamp = started.strftime("%Y%m%d_%H%M%S")
    out = args.out or os.path.join("reports", "%s_db_verification_%s.html" % (release, stamp))
    json_out = args.json or os.path.splitext(out)[0] + ".json"
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(json_out)), exist_ok=True)

    def disp(p):
        try:
            r = os.path.relpath(p, os.getcwd())
        except ValueError:
            return p
        return p if r.startswith("..") else "./" + r.replace(os.sep, "/")

    meta = dict(project=project, release=release, changesets=", ".join(c[0] or "?" for c in cl.changesets) or "-",
                changeset_ids=[c[0] or "?" for c in cl.changesets], n_changesets=len(cl.changesets),
                changelog=disp(changelog_abs), script_root=disp(script_root_abs) + "/",
                generated=ended.strftime("%Y-%m-%d %H:%M:%S"), started=started.strftime("%Y-%m-%d %H:%M:%S"),
                ended=ended.strftime("%Y-%m-%d %H:%M:%S"), duration="%.1f seconds" % dur,
                mode="Static + sqlcmd NOEXEC compile" if args.compile and not compile_note else "Static (no database)",
                compiled=bool(args.compile and not compile_note), root_tag=cl.root_tag or "?",
                config=disp(os.path.abspath(cfg_path)) if cfg_path else "built-in defaults", base_note=base_note)
    page = render(meta, cfg, entries, disk_entries, ctx.findings, issues, entity_results, ctx, compile_note)
    with open(out, "w", encoding="utf-8") as f:
        f.write(page)

    crit = sum(1 for i in issues if i.severity == "CRITICAL")
    warn = sum(1 for i in issues if i.severity == "WARNING")
    result = dict(
        meta={k: v for k, v in meta.items()},
        summary=dict(total=len(entries), deploy=sum(1 for e in entries if e.section == "Deploy"),
                     rollback=sum(1 for e in entries if e.section == "Rollback"), critical=crit, warning=warn,
                     info=sum(1 for i in issues if i.severity == "INFO"), suppressed=ctx.suppressed,
                     overall="FAIL" if crit else ("WARN" if warn else "PASS")),
        phases={PHASES[p][0]: phase_status(ctx.findings, p) for p in PHASES},
        issues=[dict(num=i.num, severity=i.severity, check=i.check, phase=PHASES[i.phase][0], section=i.section,
                     type=i.stype, title=i.title, message=i.message, fix=i.fix,
                     findings=[dict(file=f.file, lines=f.lines, detail=f.detail) for f in i.items]) for i in issues])
    with open(json_out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    log("")
    log("Result: %s - %d CRITICAL, %d WARNING issue(s)%s" % (result["summary"]["overall"], crit, warn,
                                                             (", %d suppressed" % ctx.suppressed) if ctx.suppressed else ""))
    for i in issues:
        if i.severity in ("CRITICAL", "WARNING"):
            log("  [%s] %-34s %s/%s (%d)" % (i.severity[:4], i.title, i.section, TYPE_LABEL.get(i.stype, i.stype), len(i.items)))
    log("Report: %s" % os.path.abspath(out))
    log("JSON:   %s" % os.path.abspath(json_out))
    if args.fail_on == "critical" and crit:
        return 1
    if args.fail_on == "warning" and (crit or warn):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
