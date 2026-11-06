"""
Statistics computation from parsed trace entries.

Computes per-tag and per-function summaries from trace data.
General-purpose — no lemma analysis (that lives in lemur nla).
"""

import re
from collections import Counter
from pathlib import Path

from lemur.parsers import TraceEntry, group_by_function, group_by_tag, parse_trace
from lemur.table import StatsOutput


def analyze_nla_solver(entries: list[TraceEntry]) -> list[tuple[str, str]]:
    """Analyze nla_solver trace entries — counts and function frequency only."""
    rows = []
    by_func = group_by_function(entries)

    rows.append(("Total entries", str(len(entries))))
    rows.append(("Unique functions", str(len(by_func))))

    # Check calls
    if "check" in by_func:
        check_entries = by_func["check"]
        call_nums = []
        for e in check_entries:
            m = re.search(r"calls\s*=\s*(\d+)", e.body)
            if m:
                call_nums.append(int(m.group(1)))
        if call_nums:
            rows.append(
                (
                    "Check calls",
                    f"{len(check_entries)} entries, max call# = {max(call_nums)}",
                )
            )

    # Lemma count (just the count, detail is in `lemur nla`)
    if "~lemma_builder" in by_func:
        rows.append(("Lemmas", str(len(by_func["~lemma_builder"]))))

    # init_to_refine
    if "init_to_refine" in by_func:
        mon_counts = []
        for e in by_func["init_to_refine"]:
            m = re.search(r"(\d+)\s+mons?\s+to\s+refine", e.body)
            if m:
                mon_counts.append(int(m.group(1)))
        if mon_counts:
            mn, mx = min(mon_counts), max(mon_counts)
            avg = sum(mon_counts) / len(mon_counts)
            rows.append(
                (
                    "Monomials to refine",
                    f"min={mn} avg={avg:.1f} max={mx} (n={len(mon_counts)})",
                )
            )

    # Top functions
    func_counts = Counter(e.function for e in entries)
    rows.append(("", ""))
    rows.append(("Top functions", ""))
    for func, cnt in func_counts.most_common(10):
        pct = 100 * cnt / len(entries)
        rows.append((f"  {func}", f"{cnt} ({pct:.1f}%)"))

    return rows


def analyze_nra(entries: list[TraceEntry]) -> list[tuple[str, str]]:
    """Analyze nra trace entries."""
    rows = []
    by_func = group_by_function(entries)

    rows.append(("Total entries", str(len(entries))))

    if "check" in by_func:
        check_entries = by_func["check"]
        results = Counter()
        for e in check_entries:
            m = re.search(r"nra result (\S+)", e.body)
            if m:
                results[m.group(1)] += 1
        rows.append(("NRA checks", str(len(check_entries))))
        for res, cnt in results.most_common():
            pct = 100 * cnt / len(check_entries)
            rows.append((f"  {res}", f"{cnt} ({pct:.1f}%)"))

    if "setup_solver_poly" in by_func:
        rows.append(("Constraints set up", str(len(by_func["setup_solver_poly"]))))

    func_counts = Counter(e.function for e in entries)
    rows.append(("", ""))
    rows.append(("Top functions", ""))
    for func, cnt in func_counts.most_common(10):
        pct = 100 * cnt / len(entries)
        rows.append((f"  {func}", f"{cnt} ({pct:.1f}%)"))

    return rows


_POWERS_OF_2 = {
    str(1 << n): f"2^{n}" for n in (8, 16, 32, 64, 96, 128, 160, 192, 224, 256)
}

_BIG_INT_RE = re.compile(r"\b\d{6,}\b")
_BLOCK_RE = re.compile(r"BLK__\w+")
_PREMISE_END_RE = re.compile(r"\sl_(?:true|false|undef)\s*$")


def _classify_premise_shape(line: str) -> str:
    has_ite = "(if " in line
    has_modish = ("(mod " in line) or ("(div " in line) or ("int_mul_div" in line)
    if has_ite and has_modish:
        return "mixed"
    if has_ite:
        return "ite_wrapped"
    if has_modish:
        return "mod_div_wrapped"
    return "clean_linear"


def _format_constant(value: str) -> str:
    pretty = _POWERS_OF_2.get(value)
    if pretty:
        return f"{pretty} ({len(value)} digits)"
    if len(value) > 18:
        return f"{value[:8]}…{value[-4:]} ({len(value)} digits)"
    return value


