"""Configuration defaults and JSON config loading."""
from __future__ import annotations

import json
import os
from typing import Optional

CONFIG_FILENAME = "ace_db_verify.config.json"

DEFAULT_CONFIG = {
    "project": None,
    "release": None,
    # Entity set rule: every object in one of these schemas must exist in all of them.
    "expected_schemas": ["cmy", "csg", "dbo"],
    "entity_set_types": ["Table", "Tvf", "Procedure", "Data"],
    "entity_set_exclude": [],             # regexes on the file name without schema
    # Cross-schema references that are allowed, e.g. {"from": "staging", "to": "dbo", "file_regex": "Migration"}
    "cross_schema_allow": [],
    # Objects that already exist in PROD: a CREATE OR ALTER deploy may be rolled back with ALTER.
    # Same effect as a "-- verify:preexisting" comment in the deploy script.
    "preexisting_objects": [],
    "create_or_alter_rollback": ["DROP"],
    "deploy_category_order": ["Table", "Tvf", "Procedure", "Data"],
    "rollback_category_order": ["Data", "Procedure", "Tvf", "Table"],
    "allowed_dbms": ["mssql"],
    "expected_encoding": "UTF-8",
    "severity_overrides": {},             # {"inv.entity_set": "WARNING"}
    "suppress": [],                       # [{"check": "ver.schema_mismatch", "file_regex": "...", "detail_regex": "..."}]
    "sqlcmd": {
        "exe": "sqlcmd",
        "server": None,
        "database": None,
        "trusted": True,
        "user": None,
        "password_env": "ACE_DB_PASSWORD",
        "login_timeout": 15,
        "file_timeout": 300,
    },
}




def load_config(path):
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path:
        with open(path, "r", encoding="utf-8-sig") as f:
            user = json.load(f)
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if k == "sqlcmd" and isinstance(v, dict):
                cfg["sqlcmd"].update({kk: vv for kk, vv in v.items() if not kk.startswith("_")})
            else:
                cfg[k] = v
    return cfg


def find_default_config(search_dir: Optional[str] = None) -> Optional[str]:
    """Return ace_db_verify.config.json in *search_dir* (default: current directory) if it exists."""
    cand = os.path.join(os.path.abspath(search_dir or os.getcwd()), CONFIG_FILENAME)
    return cand if os.path.isfile(cand) else None
