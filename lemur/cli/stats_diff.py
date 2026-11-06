"""lemur stats-diff: side-by-side arith_conflict diff between two traces."""

import sys
from pathlib import Path

from lemur.cli import agent_help


def register(subparsers):
    p = subparsers.add_parser(
        "stats-diff",
        help="Side-by-side arith_conflict summaries between two traces "
        "(hot blocks, top constants, premise-shape histogram, "
        "with deltas).",
        epilog="AI agents: use `lemur stats-diff --agent`.",
    )
    agent_help.add_agent_flag(p, "stats-diff")
    p.add_argument("a", help="First .z3-trace file (A).")
    p.add_argument("b", help="Second .z3-trace file (B).")
    p.add_argument(
        "--top-k",
        type=int,
        default=5,
        metavar="N",
        help="Cap rows in ranked subsections (hot blocks, top "
        "constants); default 5. Premise-shape histogram is "
        "always 4 rows.",
    )
    p.add_argument(
        "--format",
        "-f",
        choices=["rich", "plain", "json"],
        default=None,
        help="Output format (default: rich for TTY, plain otherwise)",
    )
    p.add_argument("--no-color", action="store_true", help="Disable color")
    p.set_defaults(func=run)


def run(args):
    from lemur import stats_diff
    from lemur.table import make_console

    a_path = Path(args.a).resolve()
    b_path = Path(args.b).resolve()
    for p in (a_path, b_path):
        if not p.exists():
            print(f"Error: trace file not found: {p}", file=sys.stderr)
            sys.exit(1)

    try:
        d = stats_diff.diff_arith_conflict(str(a_path), str(b_path), top_k=args.top_k)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    fmt = args.format
    effective_fmt = (
        fmt if fmt is not None else ("rich" if sys.stdout.isatty() else "plain")
    )

    if effective_fmt == "rich":
        stats_diff.render_rich(d, make_console(no_color=args.no_color))
    elif effective_fmt == "plain":
        sys.stdout.write(stats_diff.render_plain(d))
    elif effective_fmt == "json":
        print(stats_diff.render_json(d))
