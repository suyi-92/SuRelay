#!/usr/bin/env python3
"""Stage install SSH listeners, then retire old listeners after login confirmation."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

from surelay_core import Error, GEN, Manager
from surelay_security import Security


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-env", type=Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--stage", action="store_true")
    action.add_argument("--final", action="store_true")
    args = parser.parse_args()
    manager = Manager(args.config_env.resolve().parent)
    if manager.config.resolve() != args.config_env.resolve():
        manager.config = args.config_env.resolve()
        manager.reload()
    security = Security(manager)
    # --final is only invoked by ssh-final after CONFIRM_ROOT_KEY_LOGIN=yes.
    security.change_ssh_port(manager.env["SSH_PORT"], lambda _new: None, stage=args.stage)


if __name__ == "__main__":
    try:
        main()
    except (Error, GEN.ConfigError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
