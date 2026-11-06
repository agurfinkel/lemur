"""
Side-by-side diff of `arith_conflict` summaries between two traces.

Wraps the existing `analyze_arith_conflict` analyzer twice and emits
delta rows per subsection (summary, hot blocks, top constants, premise
shapes). The motivation: comparing baseline vs variant encodings on a
stuck QF_NIA target — "did the rewrite shift conflict mass off
BLK__65?" should be a single command, not a manual eyeball of two
tables.

CLI: `lemur stats-diff TRACE_A TRACE_B [--top-k N]`. Sibling subcommand
to `lemur nla-diff`; same A/B/delta column layout.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from lemur.parsers import group_by_tag, parse_trace
from lemur.stats import (
    _BIG_INT_RE,
    _BLOCK_RE,
    _PREMISE_END_RE,
    _classify_premise_shape,
    _format_constant,
)


@dataclass
class DiffRow:
    key: str
    a: str
    b: str
    delta: str


@dataclass
class DiffSubsection:
    label: str
    rows: list[DiffRow]


@dataclass
class ArithConflictDiff:
    a_path: str
    b_path: str
    a_total: int  # total arith_conflict entries in A
    b_total: int  # total arith_conflict entries in B
    subsections: list[DiffSubsection]


def _aggregate_arith_conflict(entries):
    """Single-pass aggregation of one trace's [arith_conflict] entries.

    Returns a dict shaped for diffing: per-conflict block counts,
    per-conflict constant counts, premise-shape counts, total premise
    rows, and total conflict count.
    """
    block_counts: Counter = Counter()
    const_counts: Counter = Counter()
    shape_counts: Counter = Counter()
    total_premise_rows = 0
    for e in entries:
        body = e.body
        block_counts.update(set(_BLOCK_RE.findall(body)))
        const_counts.update(set(_BIG_INT_RE.findall(body)))
        for line in body.splitlines():
            if _PREMISE_END_RE.search(line):
                shape_counts[_classify_premise_shape(line)] += 1
                total_premise_rows += 1
    return {
        "count": len(entries),
        "blocks": block_counts,
        "consts": const_counts,
        "shapes": shape_counts,
        "premise_rows": total_premise_rows,
    }


def _fmt_count_delta(a: int, b: int) -> str:
    if a == b:
        return "="
    d = b - a
    sign = "+" if d > 0 else ""
    if a > 0:
        pct = d / a * 100
        return f"{sign}{d} ({sign}{pct:.0f}%)"
    return f"{sign}{d}"


def _fmt_pct(num: int, den: int) -> str:
    if den == 0:
        return "0.0%"
    return f"{100 * num / den:.1f}%"


def _fmt_pp_delta(a_num: int, a_den: int, b_num: int, b_den: int) -> str:
    a_pct = (100 * a_num / a_den) if a_den else 0.0
    b_pct = (100 * b_num / b_den) if b_den else 0.0
    d = b_pct - a_pct
    if abs(d) < 0.05:
        return "="
    sign = "+" if d > 0 else ""
    return f"{sign}{d:.1f}pp"


def _diff_summary(a, b) -> DiffSubsection:
    rows = [
        DiffRow(
            "conflicts",
            str(a["count"]),
            str(b["count"]),
            _fmt_count_delta(a["count"], b["count"]),
        ),
        DiffRow(
            "distinct blocks",
            str(len(a["blocks"])),
            str(len(b["blocks"])),
            _fmt_count_delta(len(a["blocks"]), len(b["blocks"])),
        ),
        DiffRow(
            "distinct big-int constants",
            str(len(a["consts"])),
            str(len(b["consts"])),
            _fmt_count_delta(len(a["consts"]), len(b["consts"])),
        ),
        DiffRow(
            "premise rows",
            str(a["premise_rows"]),
            str(b["premise_rows"]),
            _fmt_count_delta(a["premise_rows"], b["premise_rows"]),
        ),
    ]
    return DiffSubsection("summary", rows)


def _diff_top_counter(
    a_ctr: Counter,
    b_ctr: Counter,
    *,
    label: str,
    top_k: int,
    a_total: int,
    b_total: int,
    key_format=lambda k: k,
) -> DiffSubsection:
    """Generic top-K counter diff used for hot blocks and top constants.

    Surfaces up to `top_k` keys taken from the union of A's and B's
    top-K, ranked by max(A_count, B_count), then key for stable
    tiebreak. Always shows the count plus `(P%)` of A's / B's totals.
    """
    a_top_keys = {k for k, _ in a_ctr.most_common(top_k)}
    b_top_keys = {k for k, _ in b_ctr.most_common(top_k)}
    union = a_top_keys | b_top_keys
    candidates = sorted(
        ((k, a_ctr.get(k, 0), b_ctr.get(k, 0)) for k in union),
        key=lambda t: (-max(t[1], t[2]), t[0]),
    )
    rows: list[DiffRow] = []
    for key, a_n, b_n in candidates[:top_k]:
        a_disp = f"{a_n} ({_fmt_pct(a_n, a_total)})"
        b_disp = f"{b_n} ({_fmt_pct(b_n, b_total)})"
        rows.append(
            DiffRow(key_format(key), a_disp, b_disp, _fmt_count_delta(a_n, b_n))
        )
    if not rows:
        rows.append(DiffRow("(none)", "0", "0", "="))
    return DiffSubsection(label, rows)


def _diff_premise_shapes(a, b) -> DiffSubsection:
    shape_order = ["clean_linear", "ite_wrapped", "mod_div_wrapped", "mixed"]
    a_total = a["premise_rows"]
    b_total = b["premise_rows"]
    rows: list[DiffRow] = []
    for s in shape_order:
        a_n = a["shapes"].get(s, 0)
        b_n = b["shapes"].get(s, 0)
        a_disp = f"{a_n} ({_fmt_pct(a_n, a_total)})"
        b_disp = f"{b_n} ({_fmt_pct(b_n, b_total)})"
        rows.append(
            DiffRow(s, a_disp, b_disp, _fmt_pp_delta(a_n, a_total, b_n, b_total))
        )
    return DiffSubsection("premise shapes (% of premise rows)", rows)


def diff_arith_conflict(
    a_path: str, b_path: str, *, top_k: int = 5
) -> ArithConflictDiff:
    """Build the full delta tree from two trace paths.

    Raises ValueError if either trace has no [arith_conflict] entries
    (the diff is meaningless without them on both sides).
    """
    a_entries = group_by_tag(list(parse_trace(a_path))).get("arith_conflict", [])
    b_entries = group_by_tag(list(parse_trace(b_path))).get("arith_conflict", [])
    if not a_entries:
        raise ValueError(f"no [arith_conflict] entries in {a_path}")
    if not b_entries:
        raise ValueError(f"no [arith_conflict] entries in {b_path}")

    a_agg = _aggregate_arith_conflict(a_entries)
    b_agg = _aggregate_arith_conflict(b_entries)

    summary = _diff_summary(a_agg, b_agg)
    hot_blocks = _diff_top_counter(
        a_agg["blocks"],
        b_agg["blocks"],
        label=f"hot blocks (top {top_k} by max(A,B) conflicts containing block)",
        top_k=top_k,
        a_total=a_agg["count"],
        b_total=b_agg["count"],
    )
    top_consts = _diff_top_counter(
        a_agg["consts"],
        b_agg["consts"],
        label=f"top constants (top {top_k})",
        top_k=top_k,
        a_total=a_agg["count"],
        b_total=b_agg["count"],
        key_format=_format_constant,
    )
    shapes = _diff_premise_shapes(a_agg, b_agg)

    return ArithConflictDiff(
        a_path=a_path,
        b_path=b_path,
        a_total=a_agg["count"],
        b_total=b_agg["count"],
        subsections=[summary, hot_blocks, top_consts, shapes],
    )


# --- Rendering --------------------------------------------------------------


def render_plain(d: ArithConflictDiff) -> str:
    lines = [
        f"# A: {d.a_path}",
        f"# B: {d.b_path}",
        f"# total arith_conflict entries: A={d.a_total} B={d.b_total}",
    ]
    # Compute column widths once across all rows so the table stays aligned
    # across subsections.
    all_rows = [r for sub in d.subsections for r in sub.rows]
    if not all_rows:
        return "\n".join(lines) + "\n(no data)\n"
    key_w = max(len("metric"), max(len(r.key) for r in all_rows))
    a_w = max(len("A"), max(len(r.a) for r in all_rows))
    b_w = max(len("B"), max(len(r.b) for r in all_rows))
    d_w = max(len("delta"), max(len(r.delta) for r in all_rows))
    for sub in d.subsections:
        lines.append("")
        lines.append(f"## {sub.label}")
        lines.append(
            f"{'metric':<{key_w}}  {'A':>{a_w}}  {'B':>{b_w}}  {'delta':>{d_w}}"
        )
        lines.append(f"{'-' * key_w}  {'-' * a_w}  {'-' * b_w}  {'-' * d_w}")
        for r in sub.rows:
            lines.append(
                f"{r.key:<{key_w}}  {r.a:>{a_w}}  {r.b:>{b_w}}  {r.delta:>{d_w}}"
            )
    return "\n".join(lines) + "\n"


def render_rich(d: ArithConflictDiff, console) -> None:
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    header = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    header.add_column("Key", style="bold")
    header.add_column("Value")
    header.add_row("A", d.a_path)
    header.add_row("B", d.b_path)
    header.add_row("arith_conflict entries", f"A={d.a_total}  B={d.b_total}")
    console.print(Panel(header, title=Text("stats-diff", style="bold"), expand=False))

    for sub in d.subsections:
        t = Table(title=sub.label, pad_edge=True)
        t.add_column("metric", style="bold", no_wrap=True)
        t.add_column("A", justify="right")
        t.add_column("B", justify="right")
        t.add_column("delta", justify="right")
        for r in sub.rows:
            t.add_row(r.key, r.a, r.b, r.delta)
        console.print(t)


def render_json(d: ArithConflictDiff) -> str:
    return json.dumps(
        {
            "a": str(Path(d.a_path).resolve()),
            "b": str(Path(d.b_path).resolve()),
            "a_total": d.a_total,
            "b_total": d.b_total,
            "subsections": [
                {
                    "label": sub.label,
                    "rows": [
                        {"key": r.key, "a": r.a, "b": r.b, "delta": r.delta}
                        for r in sub.rows
                    ],
                }
                for sub in d.subsections
            ],
        },
        indent=2,
    )
