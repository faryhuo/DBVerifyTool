"""Small HTML building helpers for the report."""
from __future__ import annotations

import html
from collections import OrderedDict

from ..classification import lines_str
from ..constants import PHASES, SEV_RANK, TYPE_LABEL


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
