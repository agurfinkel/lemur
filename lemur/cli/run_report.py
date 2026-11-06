"""Summarize a saved Z3 run without rerunning the solver."""

import json
import sys
from pathlib import Path

from lemur.cli import agent_help
from lemur.run_report import build_report, render_plain, summarize_report


def register(subparsers):
    p = subparsers.add_parser(
        "run-report", help="Explain a saved Z3 run's phases and search progress"
    )
    agent_help.add_agent_flag(p, "run-report")
    p.add_argument(
        "prefix",
        nargs="?",
        help="Sweep prefix: discovers .stdout/.stderr/.trace/.run.json",
    )
    p.add_argument("--stdout", help="Explicit solver stdout file")
    p.add_argument("--stderr", help="Explicit verbose stderr file (capture with -v:10)")
    p.add_argument("--trace", help="Explicit text trace file")
    p.add_argument(
        "--run-metadata",
        help="Sweep .run.json metadata (exit code, limits, provenance)",
    )
    output = p.add_mutually_exclusive_group()
    output.add_argument("--format", "-f", choices=["plain", "json"], default="plain")
    output.add_argument(
        "--json",
        dest="format",
        action="store_const",
        const="json",
        help="Full structured evidence (same as --format json)",
    )
    p.add_argument(
        "--summary",
        action="store_true",
        help="Compact JSON findings; plain output is already a summary",
    )
    p.set_defaults(func=run)


def run(args):
    paths = {}
    for kind in ("stdout", "stderr", "trace"):
        path = getattr(args, kind)
        if path is None and args.prefix:
            candidate = Path(f"{args.prefix}.{kind}")
            path = candidate if candidate.is_file() else None
        paths[kind] = path
    metadata = args.run_metadata
    if metadata is None and args.prefix:
        candidate = Path(f"{args.prefix}.run.json")
        metadata = candidate if candidate.is_file() else None
    paths["run_metadata"] = metadata
    if not any(paths.values()):
        print(
            "Error: supply a prefix with saved files or --stdout/--stderr/--trace",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        report = build_report(**paths)
        json_report = summarize_report(report) if args.summary else report
        print(
            json.dumps(json_report, indent=2)
            if args.format == "json"
            else render_plain(report)
        )
    except (OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
