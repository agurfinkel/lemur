"""
Parsers for `-tr:nra` (z3 nra_solver) trace blocks.

The `[nra]` tag emits three block shapes per nlsat invocation:

  1. `[nra] check` — body has `|- <constraint>` lines, then `assignment:`,
     then optionally a witness. This is the **constraint pool** in z3's
     stable x-form notation (`x0`, `x1`, ...). Used as a fingerprint for
     "did nlsat get re-asked the same question?"

  2. `[nra] check` — body is one line: `nra result l_<true|false|undef>`.
     The verdict for the preceding pool.

  3. `[nra] check` — body has `(N) j... ==> false` rows: j-form
     unsat-core / sat witness. Not used here.

We disambiguate the three by body shape, not by source-line number, so
the parser stays robust as z3 source line numbers drift.

Why x-form: j-IDs in `lemur nla` postprocessing get renumbered across
nlsat invocations, inflating apparent repeat counts by 10-100x. x-form
variables are stable within a z3 process, so the constraint set itself
becomes a usable fingerprint. See `lemur-nla-batch-helpers.md` (Helper B).
"""

from __future__ import annotations

import hashlib
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from lemur.parsers import TraceEntry, parse_trace

_CONSTRAINT_LINE_RE = re.compile(r"^\s*\|-\s+(.+?)\s*$")
_NRA_RESULT_RE = re.compile(r"nra result (l_\w+)")
_X_VAR_RE = re.compile(r"\bx\d+\b")


@dataclass
class NraCall:
    """One nlsat invocation as recovered from `[nra]` trace blocks.

    `constraints` is the sorted, normalized set used for fingerprinting.
    `raw_constraints` preserves the trace order for display. `variables`
    is the union of x-vars across all constraints, sorted numerically.
    """

    index: int
    constraints: tuple[str, ...]
    raw_constraints: tuple[str, ...]
    variables: tuple[str, ...]
    result: str | None
    fingerprint: str
    line_number: int  # source-line in trace where the constraint pool starts

    @property
    def size(self) -> int:
        return len(self.constraints)


def _normalize_constraint(s: str) -> str:
    """Pull off a single layer of `!(...)` negation. Whitespace-trim."""
    s = s.strip()
    if s.startswith("!(") and s.endswith(")"):
        s = "not " + s[2:-1]
    return s


def _fingerprint(constraints: list[str]) -> str:
    h = hashlib.sha1()
    for c in sorted(constraints):
        h.update(c.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:12]


def _is_constraint_pool(body: str) -> bool:
    return any(line.lstrip().startswith("|- ") for line in body.splitlines())


def parse_nra_calls(
    trace_path: str | Path,
    *,
    coarse: bool = False,
) -> list[NraCall]:
    """Walk `[nra]` entries in trace order, pair constraint pools with
    their result lines, return one NraCall per nlsat invocation.

    A pool without a following result keeps `result=None`. A result
    without a preceding pool is dropped (no anchor to attach to).

    `coarse=True` applies the structural-shape transform (see
    `lemur.lemma_xform._coarse_signature`) before hashing, so the
    [nra] x-form path can also bucket "same shape, drifting threshold"
    repeats into one fingerprint.
    """
    entries = [e for e in parse_trace(trace_path) if e.tag == "nra"]
    return _calls_from_entries(entries, coarse=coarse)


