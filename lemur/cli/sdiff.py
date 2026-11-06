"""lemur sdiff: structural diff between two SMT2 files.

Default mode prints a shape-count table (asserts, declarations by sort,
distinct shape counts for the standard div/mod/ite/mul-of-ITE patterns,
max nesting depth) with the count in A, count in B, and the delta. With
`--pattern PATTERN`, restricts the diff to that single user-supplied
pattern.

Both files can be pre-processed by a tactic via `--apply 'TACTIC'`,
matching `lemur sgrep`. The tactic-string grammar is identical: a single
tactic name, or `(then t1 t2 ...)`.
"""

import gc
import json
import sys
from pathlib import Path

from lemur.cli import agent_help


def register(subparsers):
    p = subparsers.add_parser(
        "sdiff",
        help="Structural diff between two SMT2 files.",
        epilog="With no --pattern, prints a shape-count table; with "
        "--pattern, prints A_count / B_count / delta for that one "
        "pattern. AI agents: use `lemur sdiff --agent`.",
    )
    agent_help.add_agent_flag(p, "sdiff")
    p.add_argument("a", help="First SMT2 file (A)")
    p.add_argument("b", help="Second SMT2 file (B)")
    p.add_argument(
        "--apply",
        metavar="TACTIC",
        default=None,
        help="Apply the same tactic to both files before diffing. "
        "Mutually exclusive with --apply-a / --apply-b.",
    )
    p.add_argument(
        "--apply-a",
        metavar="TACTIC",
        default=None,
        help="Apply this tactic only to file A (asymmetric mode). "
        "Use together with --apply-b to compare two "
        "preprocessing pipelines on the same source.",
    )
    p.add_argument(
        "--apply-b",
        metavar="TACTIC",
        default=None,
        help="Apply this tactic only to file B (asymmetric mode).",
    )
    p.add_argument(
        "--pattern",
        metavar="PATTERN",
        default=None,
        help="Restrict diff to a single sgrep-style pattern.",
    )
    p.add_argument(
        "--show-same",
        action="store_true",
        help="Include rows where A_count == B_count.",
    )
    p.add_argument(
        "--format",
        "-f",
        choices=["plain", "json"],
        default="plain",
        help="Output format (default: plain).",
    )
    p.add_argument(
        "--expand-aliases",
        action="store_true",
        help="Inline z3 let-aliases in printed output (matches sgrep's flag).",
    )
    p.set_defaults(func=run)


def _load_goal(z3, sgrep, path: Path, apply_str: str | None):
    g = sgrep.parse_smt2_to_goal(z3, str(path))
    if apply_str:
        tac = sgrep.parse_tactic(z3, apply_str)
        g = sgrep.apply_tactic_to_goal(z3, g, tac)
    return g


def run(args):
    from lemur import sgrep

    a_path = Path(args.a).resolve()
    b_path = Path(args.b).resolve()
    for p in (a_path, b_path):
        if not p.exists():
            print(f"Error: file not found: {p}", file=sys.stderr)
            sys.exit(1)

    if args.apply is not None and (
        args.apply_a is not None or args.apply_b is not None
    ):
        print(
            "Error: --apply is mutually exclusive with --apply-a / --apply-b.",
            file=sys.stderr,
        )
        sys.exit(2)

    apply_a = args.apply_a if args.apply_a is not None else args.apply
    apply_b = args.apply_b if args.apply_b is not None else args.apply

    z3 = sgrep._import_z3()
    sgrep.set_pp_aliases(z3, args.expand_aliases)

    try:
        g_a = _load_goal(z3, sgrep, a_path, apply_a)
        g_b = _load_goal(z3, sgrep, b_path, apply_b)
    except sgrep.TacticParseError as e:
        print(f"Error: --apply: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.pattern is not None:
        try:
            pat = sgrep.parse_pattern(args.pattern)
        except sgrep.PatternError as e:
            print(f"Error: pattern: {e}", file=sys.stderr)
            sys.exit(2)
        ca = len(sgrep.find_matches(z3, pat, sgrep.goal_top_level_exprs(g_a)))
        cb = len(sgrep.find_matches(z3, pat, sgrep.goal_top_level_exprs(g_b)))
        rows = [(args.pattern, ca, cb)]
    else:
        sa = sgrep.compute_summary(z3, g_a)
        sb = sgrep.compute_summary(z3, g_b)
        rows = _summary_rows(sa, sb)

    if not args.show_same:
        rows = [r for r in rows if r[1] != r[2]]

    try:
        if args.format == "json":
            out = [
                {"shape": r[0], "a": r[1], "b": r[2], "delta": r[2] - r[1]}
                for r in rows
            ]
            print(
                json.dumps({"a": str(a_path), "b": str(b_path), "rows": out}, indent=2)
            )
            return
        _render_plain_table(rows, a_path, b_path)
    finally:
        # Drop z3 native refs ahead of the atexit handler.
        del g_a
        del g_b
        for _ in range(2):
            gc.collect()


def _summary_rows(sa, sb) -> list[tuple[str, int, int]]:
    rows: list[tuple[str, int, int]] = []
    rows.append(("asserts", sa.num_asserts, sb.num_asserts))
    sorts = sorted(set(sa.decls_by_sort) | set(sb.decls_by_sort))
    for s in sorts:
        rows.append(
            (
                f"declarations ({s})",
                int(sa.decls_by_sort.get(s, 0)),
                int(sb.decls_by_sort.get(s, 0)),
            )
        )
    for spec in list(sa.shape_counts.keys()):
        rows.append((spec, sa.shape_counts.get(spec, 0), sb.shape_counts.get(spec, 0)))
    rows.append(("max nesting depth", sa.max_depth, sb.max_depth))
    return rows


def _fmt_delta(a: int, b: int) -> str:
    d = b - a
    if d == 0:
        return "="
    return f"+{d}" if d > 0 else str(d)


def _render_plain_table(rows, a_path, b_path) -> None:
    if not rows:
        print(f"# A: {a_path}")
        print(f"# B: {b_path}")
        print("(no differences)")
        return
    label_w = max(len("shape"), max(len(r[0]) for r in rows))
    a_w = max(len("A"), max(len(str(r[1])) for r in rows))
    b_w = max(len("B"), max(len(str(r[2])) for r in rows))
    print(f"# A: {a_path}")
    print(f"# B: {b_path}")
    print(f"{'shape':<{label_w}}  {'A':>{a_w}}  {'B':>{b_w}}  delta")
    print(f"{'-' * label_w}  {'-' * a_w}  {'-' * b_w}  -----")
    for label, a, b in rows:
        print(f"{label:<{label_w}}  {a:>{a_w}}  {b:>{b_w}}  {_fmt_delta(a, b)}")
