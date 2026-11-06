"""
Compare z3 `-st` statistics across configs.

Two input modes:
  1. Sweep-save directory: `<config>_s<seed>.stats.json` files written by
     `lemur sweep --stats --save`. Use `load_stats_dir`.
  2. Raw `z3 -st` output files: each file holds one invocation's stdout
     (a result line followed by the stats S-expression). Use
     `load_stats_files` with explicit (label, path) pairs.

Both modes return a `StatsComparison` rendered side-by-side with mean
values per stat across the inputs grouped under each config/label.
"""

import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.table import Table

from lemur.z3_stats import parse_z3_run

_FNAME_RE = re.compile(r"^(?P<config>.+)_s(?P<seed>\d+)\.stats\.json$")


@dataclass
class StatsComparison:
    configs: list[str]  # ordered, as discovered
    # stat_key -> config -> list of values across seeds
    values: dict[str, dict[str, list[float]]]
    seed_counts: dict[str, int]  # config -> number of seeds contributing
    # config -> per-input result strings ("sat" / "unsat" / "unknown"). Empty
    # for the dir loader (sweep already split result out into the row's
    # status field, not the .stats.json blob).
    results: dict[str, list[str]] = field(default_factory=dict)


def load_stats_dir(path: str) -> StatsComparison:
    """Load all `<config>_s<seed>.stats.json` files under `path`."""
    p = Path(path)
    if not p.is_dir():
        raise ValueError(f"{path}: not a directory")

    values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    seeds_by_config: dict[str, set[int]] = defaultdict(set)
    config_order: list[str] = []

    for f in sorted(p.glob("*.stats.json")):
        m = _FNAME_RE.match(f.name)
        if not m:
            continue
        config = m.group("config")
        seed = int(m.group("seed"))
        if config not in config_order:
            config_order.append(config)
        try:
            stats = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(stats, dict):
            continue
        seeds_by_config[config].add(seed)
        for key, val in stats.items():
            if isinstance(val, (int, float)):
                values[key][config].append(float(val))

    return StatsComparison(
        configs=config_order,
        values=dict(values),
        seed_counts={c: len(s) for c, s in seeds_by_config.items()},
    )


def load_stats_files(specs: list[tuple[str, str]]) -> StatsComparison:
    """Load raw `z3 -st` output files; one StatsComparison covering all of them.

    `specs` is a list of (label, path) pairs. Multiple files under the same
    label are treated as multiple seeds for that label (means are taken).
    Files that don't contain a parseable stats block are skipped with a
    warning on stderr; their result lines are still recorded under the label
    if found.
    """
    values: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    seed_counts: dict[str, int] = defaultdict(int)
    results: dict[str, list[str]] = defaultdict(list)
    config_order: list[str] = []

    for label, path in specs:
        try:
            text = Path(path).read_text(errors="replace")
        except OSError as e:
            print(f"Warning: skipping {path}: {e}", file=sys.stderr)
            continue
        result, stats = parse_z3_run(text)
        if stats is None and result is None:
            print(f"Warning: no z3 -st content in {path}", file=sys.stderr)
            continue
        if label not in config_order:
            config_order.append(label)
        if result is not None:
            results[label].append(result)
        if stats is not None:
            seed_counts[label] += 1
            for key, val in stats.items():
                if isinstance(val, (int, float)):
                    values[key][label].append(float(val))

    return StatsComparison(
        configs=config_order,
        values=dict(values),
        seed_counts=dict(seed_counts),
        results=dict(results),
    )


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    if v == int(v) and abs(v) < 1e12:
        return f"{int(v)}"
    return f"{v:.3f}"


def _summarize_results(results: list[str]) -> str:
    """Compact a list of per-seed result tokens for display.

    `["unsat"]`        -> `"unsat"`
    `["sat", "sat"]`   -> `"sat"`
    `["sat", "unsat"]` -> `"sat(1) unsat(1)"`
    `[]`               -> `"—"`
    """
    if not results:
        return "—"
    if len(set(results)) == 1:
        return results[0]
    counts = Counter(results)
    return " ".join(f"{r}({n})" for r, n in counts.most_common())


def render_rich(cmp: StatsComparison, console: Console, top: int | None = None) -> None:
    if not cmp.configs:
        console.print("[yellow]No .stats.json files found[/yellow]")
        return

    title = "z3 stats comparison (mean per config)"
    table = Table(title=title, pad_edge=True)
    table.add_column("stat", style="bold", no_wrap=True)
    for c in cmp.configs:
        n = cmp.seed_counts.get(c, 0)
        table.add_column(f"{c}\n(n={n})", justify="right")
    if len(cmp.configs) == 2:
        table.add_column("diff", justify="right")

    if cmp.results:
        res_row = ["result"] + [
            _summarize_results(cmp.results.get(c, [])) for c in cmp.configs
        ]
        if len(cmp.configs) == 2:
            res_row.append("")
        table.add_row(*res_row, style="bold")

    # Sort keys by max absolute mean descending (most-variable stats first);
    # falls back to key name as a tiebreak for stability.
    def key_sort(k: str) -> tuple[float, str]:
        means = [_mean(cmp.values[k].get(c, [])) or 0.0 for c in cmp.configs]
        return (-max(abs(m) for m in means) if means else 0.0, k)

    keys = sorted(cmp.values.keys(), key=key_sort)
    if top is not None:
        keys = keys[:top]

    for k in keys:
        row = [k]
        means = []
        for c in cmp.configs:
            m = _mean(cmp.values[k].get(c, []))
            means.append(m)
            row.append(_fmt(m))
        if len(cmp.configs) == 2:
            a, b = means
            if a is None or b is None or a == 0:
                row.append("—")
            else:
                pct = (b - a) / abs(a) * 100
                sign = "+" if pct >= 0 else ""
                row.append(f"{sign}{pct:.1f}%")
        table.add_row(*row)

    console.print(table)


def to_csv(cmp: StatsComparison, top: int | None = None) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    header = ["stat"] + cmp.configs
    if len(cmp.configs) == 2:
        header.append("diff_pct")
    w.writerow(header)

    if cmp.results:
        res_row = ["result"] + [
            _summarize_results(cmp.results.get(c, [])) for c in cmp.configs
        ]
        if len(cmp.configs) == 2:
            res_row.append("")
        w.writerow(res_row)

    keys = sorted(cmp.values.keys())
    if top is not None:
        keys = keys[:top]
    for k in keys:
        row = [k]
        means = []
        for c in cmp.configs:
            m = _mean(cmp.values[k].get(c, []))
            means.append(m)
            row.append(
                ""
                if m is None
                else (f"{int(m)}" if m == int(m) and abs(m) < 1e12 else f"{m:.3f}")
            )
        if len(cmp.configs) == 2:
            a, b = means
            if a is None or b is None or a == 0:
                row.append("")
            else:
                row.append(f"{(b - a) / abs(a) * 100:.1f}")
        w.writerow(row)
    return buf.getvalue()


def to_json(cmp: StatsComparison, top: int | None = None) -> str:
    keys = sorted(cmp.values.keys())
    if top is not None:
        keys = keys[:top]
    out: dict = {
        "configs": cmp.configs,
        "seed_counts": cmp.seed_counts,
        "stats": {
            k: {c: _mean(cmp.values[k].get(c, [])) for c in cmp.configs} for k in keys
        },
    }
    if cmp.results:
        out["results"] = {c: cmp.results.get(c, []) for c in cmp.configs}
    return json.dumps(out, indent=2)