def _calls_from_entries(
    entries: list[TraceEntry],
    *,
    coarse: bool = False,
) -> list[NraCall]:
    # Local import keeps lemma_xform → nra_parsers dependency one-way at
    # module load time; it only matters when coarse mode is requested.
    if coarse:
        from lemur.lemma_xform import _coarse_signature
    calls: list[NraCall] = []
    pending: NraCall | None = None

    for e in entries:
        if e.function != "check":
            continue
        body = e.body

        if _is_constraint_pool(body):
            raw: list[str] = []
            normalized: list[str] = []
            vars_set: set[str] = set()
            for line in body.splitlines():
                m = _CONSTRAINT_LINE_RE.match(line)
                if not m:
                    continue
                c_raw = m.group(1)
                c_norm = _normalize_constraint(c_raw)
                raw.append(c_raw)
                normalized.append(c_norm)
                vars_set.update(_X_VAR_RE.findall(c_norm))
            sig_lines = (
                [_coarse_signature(s) for s in normalized] if coarse else normalized
            )
            pending = NraCall(
                index=len(calls),
                constraints=tuple(sorted(sig_lines)),
                raw_constraints=tuple(raw),
                variables=tuple(sorted(vars_set, key=lambda v: int(v[1:]))),
                result=None,
                fingerprint=_fingerprint(sig_lines),
                line_number=e.line_number,
            )
            calls.append(pending)
            continue

        m = _NRA_RESULT_RE.search(body)
        if m and pending is not None:
            pending.result = m.group(1)
            pending = None

    return calls


# --- Report -----------------------------------------------------------------


@dataclass
class XFormReport:
    total: int
    unique_fingerprints: int
    repeats: list[tuple[int, NraCall]]  # (count, representative call)
    size_min: int | None
    size_median: float | None
    size_max: int | None
    result_counts: Counter = field(default_factory=Counter)


def build_xform_report(calls: list[NraCall], *, top: int = 10) -> XFormReport:
    """Aggregate fingerprint stats from a list of NraCall."""
    if not calls:
        return XFormReport(
            total=0,
            unique_fingerprints=0,
            repeats=[],
            size_min=None,
            size_median=None,
            size_max=None,
        )

    fp_counts: Counter[str] = Counter(c.fingerprint for c in calls)
    fp_to_call: dict[str, NraCall] = {}
    for c in calls:
        fp_to_call.setdefault(c.fingerprint, c)

    sizes = [c.size for c in calls]
    result_counts: Counter[str] = Counter(c.result or "<none>" for c in calls)

    # `repeats`: every fingerprint with count >= 2, ranked by count desc.
    repeats: list[tuple[int, NraCall]] = []
    for fp, n in fp_counts.most_common():
        if n < 2:
            break
        repeats.append((n, fp_to_call[fp]))
    repeats = repeats[:top]

    return XFormReport(
        total=len(calls),
        unique_fingerprints=len(fp_counts),
        repeats=repeats,
        size_min=min(sizes),
        size_median=statistics.median(sizes),
        size_max=max(sizes),
        result_counts=result_counts,
    )


def _format_vars(vars_: tuple[str, ...], cap: int = 8) -> str:
    if len(vars_) <= cap:
        return "[" + ",".join(vars_) + "]"
    return "[" + ",".join(vars_[:cap]) + f",...] (+{len(vars_) - cap})"


