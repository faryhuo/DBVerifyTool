import json
import os
import subprocess
import sys

import pytest

from ace_db_verify.cli import main

pytestmark = pytest.mark.integration

SRC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src")


def _run(tmp_path, changelog, *extra):
    out = tmp_path / "report.html"
    rc = main(["--changelog", changelog, "--out", str(out), "--project", "P", "--quiet", *extra])
    return rc, out, json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))


def test_sample_project_report(tmp_path, sample_changelog, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no config in cwd -> built-in defaults
    rc, out, result = _run(tmp_path, sample_changelog)

    assert rc == 1
    assert "<html" in out.read_text(encoding="utf-8")
    summary = result["summary"]
    assert (summary["total"], summary["deploy"], summary["rollback"]) == (5, 3, 2)
    assert summary["overall"] == "FAIL"
    assert result["meta"]["config"] == "built-in defaults"
    checks = {i["check"] for i in result["issues"]}
    assert {"inv.entity_set", "ver.pairing", "cmp.dup_declare", "cmp.quoted_ident", "inv.content", "inv.extra"} <= checks


def test_fail_on_never_returns_zero(tmp_path, sample_changelog, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc, _, _ = _run(tmp_path, sample_changelog, "--fail-on", "never")
    assert rc == 0


def test_severity_override_and_suppress(tmp_path, sample_changelog, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({
        "severity_overrides": {"inv.entity_set": "WARNING"},
        "suppress": [{"check": "cmp.quoted_ident"}],
    }), encoding="utf-8")
    _, _, result = _run(tmp_path, sample_changelog, "--config", str(cfg))
    by_check = {i["check"]: i["severity"] for i in result["issues"]}
    assert by_check["inv.entity_set"] == "WARNING"
    assert "cmp.quoted_ident" not in by_check
    assert result["summary"]["suppressed"] >= 1


def test_missing_changelog_is_tool_error(tmp_path):
    assert main(["--changelog", str(tmp_path / "nope.xml"), "--quiet"]) == 2


def test_root_launcher_shim_runs_without_install(tmp_path, sample_changelog):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    shim = os.path.join(os.path.dirname(SRC_DIR), "ace_db_verify.py")
    proc = subprocess.run(
        [sys.executable, "-I", shim, "--changelog", sample_changelog,
         "--out", str(tmp_path / "r.html"), "--fail-on", "never", "--quiet"],
        cwd=str(tmp_path), env=env,
    )
    assert proc.returncode == 0
    assert (tmp_path / "r.json").is_file()


def test_module_entry_point(tmp_path, sample_changelog):
    env = dict(os.environ, PYTHONPATH=SRC_DIR)
    proc = subprocess.run(
        [sys.executable, "-m", "ace_db_verify", "--changelog", sample_changelog,
         "--out", str(tmp_path / "r.html"), "--fail-on", "never", "--quiet"],
        cwd=str(tmp_path), env=env,
    )
    assert proc.returncode == 0
