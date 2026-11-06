"""lemur tally: Aggregate sweep CSV results per-config."""

import sys
from pathlib import Path

from lemur.cli import agent_help
from lemur.table import make_console
from lemur.tally import compute_tally, read_sweep_csv, render_rich, to_csv, to_json


def register(subparsers):
    p = subparsers.add_parser(
        "tally",
        help="Aggregate sweep CSV by config",
        epilog="AI agents: use `lemur tally --agent` for terse usage guide.",
    )
    agent_help.add_agent_flag(p, "tally")
    p.add_argument(
        "csv_file",
        help="Sweep CSV file (columns: config,seed,status,time_s; "
        "optional leading `split` column from `sweep --split`)",
    )
    p.add_argument(
        "--format",
        "-f",
        choices=["rich", "plain", "json"],
        default=None,
        help="Output format (default: rich for TTY, plain otherwise)",
    )
    p.add_argument("--no-color", action="store_true", help="Disable color output")
    p.set_defaults(func=run)


def run(args):
    csv_path = Path(args.csv_file).resolve()
    if not csv_path.exists():
        print(f"Error: file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    try:
        rows = read_sweep_csv(str(csv_path))
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    tally = compute_tally(rows)

    fmt = args.format
    effective_fmt = (
        fmt if fmt is not None else ("rich" if sys.stdout.isatty() else "plain")
    )

    if effective_fmt == "rich":
        render_rich(tally, make_console(no_color=args.no_color))
    elif effective_fmt == "plain":
        print(to_csv(tally), end="")
    elif effective_fmt == "json":
        print(to_json(tally))
