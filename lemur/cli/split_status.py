"""lemur split-status: Walk a recursive split tree, aggregate stats."""

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from lemur.cli import agent_help
from lemur.split import Plan, SplitError, read_plan
from lemur.table import make_console


@dataclass
class _PlanNode:
    plan_path: Path  # path to plan.json
    plan_dir: Path  # its containing directory
    rel_path: Path  # plan_dir relative to the scan root
    depth: int  # 0 for the root plan.json, 1 for its children, ...
    plan: Plan


def register(subparsers):
    p = subparsers.add_parser(
        "split-status",
        help="Aggregate split stats across a recursive leaves/ tree",
        epilog="AI agents: use `lemur split-status --agent` for terse usage guide.",
    )
    agent_help.add_agent_flag(p, "split-status")
    p.add_argument(
        "directory", help="Top of the split tree (a directory with plan.json)"
    )
    p.add_argument(
        "--verbose", "-v", action="store_true", help="List every leaf with its path"
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


def _walk(root: Path) -> list[_PlanNode]:
    """Find every plan.json under `root`; return nodes with depth."""
    nodes: list[_PlanNode] = []
    for plan_path in sorted(root.rglob("plan.json")):
        plan_dir = plan_path.parent
        try:
            plan = read_plan(str(plan_path))
        except SplitError:
            continue
        rel = plan_dir.relative_to(root)
        depth = len(rel.parts)  # root's plan.json has depth 0
        nodes.append(
            _PlanNode(
                plan_path=plan_path,
                plan_dir=plan_dir,
                rel_path=rel,
                depth=depth,
                plan=plan,
            )
        )
    return nodes


def _leaf_has_children(node: _PlanNode, leaf_file: str | None) -> bool:
    """A leaf has been recursively split if `<stem>_children/plan.json` exists
    in the same directory as the leaf."""
    if leaf_file is None:
        return False
    stem = Path(leaf_file).stem
    return (node.plan_dir / f"{stem}_children" / "plan.json").exists()


def run(args):
    root = Path(args.directory).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    # Require the root itself to be a plan dir; otherwise this isn't a
    # split tree and the command is meaningless.
    if not (root / "plan.json").exists():
        print(f"Error: {root}/plan.json not found; not a split tree", file=sys.stderr)
        sys.exit(1)

    nodes = _walk(root)

    total_leaves = sum(len(n.plan.leaves) for n in nodes)
    emitted = sum(
        1 for n in nodes for leaf in n.plan.leaves if not leaf.pruned and leaf.file
    )
    pruned = sum(1 for n in nodes for leaf in n.plan.leaves if leaf.pruned)
    max_depth = max(n.depth for n in nodes)
    with_results = sum(1 for n in nodes if n.plan.results)

    fmt = args.format
    effective_fmt = (
        fmt if fmt is not None else ("rich" if sys.stdout.isatty() else "plain")
    )

    if effective_fmt == "json":
        out = {
            "root": str(root),
            "plans": len(nodes),
            "max_depth": max_depth,
            "leaves": {"total": total_leaves, "emitted": emitted, "pruned": pruned},
            "plans_with_results": with_results,
            "tree": [
                {
                    "path": str(n.rel_path) if str(n.rel_path) != "." else ".",
                    "depth": n.depth,
                    "source": n.plan.source,
                    "split_predicates": [c.name for c in n.plan.split_predicates],
                    "leaves": [
                        {
                            "file": leaf.file,
                            "pruned": leaf.pruned,
                            "recursed": _leaf_has_children(n, leaf.file),
                            "valuation": leaf.valuation,
                        }
                        for leaf in n.plan.leaves
                    ],
                    "results_populated": n.plan.results is not None,
                }
                for n in nodes
            ],
        }
        print(json.dumps(out, indent=2))
        return

    if effective_fmt == "plain":
        print(f"root: {root}")
        print(f"plans: {len(nodes)}  max_depth: {max_depth}")
        print(f"leaves: {total_leaves} total  emitted: {emitted}  pruned: {pruned}")
        print(f"plans_with_results: {with_results}")
        for n in nodes:
            disp = str(n.rel_path) if str(n.rel_path) != "." else "."
            print(f"  [depth={n.depth}] {disp}")
            print(
                f"    source: {n.plan.source}  "
                f"predicates: {len(n.plan.split_predicates)}  "
                f"leaves: {len(n.plan.leaves)} "
                f"(emitted {sum(1 for leaf in n.plan.leaves if not leaf.pruned and leaf.file)}, "
                f"pruned {sum(1 for leaf in n.plan.leaves if leaf.pruned)})"
            )
            if args.verbose:
                for leaf in n.plan.leaves:
                    mark = (
                        "P"
                        if leaf.pruned
                        else ("R" if _leaf_has_children(n, leaf.file) else " ")
                    )
                    print(f"    {mark} {leaf.file or '(pruned)'}")
        return

    # Rich
    from rich.table import Table

    console = make_console(no_color=args.no_color)
    console.print(f"[bold]split tree:[/bold] {root}")
    console.print(
        f"plans=[bold]{len(nodes)}[/bold]  "
        f"max_depth=[bold]{max_depth}[/bold]  "
        f"leaves: [bold]{total_leaves}[/bold] total "
        f"([green]{emitted} emitted[/green], [yellow]{pruned} pruned[/yellow])  "
        f"plans_with_results=[bold]{with_results}[/bold]"
    )

    t = Table(title="Plans", pad_edge=True)
    t.add_column("depth", justify="right")
    t.add_column("path", style="bold")
    t.add_column("source", style="dim")
    t.add_column("preds", justify="right")
    t.add_column("leaves", justify="right")
    t.add_column("emitted", justify="right", style="green")
    t.add_column("pruned", justify="right", style="yellow")
    t.add_column("results?")
    for n in nodes:
        disp = str(n.rel_path) if str(n.rel_path) != "." else "."
        n_em = sum(1 for leaf in n.plan.leaves if not leaf.pruned and leaf.file)
        n_pr = sum(1 for leaf in n.plan.leaves if leaf.pruned)
        t.add_row(
            str(n.depth),
            disp,
            n.plan.source,
            str(len(n.plan.split_predicates)),
            str(len(n.plan.leaves)),
            str(n_em),
            str(n_pr),
            "yes" if n.plan.results else "—",
        )
    console.print(t)

    if args.verbose:
        lt = Table(title="Leaves", pad_edge=True)
        lt.add_column("path", style="bold")
        lt.add_column("state")
        lt.add_column("recursed?")
        for n in nodes:
            prefix = str(n.rel_path) if str(n.rel_path) != "." else ""
            for leaf in n.plan.leaves:
                leaf_path = (
                    f"{prefix}/{leaf.file}"
                    if (prefix and leaf.file)
                    else (leaf.file or "(pruned)")
                )
                state = "pruned" if leaf.pruned else "emitted"
                recursed = "yes" if _leaf_has_children(n, leaf.file) else ""
                lt.add_row(leaf_path, state, recursed)
        console.print(lt)
