"""lemur nla-diff: structured diff of two nla_solver traces."""

import json
import sys
from pathlib import Path

from lemur.cli import agent_help


def register(subparsers):
    p = subparsers.add_parser(
        "nla-diff",
        help="Structured diff of two nla_solver traces (lemma counts, "
        "strategy distribution, is_patch_blocked rate, top-fingerprint "
        "stability).",
        epilog="AI agents: use `lemur nla-diff --agent`.",
    )
    agent_help.add_agent_flag(p, "nla-diff")
    p.add_argument("a", help="First .z3-trace file (A)")
    p.add_argument("b", help="Second .z3-trace file (B)")
    p.add_argument(
        "--nra-a",
        default=None,
        metavar="PATH",
        help="Optional separate -tr:nra trace for A (default: "
        "look for [nra] entries inside A itself). Used for "
        "stable x-form nlsat-call fingerprints.",
    )
    p.add_argument(
        "--nra-b",
        default=None,
        metavar="PATH",
        help="Optional separate -tr:nra trace for B.",
    )
    p.add_argument(
        "--top",
        type=int,
        default=5,
        metavar="N",
        help="Show top N strategies / functions / fingerprints (default: 5).",
    )
    p.add_argument(
        "--format",
        "-f",
        choices=["plain", "json"],
        default="plain",
        help="Output format (default: plain).",
    )
    p.set_defaults(func=run)


def run(args):
    from lemur import nla_diff

    a_path = Path(args.a).resolve()
    b_path = Path(args.b).resolve()
    for p in (a_path, b_path):
        if not p.exists():
            print(f"Error: trace file not found: {p}", file=sys.stderr)
            sys.exit(1)

    try:
        m_a = nla_diff.compute_metrics(str(a_path), nra_trace_path=args.nra_a)
        m_b = nla_diff.compute_metrics(str(b_path), nra_trace_path=args.nra_b)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    rows = nla_diff.diff(m_a, m_b, top=args.top)

    if args.format == "json":
        print(
            json.dumps(nla_diff.to_jsonable(rows, str(a_path), str(b_path)), indent=2)
        )
        return
    sys.stdout.write(nla_diff.render_plain(rows, str(a_path), str(b_path)))
