"""
Aggregate sweep results into a per-config tally.

Counts by status (sat, unsat, timeout, unknown, error) plus the fastest
sat / unsat time per config (with the seed that produced it).
"""

import csv
import io
import json
from dataclasses import dataclass, field

from rich.console import Console
from rich.table import Table


@dataclass
class TallyRow:
    config: str
    split: str | None = None
    total: int = 0
    sat: int = 0
    unsat: int = 0
    timeout: int = 0
    unknown: int = 0
    error: int = 0
    # (time_s, seed) of fastest successful run per status, or None
    fastest_sat: tuple[float, int] | None = None
    fastest_unsat: tuple[float, int] | None = None


@dataclass
class Tally:
    rows: list[TallyRow] = field(default_factory=list)
    has_splits: bool = False


def _result_fields(r):
    if hasattr(r, "config"):
        return (r.config, r.seed, r.status, r.time_s, getattr(r, "split", None))
    if isinstance(r, dict):
        return (r["config"], r["seed"], r["status"], r["time_s"], r.get("split"))
    # legacy tuple
    if len(r) >= 5:
        return r[0], r[1], r[2], r[3], r[4]
    return r[0], r[1], r[2], r[3], None


def compute_tally(results) -> Tally:
    """Build a Tally from results (RunResult objects, dicts, or tuples).

    If any result carries a `split`, rows are grouped by (split, config).
    Otherwise rows are grouped by config alone.
    """
    rows: list = []  # list of TallyRow, ordered by first-seen
    by_key: dict[tuple, TallyRow] = {}
    has_splits = False
    for r in results:
        config, seed, status, time_s, split = _result_fields(r)
        if split is not None:
            has_splits = True
        key = (split, config)
        row = by_key.get(key)
        if row is None:
            row = TallyRow(config=config, split=split)
            by_key[key] = row
            rows.append(row)
        row.total += 1
        if status == "sat":
            row.sat += 1
            if row.fastest_sat is None or time_s < row.fastest_sat[0]:
                row.fastest_sat = (float(time_s), int(seed))
        elif status == "unsat":
            row.unsat += 1
            if row.fastest_unsat is None or time_s < row.fastest_unsat[0]:
                row.fastest_unsat = (float(time_s), int(seed))
        elif status == "timeout":
            row.timeout += 1
        elif status == "unknown":
            row.unknown += 1
        else:
            row.error += 1
    return Tally(rows=rows, has_splits=has_splits)


def _fmt_fastest(val: tuple[float, int] | None) -> str:
    if val is None:
        return "n/a"
    time_s, seed = val
    return f"{time_s:.3f}s (seed {seed})"


def render_rich(tally: Tally, console: Console) -> None:
    table = Table(title="Tally", pad_edge=True)
    if tally.has_splits:
        table.add_column("split", style="bold magenta", no_wrap=True)
    table.add_column("config", style="bold", no_wrap=True)
    table.add_column("total", justify="right")
    table.add_column("sat", justify="right", style="green")
    table.add_column("unsat", justify="right", style="cyan")
    table.add_column("to", justify="right", style="red")
    table.add_column("unknown", justify="right", style="yellow")
    table.add_column("err", justify="right")
    table.add_column("fastest-sat", justify="right")
    table.add_column("fastest-unsat", justify="right")
    for row in tally.rows:
        cells = []
        if tally.has_splits:
            cells.append(row.split or "")
        cells.extend(
            [
                row.config,
                str(row.total),
                str(row.sat),
                str(row.unsat),
                str(row.timeout),
                str(row.unknown),
                str(row.error),
                _fmt_fastest(row.fastest_sat),
                _fmt_fastest(row.fastest_unsat),
            ]
        )
        table.add_row(*cells)
    console.print(table)

    # Per-split closure summary
    if tally.has_splits:
        _render_split_summary(tally, console)


def _render_split_summary(tally: Tally, console: Console) -> None:
    by_split: dict[str, dict] = {}
    for row in tally.rows:
        s = by_split.setdefault(row.split or "", {"sat": 0, "unsat": 0})
        s["sat"] += row.sat
        s["unsat"] += row.unsat
    all_closed = True
    summary = Table(title="Splits", pad_edge=True)
    summary.add_column("split", style="bold magenta")
    summary.add_column("sat", justify="right", style="green")
    summary.add_column("unsat", justify="right", style="cyan")
    summary.add_column("closed?")
    for split, counts in by_split.items():
        closed = counts["sat"] + counts["unsat"] > 0
        all_closed = all_closed and closed
        summary.add_row(
            split,
            str(counts["sat"]),
            str(counts["unsat"]),
            "[green]yes[/green]" if closed else "[red]no[/red]",
        )
    console.print(summary)
    console.print(
        "[bold]disjunction of splits: "
        + (
            "[green]all closed[/green][/bold]"
            if all_closed
            else "[red]incomplete[/red][/bold]"
        )
    )


def to_csv(tally: Tally) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    header = []
    if tally.has_splits:
        header.append("split")
    header.extend(
        [
            "config",
            "total",
            "sat",
            "unsat",
            "timeout",
            "unknown",
            "error",
            "fastest_sat_time_s",
            "fastest_sat_seed",
            "fastest_unsat_time_s",
            "fastest_unsat_seed",
        ]
    )
    w.writerow(header)
    for row in tally.rows:
        fs = row.fastest_sat
        fu = row.fastest_unsat
        rec = []
        if tally.has_splits:
            rec.append(row.split or "")
        rec.extend(
            [
                row.config,
                row.total,
                row.sat,
                row.unsat,
                row.timeout,
                row.unknown,
                row.error,
                f"{fs[0]:.3f}" if fs else "",
                fs[1] if fs else "",
                f"{fu[0]:.3f}" if fu else "",
                fu[1] if fu else "",
            ]
        )
        w.writerow(rec)
    return buf.getvalue()


def to_json(tally: Tally) -> str:
    data = []
    for row in tally.rows:
        fs = row.fastest_sat
        fu = row.fastest_unsat
        d = {
            "config": row.config,
            "total": row.total,
            "sat": row.sat,
            "unsat": row.unsat,
            "timeout": row.timeout,
            "unknown": row.unknown,
            "error": row.error,
            "fastest_sat": {"time_s": round(fs[0], 3), "seed": fs[1]} if fs else None,
            "fastest_unsat": {"time_s": round(fu[0], 3), "seed": fu[1]} if fu else None,
        }
        if tally.has_splits:
            d["split"] = row.split
        data.append(d)
    return json.dumps(data, indent=2)


def read_sweep_csv(path: str) -> list[dict]:
    """Read a sweep CSV (columns: config,seed,status,time_s, optional split)."""
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        required = {"config", "seed", "status", "time_s"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"{path}: missing columns {sorted(missing)}; "
                f"expected sweep CSV with columns {sorted(required)}"
            )
        has_split = "split" in (reader.fieldnames or [])
        for r in reader:
            row = {
                "config": r["config"],
                "seed": int(r["seed"]),
                "status": r["status"],
                "time_s": float(r["time_s"]),
            }
            if has_split and r.get("split"):
                row["split"] = r["split"]
            rows.append(row)
    return rows
