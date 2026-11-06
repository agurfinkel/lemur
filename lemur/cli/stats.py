"""lemur stats: General trace file statistics."""

import sys
from pathlib import Path

from lemur.cli import agent_help
from lemur.stats import build_stats_output
from lemur.table import make_console, output


def register(subparsers):
    p = subparsers.add_parser(
        "stats",
        help="General trace file statistics",
        epilog="AI agents: use `lemur stats --agent` for terse usage guide. "
        "For NLA lemma analysis, use `lemur nla`.",
    )
    agent_help.add_agent_flag(p, "stats")
    p.add_argument("trace", help="Path to .z3-trace file")
    p.add_argument(
        "--tag",
        action="append",
        default=None,
        help="Filter to specific tag(s). Repeatable.",
    )
    p.add_argument(
        "--function",
        "--fn",
        action="append",
        default=None,
        help="Filter to specific function(s). Repeatable.",
    )
    p.add_argument(
        "--top-k",
        type=int,
        default=5,
        metavar="N",
        help="How many entries to show per ranked subsection "
        "(currently used by arith_conflict hot-block / "
        "top-constant lists; default 5)",
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
    trace_path = Path(args.trace)
    if not trace_path.exists():
        print(f"Error: trace file not found: {trace_path}", file=sys.stderr)
        sys.exit(1)

    stats_out = build_stats_output(
        trace_path,
        tags=args.tag,
        functions=args.function,
        top_k=args.top_k,
    )

    fmt = args.format
    console = (
        make_console(no_color=args.no_color)
        if fmt != "plain" and fmt != "json"
        else None
    )
    output(stats_out, fmt=fmt, console=console)
