"""Command-line entry point."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from typing import List

from .analysis.file_analyzer import analyze_file
from .changelog import parse_changelog
from .classification import classify, norm_rel
from .compiler import sqlcmd_compile
from .config import CONFIG_FILENAME, find_default_config, load_config
from .constants import PHASES, TYPE_LABEL
from .issues import aggregate, apply_rules, phase_status
from .models import Ctx, Entry
from .phases.dependencies import phase45_dependencies
from .phases.inventory import phase2_inventory
from .phases.verification import phase3_verification
from .phases.xml_integrity import phase7_xml
from .report.renderer import render


def main(argv=None):
    ap = argparse.ArgumentParser(description="ACE DB deploy/rollback SQL scripts verification (static).")
    ap.add_argument("--changelog", default="changelogs/changelog_ACE_GWP_ID.xml",
                    help="Changelog XML (default: changelogs/changelog_ACE_GWP_ID.xml, same as the bats)")
    ap.add_argument("--config", help="JSON config file (default: %s in the current directory, if present)" % CONFIG_FILENAME)
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
    cfg_path = args.config or find_default_config()
    cfg = load_config(cfg_path)
    log("Config: %s" % (os.path.abspath(cfg_path) if cfg_path else "built-in defaults"))
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


def run():
    """Console-script entry point: Ctrl+C maps to the tool-error exit code."""
    try:
        return main()
    except KeyboardInterrupt:
        return 2
