"""Allow ``python -m ace_db_verify``."""
import sys

from .cli import run

if __name__ == "__main__":
    sys.exit(run())
