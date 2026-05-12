# Copyright (c) 2026 Nutanix Inc. All rights reserved. Author: era-dev@nutanix.com
"""Entry point for ``python -m pg_upgrade_test``."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
