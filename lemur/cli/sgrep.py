"""lemur sgrep: structural search over an SMT2 file's AST."""

import gc
import json
import sys
from pathlib import Path

from lemur.cli import agent_help


def register(subparsers):
    p = subparsers.add_parser(
        "sgrep",
        help="Structural search over SMT2 ASTs (s-expression patterns).",
        epilog="Pattern syntax: _ wildcard, ?name capture (same name unifies "
        "by id-equality), (head c1 c2 ...) compound match, NAME "
        "literal, type filters ?x:Bool|Numeral|Var|Expr, negation "
        "!?x:T or ?x:!T. AI agents: use `lemur sgrep --agent`.",
    )
    agent_help.add_agent_flag(p, "sgrep")
    p.add_argument("file", help="SMT2 input file")
    p.add_argument(
        "pattern", nargs="?", default=None, help="Pattern. Omit to get --summary."
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--summary", action="store_true", help="File overview (default if no pattern)."
    )
    mode.add_argument(
        "--count", action="store_true", help="Print number of matches and exit."
    )
    mode.add_argument(
        "--list",
        action="store_true",
        help="Print one line per match (default if pattern).",
    )
    mode.add_argument(
        "--distinct", action="store_true", help="--list with duplicates removed."
    )
    p.add_argument(
        "--apply",
        metavar="TACTIC",
        default=None,
        help="Apply z3 tactic before searching. Accepts a single "
        "tactic name or a `(then t1 t2 ...)` chain.",
    )
    p.add_argument(
        "--check-pattern",
        action="store_true",
        help="Validate the positional PATTERN syntactically and "
        "exit. Skips file I/O and tactic application — use "
        "while iterating on a pattern to short-circuit slow "
        "--apply cycles. Exits 0 on valid, 2 on error.",
    )
    p.add_argument(
        "--show",
        choices=["captures", "kind"],
        default=None,
        help="Per-match extras: `captures` prints full capture "
        "bindings; `kind` prints a one-line classification "
        "(Var(name) / Numeral(N) / op-head) per capture — "
        "use this when --distinct over compound guards "
        "would otherwise dump huge subtrees.",
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
        help="Inline let-aliases in printed expressions. Beware: "
        "deeply-shared subterms can blow up exponentially.",
    )
    p.set_defaults(func=run)


def _eff_mode(args) -> str:
    if args.summary:
        return "summary"
    if args.count:
        return "count"
    if args.list:
        return "list"
    if args.distinct:
        return "distinct"
    return "summary" if args.pattern is None else "list"


def run(args):
    from lemur import sgrep

    mode = _eff_mode(args)
    if mode == "summary" and args.pattern is not None:
        print(
            "Error: --summary with a positional pattern is ambiguous; drop one.",
            file=sys.stderr,
        )
        sys.exit(2)
    if mode != "summary" and args.pattern is None:
        print("Error: pattern required for --count/--list/--distinct.", file=sys.stderr)
        sys.exit(2)

    z3 = sgrep._import_z3()

    # Validate user-input strings (pattern, tactic) up front. This shaves
    # the slow --apply round-trip off the iteration loop when the user is
    # tweaking a pattern.
    pat = None
    if args.pattern is not None:
        try:
            pat = sgrep.parse_pattern(args.pattern)
        except sgrep.PatternError as e:
            print(f"Error: pattern: {e}", file=sys.stderr)
            sys.exit(2)
    if args.check_pattern:
        # Pattern is valid; that's all the user wanted.
        return
    tac = None
    if args.apply:
        try:
            tac = sgrep.parse_tactic(z3, args.apply)
        except sgrep.TacticParseError as e:
            print(f"Error: --apply: {e}", file=sys.stderr)
            sys.exit(2)

    bench = Path(args.file).resolve()
    if not bench.exists():
        print(f"Error: file not found: {bench}", file=sys.stderr)
        sys.exit(1)

    sgrep.set_pp_aliases(z3, args.expand_aliases)

    try:
        goal = sgrep.parse_smt2_to_goal(z3, str(bench))
    except Exception as e:
        print(f"Error: parse failed: {e}", file=sys.stderr)
        sys.exit(1)

    if tac is not None:
        goal = sgrep.apply_tactic_to_goal(z3, goal, tac)

    try:
        _do_run(z3, sgrep, goal, pat, mode, args)
    finally:
        # Drop z3 native refs so the atexit handler doesn't surface them as
        # 'Uncollected memory' warnings on debug builds.
        del goal
        del tac
        for _ in range(2):
            gc.collect()


def _do_run(z3, sgrep, goal, pat, mode, args) -> None:
    if mode == "summary":
        s = sgrep.compute_summary(z3, goal)
        if args.format == "json":
            print(json.dumps(_summary_to_jsonable(s), indent=2))
        else:
            _render_summary(s)
        return

    matches = sgrep.find_matches(z3, pat, sgrep.goal_top_level_exprs(goal))

    if mode == "count":
        if args.format == "json":
            print(json.dumps({"count": len(matches)}))
        else:
            print(len(matches))
        return

    if mode == "distinct":
        seen_strs: set[str] = set()
        unique: list = []
        for m in matches:
            ks = str(m.expr)
            if ks in seen_strs:
                continue
            seen_strs.add(ks)
            unique.append(m)
        matches = unique

    if args.format == "json":
        for m in matches:
            if args.show == "kind":
                obj = {"kind": sgrep.describe_kind(z3, m.expr)}
                if m.captures:
                    obj["captures"] = {
                        k: sgrep.describe_kind(z3, v) for k, v in m.captures.items()
                    }
            else:
                obj = {"expr": str(m.expr)}
                if m.captures and args.show == "captures":
                    obj["captures"] = {k: str(v) for k, v in m.captures.items()}
            print(json.dumps(obj))
        return

    for m in matches:
        if args.show == "kind":
            # Suppress full expr — that's the whole point of --show kind:
            # consumable output when match.expr or captures are large
            # subtrees. Print kind-of-match + kind-of-each-capture.
            head = sgrep.describe_kind(z3, m.expr)
            if m.captures:
                caps = "  ".join(
                    f"?{k}={sgrep.describe_kind(z3, v)}" for k, v in m.captures.items()
                )
                print(f"{head}  [{caps}]")
            else:
                print(head)
            continue
        line = str(m.expr)
        if args.show == "captures" and m.captures:
            caps = "  ".join(f"?{k}={v}" for k, v in m.captures.items())
            line = f"{line}    [{caps}]"
        print(line)


def _summary_to_jsonable(s) -> dict:
    return {
        "asserts": s.num_asserts,
        "decls_by_sort": dict(s.decls_by_sort),
        "top_ops": dict(s.top_ops),
        "shape_counts": dict(s.shape_counts),
        "max_depth": s.max_depth,
    }


def _render_summary(s) -> None:
    print(f"asserts: {s.num_asserts}")
    print("declarations:")
    for sort_name, n in sorted(s.decls_by_sort.items(), key=lambda kv: -kv[1]):
        print(f"  {sort_name}: {n}")
    print("top operators:")
    for op, n in s.top_ops.most_common(15):
        print(f"  {op}: {n}")
    print("shape counts:")
    for spec, n in s.shape_counts.items():
        print(f"  {spec}: {n}")
    print(f"max nesting depth: {s.max_depth}")