def analyze_arith_conflict(
    entries: list[TraceEntry], *, top_k: int = 5
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Summary of `arith_conflict` blocks: hot block-reachability variables,
    top numeric constants, premise-shape histogram.

    Block and constant tallies count conflicts containing X (not raw text
    occurrences), so a block mentioned 3x in one conflict counts once.
    Premise-shape classification only inspects rows ending in `l_true` /
    `l_false` / `l_undef` (the LRA `set_conflict_or_lemma` body shape from
    theory_lra.cpp:3621); other arith_conflict emitters have no such rows
    and the histogram reports `n/a` for those traces.

    Returns subsection-form so build_stats_output emits one StatsOutput
    section per logical group.
    """
    block_counts: Counter = Counter()
    const_counts: Counter = Counter()
    shape_counts: Counter = Counter()
    total_premise_rows = 0

    for entry in entries:
        body = entry.body
        block_counts.update(set(_BLOCK_RE.findall(body)))
        const_counts.update(set(_BIG_INT_RE.findall(body)))
        for line in body.splitlines():
            if _PREMISE_END_RE.search(line):
                shape_counts[_classify_premise_shape(line)] += 1
                total_premise_rows += 1

    n = len(entries)

    def pct(x, total):
        return f"{100 * x / total:.1f}%" if total else "0.0%"

    summary_rows = [
        ("conflicts", str(n)),
        ("distinct blocks", str(len(block_counts))),
        ("distinct big-int constants", str(len(const_counts))),
        ("premise rows", str(total_premise_rows)),
    ]

    if block_counts:
        hot_block_rows = [
            (blk, f"{cnt} ({pct(cnt, n)})")
            for blk, cnt in block_counts.most_common(top_k)
        ]
    else:
        hot_block_rows = [("(none)", "no BLK__ variables in any body")]

    if const_counts:
        top_const_rows = [
            (_format_constant(val), f"{cnt} ({pct(cnt, n)})")
            for val, cnt in const_counts.most_common(top_k)
        ]
    else:
        top_const_rows = [("(none)", "no big-int constants in any body")]

    shape_order = ["clean_linear", "ite_wrapped", "mod_div_wrapped", "mixed"]
    if total_premise_rows:
        shape_rows = [
            (
                s,
                f"{shape_counts.get(s, 0)} ({pct(shape_counts.get(s, 0), total_premise_rows)})",
            )
            for s in shape_order
        ]
    else:
        shape_rows = [("n/a", "no `l_true` premise rows in body")]

    return [
        ("summary", summary_rows),
        (f"hot blocks (top {top_k} by conflicts containing block)", hot_block_rows),
        (f"top constants (top {top_k})", top_const_rows),
        ("premise shapes", shape_rows),
    ]


TAG_ANALYZERS = {
    "nla_solver": analyze_nla_solver,
    "nra": analyze_nra,
    "arith_conflict": analyze_arith_conflict,
}


def analyze_generic(tag: str, entries: list[TraceEntry]) -> list[tuple[str, str]]:
    """Generic analysis for tags without a specific analyzer."""
    rows = []
    rows.append(("Total entries", str(len(entries))))

    func_counts = Counter(e.function for e in entries)
    rows.append(("Unique functions", str(len(func_counts))))

    source_files = Counter(Path(e.source_file).name for e in entries)
    rows.append(
        (
            "Source files",
            ", ".join(f"{f} ({c})" for f, c in source_files.most_common(5)),
        )
    )

    rows.append(("", ""))
    rows.append(("Top functions", ""))
    for func, cnt in func_counts.most_common(10):
        pct = 100 * cnt / len(entries)
        rows.append((f"  {func}", f"{cnt} ({pct:.1f}%)"))

    return rows


def build_stats_output(
    trace_path: str | Path,
    tags: list[str] | None = None,
    functions: list[str] | None = None,
    *,
    top_k: int = 5,
) -> StatsOutput:
    """Parse a trace file and build a StatsOutput with per-tag analysis.

    Analyzers may return either flat key-value rows (the existing contract)
    or subsection-form `list[tuple[str, list[tuple[str, str]]]]`. The latter
    causes one StatsOutput section to be emitted per subsection, with the
    section title `[<tag>] <subsection_label>`.
    """
    entries = list(parse_trace(trace_path))
    by_tag = group_by_tag(entries)

    out = StatsOutput()

    summary_rows = [
        ("Trace file", str(trace_path)),
        ("Total entries", str(len(entries))),
        ("Tags found", ", ".join(f"{t} ({len(es)})" for t, es in by_tag.items())),
    ]
    out.add_section("Summary", summary_rows)

    parameterised = {
        "arith_conflict": lambda es: analyze_arith_conflict(es, top_k=top_k),
    }

    for tag, tag_entries in by_tag.items():
        if tags and tag not in tags:
            continue
        if functions:
            tag_entries = [e for e in tag_entries if e.function in set(functions)]
            if not tag_entries:
                continue

        if tag in parameterised:
            analyzer = parameterised[tag]
        elif tag in TAG_ANALYZERS:
            analyzer = TAG_ANALYZERS[tag]
        else:

            def analyzer(es, _t=tag):
                return analyze_generic(_t, es)

        result = analyzer(tag_entries)

        if result and isinstance(result[0][1], list):
            for sub_label, sub_rows in result:
                out.add_section(f"[{tag}] {sub_label}", sub_rows)
        else:
            out.add_section(f"[{tag}]", result)

    return out
