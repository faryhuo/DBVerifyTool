"""Optional: compile batches with sqlcmd under SET NOEXEC ON."""
from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import tempfile


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
