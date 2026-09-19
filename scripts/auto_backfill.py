#!/usr/bin/env python3
"""Run all official-source backfills continuously and install a macOS service."""

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.config import DATABASE_URL
from market_data.auto_backfill import AutoBackfillRunner, StateStore, phase_commands


LABEL = "com.indian-stock-market-data.backfill"


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "install", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--database-url", default=DATABASE_URL)
        command.add_argument(
            "--state-file",
            type=Path,
            default=Path("data/databases/auto-backfill-state.json"),
        )
    run = subparsers.choices["run"]
    run.add_argument("--once", action="store_true")
    run.add_argument("--min-free-gib", type=float, default=12.0)
    run.add_argument("--poll-interval", type=int, default=300)
    install = subparsers.choices["install"]
    install.add_argument("--min-free-gib", type=float, default=12.0)
    return parser


def install(args) -> int:
    root = Path(__file__).resolve().parents[1]
    python = root / "venv" / "bin" / "python"
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    launch_agents = Path.home() / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    plist_path = launch_agents / "{}.plist".format(LABEL)
    payload = {
        "Label": LABEL,
        "ProgramArguments": [
            str(python),
            str(Path(__file__).resolve()),
            "run",
            "--database-url",
            args.database_url,
            "--state-file",
            str(args.state_file.resolve()),
            "--min-free-gib",
            str(args.min_free_gib),
        ],
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 60,
        "StandardOutPath": str(logs / "auto-backfill.log"),
        "StandardErrorPath": str(logs / "auto-backfill-error.log"),
    }
    with plist_path.open("wb") as handle:
        plistlib.dump(payload, handle)
    domain = "gui/{}".format(os.getuid())
    subprocess.run(
        ["launchctl", "bootout", domain, str(plist_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=True,
    )
    print(plist_path)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "status":
        print(json.dumps(StateStore(args.state_file).load(), indent=2, sort_keys=True))
        return 0
    if args.command == "install":
        return install(args)

    root = Path(__file__).resolve().parents[1]
    runner = AutoBackfillRunner(
        phase_commands(str(root / "venv" / "bin" / "python"), args.database_url),
        StateStore(args.state_file),
        Path("{}-auto.lock".format(args.state_file)),
        int(args.min_free_gib * 1024 ** 3),
        poll_interval=args.poll_interval,
    )
    try:
        runner.run(once=args.once)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
