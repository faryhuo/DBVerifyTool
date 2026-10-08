"""Phase 7 - changelog XML integrity."""
from __future__ import annotations

import os

from ..constants import TYPE_FOLDER_LABEL, TYPE_LABEL


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
