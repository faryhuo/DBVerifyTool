"""Render the self-contained HTML report."""
from __future__ import annotations

from collections import OrderedDict, defaultdict

from ..classification import lines_str
from ..constants import CHECKS, PHASES, SEV_RANK, TYPE_FOLDER_LABEL, TYPE_LABEL, VERSION, WORKFLOW
from ..issues import phase_status
from ..phases.inventory import category_ranges
from .assets import CSS, JS
from .html_helpers import badge, chip, esc, issues_table, script_table, sev_cell, th


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
    for n, label, c in cards:
        a('<div class="stat"><div class="n %s">%d</div><div class="l">%s</div></div>' % (c, n, esc(label)))
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
