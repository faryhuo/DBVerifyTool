import json

import pytest

from ace_db_verify.config import CONFIG_FILENAME, DEFAULT_CONFIG, find_default_config, load_config

pytestmark = pytest.mark.unit


def test_load_config_defaults_are_a_copy():
    cfg = load_config(None)
    cfg["sqlcmd"]["server"] = "changed"
    assert DEFAULT_CONFIG["sqlcmd"]["server"] is None


def test_load_config_merges_and_skips_comment_keys(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"_note": "x", "release": "R9", "sqlcmd": {"server": "s", "_c": 1}}), encoding="utf-8")
    cfg = load_config(str(p))
    assert cfg["release"] == "R9"
    assert "_note" not in cfg
    assert cfg["sqlcmd"]["server"] == "s"
    assert cfg["sqlcmd"]["exe"] == "sqlcmd"
    assert "_c" not in cfg["sqlcmd"]


def test_find_default_config(tmp_path):
    assert find_default_config(str(tmp_path)) is None
    (tmp_path / CONFIG_FILENAME).write_text("{}", encoding="utf-8")
    assert find_default_config(str(tmp_path)) == str(tmp_path / CONFIG_FILENAME)
