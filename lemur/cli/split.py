"""lemur split: Auto-discover Boolean case-splits in an SMT2 benchmark."""

import json
import sys
from pathlib import Path

from lemur.cli import agent_help
from lemur.table import make_console


def register(subparsers):
    p = subparsers.add_parser(
        'split',
        help='Auto-discover Boolean case-splits; emit leaf .smt2 files + plan.json',
        epilog='AI agents: use `lemur split --agent` for terse usage guide. '
               'Requires the [split] extra: run `uv sync --extra split` in the checkout.',
    )
    agent_help.add_agent_flag(p, 'split')
    p.add_argument('benchmark', help='SMT2 benchmark file')
    p.add_argument('--out', default=None, metavar='DIR',
                   help='Output directory (default: <benchmark-stem>_children/ '
                        'next to the source)')
    p.add_argument('--max-leaves', type=int, default=32,
                   help='Cap on number of leaves, 2^k (default: 32, floor 8)')
    p.add_argument('--split-score-threshold', type=float, default=10.0,
                   help='Minimum score for a candidate to be accepted '
                        '(default: 10)')
    p.add_argument('--split-probe-timeout', type=float, default=5.0,
                   metavar='SECS',
                   help='Per-candidate simplification timeout in seconds '
                        '(default: 5)')
    p.add_argument('--split-name-pattern', default=r'BLK__\d+', metavar='REGEX',
                   help='Regex identifying reachability Bool candidates '
                        '(default: BLK__\\d+)')
    p.add_argument('--split-only', default=None, metavar='REGEX',
                   help='Hard allowlist: restrict split candidates (both '
                        'reachability and ITE guards) to names matching this '
                        'regex. Use to force a split on a specific predicate, '
                        'e.g. \'^BLK__49_1_0_0_0_0$\'.')
    p.add_argument('--weight-nla-ite-collapse', type=float, default=5.0,
                   metavar='W',
                   help='Score weight for the (div|mod|mul)-of-ITE shrinkage '
                        'axis. NLA solvers pay extra per ITE-wrapped factor, '
                        'so eliminating these terms is worth more than a '
                        'generic ITE removal. 0 disables. Default: 5.')
    p.add_argument('--plan-only', action='store_true',
                   help='Write plan.json only; no leaf .smt2 files on disk')
    p.add_argument('--force', action='store_true',
                   help='Overwrite --out directory if it already has a plan.json')
    p.add_argument('--format', '-f', choices=['rich', 'plain', 'json'],
                   default=None,
                   help='Output format for the summary (default: rich for TTY)')
    p.add_argument('--no-color', action='store_true', help='Disable color')
    p.add_argument('--quiet', '-q', action='store_true',
                   help='Suppress per-phase progress messages on stderr')
    p.set_defaults(func=run)


def run(args):
    bench = Path(args.benchmark).resolve()
    if not bench.exists():
        print(f"Error: benchmark file not found: {bench}", file=sys.stderr)
        sys.exit(1)

    # Default --out: <bench-parent>/<stem>_children/
    if args.out:
        out_dir = Path(args.out).resolve()
    else:
        out_dir = bench.parent / f"{bench.stem}_children"

    # Overwrite safety — error if an existing plan.json is present.
    if (out_dir / 'plan.json').exists() and not args.force:
        print(f"Error: {out_dir}/plan.json already exists. "
              f"Pass --force to overwrite.", file=sys.stderr)
        sys.exit(2)

    # Bound max_leaves (floor 8 per proposal).
    max_leaves = max(8, int(args.max_leaves))

    # Lazy import — we only need z3 here, not at CLI registration time.
    from lemur import split as split_mod

    def _log(msg: str) -> None:
        print(f"[split] {msg}", file=sys.stderr, flush=True)

    try:
        plan = split_mod.build_plan(
            str(bench),
            max_leaves=max_leaves,
            threshold=args.split_score_threshold,
            probe_timeout=args.split_probe_timeout,
            name_pattern=args.split_name_pattern,
            restrict_pattern=args.split_only,
            nla_ite_weight=args.weight_nla_ite_collapse,
            log=None if args.quiet else _log,
        )
    except split_mod.SplitError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not plan.split_predicates:
        print(f"[split] no profitable splits found (threshold "
              f"{args.split_score_threshold}).", file=sys.stderr)
        # Still write plan.json so downstream tools can detect the decision.
        split_mod.emit_leaves(plan, str(bench), str(out_dir),
                              plan_only=args.plan_only)
        fmt = _effective_fmt(args.format)
        _render_summary(plan, out_dir, fmt, args.no_color)
        return

    split_mod.emit_leaves(plan, str(bench), str(out_dir),
                          plan_only=args.plan_only)

    fmt = _effective_fmt(args.format)
    _render_summary(plan, out_dir, fmt, args.no_color)


def _effective_fmt(fmt: str | None) -> str:
    if fmt is not None:
        return fmt
    return 'rich' if sys.stdout.isatty() else 'plain'


def _render_summary(plan, out_dir: Path, fmt: str, no_color: bool) -> None:
    live = [l for l in plan.leaves if not l.pruned]
    pruned = [l for l in plan.leaves if l.pruned]

    if fmt == 'json':
        out = {
            "out_dir": str(out_dir),
            "split_predicates": [
                {"name": c.name, "score": c.score,
                 "reduces_to_false_on": c.reduces_to_false_on}
                for c in plan.split_predicates
            ],
            "leaves_total": len(plan.leaves),
            "leaves_emitted": len(live),
            "leaves_pruned": len(pruned),
        }
        print(json.dumps(out, indent=2))
        return

    if fmt == 'plain':
        print(f"out_dir: {out_dir}")
        print(f"split_predicates: {len(plan.split_predicates)}")
        for c in plan.split_predicates:
            suffix = (f"  (reduces to false on {c.reduces_to_false_on})"
                      if c.reduces_to_false_on else "")
            print(f"  {c.name}  score={c.score:.1f}{suffix}")
        print(f"leaves: {len(plan.leaves)} total "
              f"({len(live)} emitted, {len(pruned)} pruned)")
        return

    # Rich
    from rich.table import Table
    console = make_console(no_color=no_color)
    console.print(f"[bold]lemur split[/bold]  out: {out_dir}")
    if plan.split_predicates:
        t = Table(title="Plan", pad_edge=True)
        t.add_column("#", justify="right", style="dim")
        t.add_column("predicate", style="bold")
        t.add_column("score", justify="right")
        t.add_column("reduces→false", justify="center")
        for i, c in enumerate(plan.split_predicates, 1):
            rtf = c.reduces_to_false_on or ""
            t.add_row(str(i), c.name, f"{c.score:.1f}", rtf)
        console.print(t)
    console.print(
        f"Leaves: [bold]{len(plan.leaves)}[/bold] total  "
        f"[green]{len(live)} emitted[/green]  "
        f"[yellow]{len(pruned)} pruned[/yellow]  "
        f"→ `lemur sweep {out_dir}/ --stop-on-per-split unsat --tally`"
    )
