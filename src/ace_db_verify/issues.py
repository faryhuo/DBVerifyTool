"""Rule application (severity overrides / suppression) and issue aggregation."""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import List

from .constants import CHECKS, PHASES, SEV_RANK, TYPE_LABEL
from .models import Finding


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
