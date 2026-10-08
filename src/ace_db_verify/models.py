"""Data model shared by the analysis phases."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .lexer import Tok


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
                                     sorted(set(ln for ln in (lines or []) if ln)), detail))
