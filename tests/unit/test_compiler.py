import sys

import pytest

from ace_db_verify.compiler import sqlcmd_compile
from ace_db_verify.config import load_config
from ace_db_verify.models import Ctx

pytestmark = pytest.mark.unit


def _compile(cfg):
    return sqlcmd_compile(Ctx(cfg), cfg, [], set(), lambda _msg: None)


def test_skips_when_sqlcmd_missing():
    cfg = load_config(None)
    cfg["sqlcmd"]["exe"] = "definitely-not-a-real-sqlcmd-binary"
    assert "not found" in _compile(cfg)


def test_skips_when_server_not_configured():
    cfg = load_config(None)
    cfg["sqlcmd"]["exe"] = sys.executable  # any existing executable passes the lookup
    assert "not configured" in _compile(cfg)