def render_xform_plain(
    report: XFormReport,
    *,
    unit_label: str = "nlsat calls",
    show: bool = False,
) -> str:
    if report.total == 0:
        return f"(no {unit_label} found in trace)\n"

    pct_unique = 100.0 * report.unique_fingerprints / report.total
    label_w = max(len(unit_label) + 1, len("unique fingerprints:"))
    lines = [
        f"{unit_label + ':':<{label_w}}  {report.total}",
        f"{'unique fingerprints:':<{label_w}}  {report.unique_fingerprints}  ({pct_unique:.1f}%)",
    ]

    # Suppress the results row when there's only a synthetic '<none>' entry —
    # the varmap path can't observe nlsat verdicts.
    real_results = {r: n for r, n in report.result_counts.items() if r != "<none>"}
    if real_results:
        rc = " ".join(
            f"{r}={n}" for r, n in sorted(real_results.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"{'results:':<{label_w}}  {rc}")

    if report.repeats:
        lines.append("")
        lines.append("top repeats:")
        for count, call in report.repeats:
            v = _format_vars(call.variables)
            lines.append(
                f"  count={count}  size={call.size}  vars={v}  fp={call.fingerprint}"
            )
            if show:
                # `constraints` is sorted; in coarse mode it's the structural
                # shape (LIT / #A0 tokens), in fine mode the raw resolved
                # signature. Either is the right "what is this fingerprint?"
                # answer.
                for c in call.constraints:
                    lines.append(f"      {c}")
                lines.append("")
    else:
        lines.append("")
        lines.append(
            "top repeats: (none — every nlsat call had a unique constraint set)"
        )

    if report.size_min is not None:
        lines.append("")
        lines.append("session-size distribution:")
        lines.append(
            f"  min={report.size_min}  median={report.size_median:g}  "
            f"max={report.size_max}"
        )

    return "\n".join(lines) + "\n"


def render_xform_rich(
    report: XFormReport, console, *, unit_label: str = "nlsat calls", show: bool = False
) -> None:
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    if report.total == 0:
        console.print(f"[yellow](no {unit_label} found in trace)[/yellow]")
        return

    pct_unique = 100.0 * report.unique_fingerprints / report.total
    overview = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    overview.add_column("Key", style="bold")
    overview.add_column("Value")
    overview.add_row(unit_label, str(report.total))
    overview.add_row(
        "unique fingerprints", f"{report.unique_fingerprints}  ({pct_unique:.1f}%)"
    )
    real_results = {r: n for r, n in report.result_counts.items() if r != "<none>"}
    if real_results:
        rc = " ".join(
            f"{r}={n}" for r, n in sorted(real_results.items(), key=lambda kv: -kv[1])
        )
        overview.add_row("results", rc)
    if report.size_min is not None:
        overview.add_row(
            "constraint-set size",
            f"min={report.size_min}  "
            f"median={report.size_median:g}  "
            f"max={report.size_max}",
        )
    console.print(
        Panel(overview, title=Text("nla --x-form", style="bold"), expand=False)
    )

    if report.repeats:
        t = Table(title="top repeats", pad_edge=True)
        t.add_column("count", justify="right", style="bold")
        t.add_column("size", justify="right")
        t.add_column("nvars", justify="right")
        t.add_column("vars (head)", overflow="fold")
        t.add_column("fp", style="dim")
        for count, call in report.repeats:
            head = ",".join(call.variables[:6])
            if len(call.variables) > 6:
                head += ",..."
            t.add_row(
                str(count),
                str(call.size),
                str(len(call.variables)),
                head,
                call.fingerprint,
            )
        console.print(t)
        if show:
            # The table summarizes; the per-fingerprint constraint dump
            # below it answers "what shape does this fingerprint actually
            # encode?". Indented and dimmed-style headers separate the
            # representatives visually from the next section.
            for count, call in report.repeats:
                console.print(
                    f"[bold]fp={call.fingerprint}[/bold]  "
                    f"[dim](count={count}, size={call.size})[/dim]"
                )
                for c in call.constraints:
                    console.print(f"    {c}", soft_wrap=True)
    else:
        console.print(
            "[dim]top repeats: every nlsat call had a unique constraint set[/dim]"
        )


def render_xform_json(report: XFormReport, *, show: bool = False) -> str:
    import json

    return json.dumps(_xform_to_jsonable(report, show=show), indent=2)


def _xform_to_jsonable(report: XFormReport, *, show: bool = False) -> dict:
    repeats = []
    for count, call in report.repeats:
        entry = {
            "count": count,
            "size": call.size,
            "variables": list(call.variables),
            "fingerprint": call.fingerprint,
        }
        if show:
            entry["constraints"] = list(call.constraints)
        repeats.append(entry)
    return {
        "nlsat_calls": report.total,
        "unique_fingerprints": report.unique_fingerprints,
        "results": dict(report.result_counts),
        "size_min": report.size_min,
        "size_median": report.size_median,
        "size_max": report.size_max,
        "top_repeats": repeats,
    }
