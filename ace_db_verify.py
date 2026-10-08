#!/usr/bin/env python3
"""Backward-compatible launcher: ``python ace_db_verify.py [args]``.

Runs the ``ace_db_verify`` package from ./src without requiring ``pip install``.
Prefer ``ace-db-verify`` (after ``pip install .``) or ``python -m ace_db_verify``.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from ace_db_verify.cli import run  # noqa: E402

if __name__ == "__main__":
    sys.exit(run())
