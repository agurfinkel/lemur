"""
Stable nlsat-call fingerprints from `-tr:nla_solver` alone, via per-lemma
varmap snapshots.

Each `~lemma_builder` block is followed (in trace order) by a single
`[nla_solver] false_case_of_check_nla` block whose body carries a
`varmap:` line. The varmap maps the lemma's j-vars to z3-internal R/I
identifiers (and composite expressions over them) AT THAT LEMMA'S
EMISSION TIME — a per-call snapshot, not a global table.

Resolving the lemma's preconditions + conclusion through that snapshot
yields an R/I-form expression set whose tokens (`R188`, `(* R188 R195)`,
...) are stable across nlsat invocations: R/I IDs are z3 enode IDs,
which are not recycled, unlike LP-column j-IDs.

This path produces the same stability guarantee as the older
`-tr:nra`-based fingerprinter without the 8x trace-size cost. The
[nra] path remains available as a fallback when the user has separately
captured an nra trace.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from lemur.nra_parsers import NraCall
from lemur.parsers import TraceEntry, parse_trace, parse_varmap_line

_J_VAR_RE = re.compile(r"\bj\d+\b")
_R_OR_I_VAR_RE = re.compile(r"\b[RI]\d+\b")

# --- Coarse ("structural") fingerprint normalization ------------------------
#
# Two passes turn a varmap-resolved lemma signature into a structural shape:
#
#   1. Alpha-rename `#NNN` aux Bool/Int IDs by first appearance within the
#      single signature being normalized, so two emissions that disagree
#      only on which Boolean atoms appear in disjunctions hash the same.
#      State is local to one signature — the next signature starts fresh
#      at #A0.
#   2. Collapse standalone integer / rational literals to the token `LIT`,
#      so threshold values that drift with the model snapshot stop
#      driving spurious uniqueness. Negative lookarounds preserve the
#      digit suffixes inside SMT identifiers (R188, I99, j26, x12,
#      CANON123) and the alpha-rename tokens (#A0) we just inserted —
#      those are stable structural identity, not snapshot data.
#
# See ../z3-research/lemur/structural-fingerprint-proposal.md.
_AUX_ID_RE = re.compile(r"#\d+")
_LITERAL_RE = re.compile(
    r"(?<![A-Za-z\d#_!])"  # no letter/digit/#/_/! before
    r"-?\d[\d_]*"  # digit run (with bignum '_' separators)
    r"(?:\s*/\s*-?\d[\d_]*)?"  # optional rational denominator
    r"(?![A-Za-z_\d!])"  # no letter/digit/_/! after
)
# `!` joins z3 enode-occurrence ids (`CANON123!!8`); excluding it keeps
# the trailing `8` glued to the identifier instead of collapsing to LIT.


def _coarse_signature(text: str) -> str:
    """Map a resolved lemma signature to its structural shape.

    Order matters: alpha-rename #NNN before collapsing literals, otherwise
    the digit-collapse pass would erase the very tokens step 1 needs.
    """
    seen: dict[str, str] = {}

    def _rename(m: re.Match) -> str:
        tok = m.group(0)
        if tok not in seen:
            seen[tok] = f"#A{len(seen)}"
        return seen[tok]

    text = _AUX_ID_RE.sub(_rename, text)
    text = _LITERAL_RE.sub("LIT", text)
    return text


def _resolve_jvars(expr: str, varmap: dict[str, str]) -> str:
    """Substitute every `j\\d+` token in `expr` with its varmap value.

    Re-runs the substitution until no j-tokens remain or no more change
    happens (the varmap may chain — j26 -> "(* R188 R195)" — but in
    practice z3 emits a single hop, so this loop usually runs once).
    Tokens not in varmap are left as-is; downstream the fingerprint
    will absorb them, surfacing missing-mapping cases as distinct
    fingerprints rather than crashes.
    """
    prev = None
    cur = expr
    for _ in range(8):  # safety cap on chain depth
        if cur == prev:
            break
        prev = cur
        cur = _J_VAR_RE.sub(lambda m: varmap.get(m.group(0), m.group(0)), cur)
    return cur


def _normalize(expr: str) -> str:
    """Collapse whitespace; deterministic single-line form for hashing."""
    return " ".join(expr.split())


def _fingerprint(items: list[str]) -> str:
    h = hashlib.sha1()
    for line in sorted(items):
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:12]


def _extract_varmap(body: str) -> dict[str, str]:
    """Pull the single `varmap:` line out of an entry body, parse it."""
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("varmap:"):
            return parse_varmap_line(s)
    return {}


_PRECOND_LINE_RE = re.compile(r"^\((?:\d+)\)\s*(.+)$")
# `j120 := j70 * j114` — first j-var is the lemma's target.
_MONOMIAL_DEF_RE = re.compile(r"^(?P<target>j\d+)\s*:=\s*(?P<expr>.+)$")


def _extract_lemma_jform(body: str) -> tuple[list[str], str | None]:
    """Pull preconditions + conclusion from a ~lemma_builder body.

    Returns (precond_strings, conclusion_or_None). Anything after the
    `==>` line (variable assignments, monomial defs) is ignored — those
    are descriptive, not part of the lemma signature.
    """
    preconds: list[str] = []
    concl: str | None = None
    saw_concl = False
    for raw in body.splitlines():
        line = raw.rstrip()
        s = line.lstrip()
        if not s:
            continue
        if s.startswith("==>") or s.startswith("=>"):
            concl = s.lstrip("=").lstrip(">").strip()
            saw_concl = True
            continue
        if saw_concl:
            continue
        m = _PRECOND_LINE_RE.match(s)
        if m:
            preconds.append(m.group(1).strip())
            continue
        # Strategy line, free-form text — skip.
    return preconds, concl


def _extract_lemma_target_var(body: str, conclusion: str | None) -> str | None:
    """Pick the lemma's target j-var: the LHS of the first `j\\d+ := …`
    monomial-definition line whose target also appears in the conclusion
    (the "prioritized" monomial in `lemma.py`'s vocabulary).

    Falls back to the first un-prioritized monomial definition if none
    matches the conclusion, then to the first j-var in the conclusion
    itself, then to None. The fallback chain matches LemmaAnalyzer's
    own tie-breakers, so a target is found whenever the lemma has any
    structural anchor at all.
    """
    concl_jvars = set(_J_VAR_RE.findall(conclusion or ""))
    monomials: list[tuple[str, bool]] = []  # (target_var, is_in_conclusion)
    for raw in body.splitlines():
        s = raw.lstrip()
        if not s or s.startswith("==>") or s.startswith("=>"):
            continue
        m = _MONOMIAL_DEF_RE.match(s)
        if m:
            tgt = m.group("target")
            monomials.append((tgt, tgt in concl_jvars))
    for tgt, in_concl in monomials:
        if in_concl:
            return tgt
    if monomials:
        return monomials[0][0]
    if conclusion:
        m = _J_VAR_RE.search(conclusion)
        if m:
            return m.group(0)
    return None


def parse_lemma_xform_calls(
    trace_path: str | Path,
    *,
    coarse: bool = False,
) -> list[NraCall]:
    """One NraCall per ~lemma_builder block, with R/I-form fingerprints
    derived from the per-lemma varmap snapshot.

    Pairing rule: each ~lemma_builder is followed (in trace order, only
    nla_solver entries considered) by a `false_case_of_check_nla` entry
    whose body has the varmap line. Lemmas without a paired varmap are
    skipped — without one, fingerprinting would fall back to j-form,
    which is the broken state we're trying to escape.

    `coarse=True` switches to structural fingerprinting: aux Bool IDs
    are alpha-renamed and integer/rational literals collapse to LIT.
    Different threshold values and different aux-Bool atom orderings
    on the same monomial target then hash to one shape.
    """
    entries = [e for e in parse_trace(trace_path) if e.tag == "nla_solver"]
    return _calls_from_entries(entries, coarse=coarse)


def _calls_from_entries(
    entries: list[TraceEntry],
    *,
    coarse: bool = False,
) -> list[NraCall]:
    """Pair lemma_builder entries with varmap-bearing entries in FIFO order.

    z3 batches lemma emissions: it may flush several `~lemma_builder` blocks
    before flushing the corresponding `varmap:` blocks. Strict adjacency
    would drop ~78% of lemmas (467/2175 on the test trace) when in fact
    the counts match 1:1 globally. FIFO pairing recovers all of them.
    """
    lemmas = [e for e in entries if e.function == "~lemma_builder"]
    varmaps = [e for e in entries if "varmap:" in e.body]

    calls: list[NraCall] = []
    for lemma, vm_entry in zip(lemmas, varmaps):
        varmap = _extract_varmap(vm_entry.body)
        if not varmap:
            continue
        preconds_j, concl_j = _extract_lemma_jform(lemma.body)
        resolved: list[str] = []
        for p in preconds_j:
            resolved.append(_normalize(_resolve_jvars(p, varmap)))
        if concl_j is not None:
            resolved.append(_normalize(_resolve_jvars(concl_j, varmap)))
        if not resolved:
            continue
        # `variables` is computed from the un-coarsened resolved strings
        # so that nvars / vars-head columns stay meaningful (R/I IDs are
        # the structural variables, not the coarse-mode `LIT` token).
        vars_set: set[str] = set()
        for line in resolved:
            vars_set.update(_R_OR_I_VAR_RE.findall(line))
        sorted_vars = tuple(sorted(vars_set, key=lambda v: (v[0], int(v[1:]))))
        # The signature used for hashing: in coarse mode each line is
        # passed through _coarse_signature first. `constraints` (the
        # display-side tuple) follows the same transform so the top-
        # repeats representative shows the structural form, which is
        # what makes a coarse repeat actionable.
        sig_lines = (
            [_coarse_signature(line) for line in resolved] if coarse else resolved
        )
        calls.append(
            NraCall(
                index=len(calls),
                constraints=tuple(sorted(sig_lines)),
                raw_constraints=tuple(resolved),
                variables=sorted_vars,
                result=None,
                fingerprint=_fingerprint(sig_lines),
                line_number=lemma.line_number,
            )
        )
    return calls


# --- Target-only ("bracket") view -------------------------------------------
#
# A strict relaxation of the x-form fingerprint: keep only the lemma's target
# monomial (the LP variable being bounded / split / related), drop preconditions
# and conclusion-threshold structure entirely. Designed as a cascade
# diagnostic: "is a small set of monomials swallowing all the lemma volume?"
# See ../z3-research/lemur/target-only-fingerprint-proposal.md.

NO_TARGET_TEXT = "<no-target>"


@dataclass
class TargetCall:
    """One ~lemma_builder block reduced to (strategy, target text, fp)."""

    index: int
    strategy: str
    target_var: str | None  # the j-var name, or None
    target_text: str  # resolved + optionally coarsened; or NO_TARGET_TEXT
    fingerprint: str  # sha1[:12] over target_text
    line_number: int  # source-line in trace where lemma starts


def parse_lemma_target_calls(
    trace_path: str | Path,
    *,
    coarse: bool = True,
) -> list[TargetCall]:
    """One TargetCall per ~lemma_builder block.

    Pairing rule mirrors `parse_lemma_xform_calls`: each ~lemma_builder
    is followed (FIFO across nla_solver entries) by a varmap-bearing
    `false_case_of_check_nla` block.

    `coarse=True` (the sensible default for this view) applies
    `_coarse_signature` to the resolved target text — un-normalized
    targets re-introduce the literal noise that target-only is trying
    to escape (e.g. `+ 1500012 + I99` vs `+ 1500013 + I99` are the same
    target with different model snapshots). Set False to fingerprint
    raw resolved targets; useful for "are these two
    structurally-identical targets actually distinct constants?" cross-
    checks.
    """
    entries = [e for e in parse_trace(trace_path) if e.tag == "nla_solver"]
    return _target_calls_from_entries(entries, coarse=coarse)


def _target_calls_from_entries(
    entries: list[TraceEntry],
    *,
    coarse: bool,
) -> list[TargetCall]:
    lemmas = [e for e in entries if e.function == "~lemma_builder"]
    varmaps = [e for e in entries if "varmap:" in e.body]

    calls: list[TargetCall] = []
    for lemma, vm_entry in zip(lemmas, varmaps):
        varmap = _extract_varmap(vm_entry.body)
        if not varmap:
            continue
        body = lemma.body
        strategy = _strategy_from_body(body)
        _, concl = _extract_lemma_jform(body)
        target_var = _extract_lemma_target_var(body, concl)

        if target_var is None:
            target_text = NO_TARGET_TEXT
        else:
            # varmap entry for the target j-var carries the prefix-form
            # expression (e.g. `(* R188 R195)`); resolve any nested
            # j-vars too. If the var isn't in the varmap, fall back to
            # the bare j-var token — surfaces missing-mapping cases as
            # their own bucket rather than crashing.
            raw = varmap.get(target_var, target_var)
            target_text = _normalize(_resolve_jvars(raw, varmap))
            if coarse:
                target_text = _coarse_signature(target_text)

        calls.append(
            TargetCall(
                index=len(calls),
                strategy=strategy,
                target_var=target_var,
                target_text=target_text,
                fingerprint=_fingerprint([target_text]),
                line_number=lemma.line_number,
            )
        )
    return calls


def _strategy_from_body(body: str) -> str:
    """First non-empty line of a ~lemma_builder body is the strategy
    label (e.g. 'binomial sign anchor', 'propagate value - lower bound
    of range is above value'). Strip any trailing lemma-id integer."""
    for raw in body.splitlines():
        s = raw.strip()
        if not s:
            continue
        # Trim trailing integer (the LemmaAnalyzer also extracts this
        # as `lemma_id`; for the strategy label proper we want it gone).
        m = re.match(r"^(.*?)(\s+\d+)?$", s)
        return (m.group(1) if m else s).strip()
    return ""


# --- Target-only report + rendering -----------------------------------------


@dataclass
class TargetGroup:
    """One target-fingerprint bucket, with a strategy crosstab."""

    fingerprint: str
    target_text: str
    count: int
    strategies: Counter  # name -> emission count
    representative_line: int  # source-line of one example emission


@dataclass
class TargetReport:
    total: int
    unique_targets: int
    groups: list[TargetGroup]  # top-N by count, descending
    no_target_count: int  # lemmas with no extractable target


def build_target_report(calls: list[TargetCall], *, top: int = 10) -> TargetReport:
    if not calls:
        return TargetReport(total=0, unique_targets=0, groups=[], no_target_count=0)
    by_fp: dict[str, list[TargetCall]] = {}
    for c in calls:
        by_fp.setdefault(c.fingerprint, []).append(c)
    groups: list[TargetGroup] = []
    for fp, entries in by_fp.items():
        strategies: Counter = Counter(e.strategy for e in entries)
        groups.append(
            TargetGroup(
                fingerprint=fp,
                target_text=entries[0].target_text,
                count=len(entries),
                strategies=strategies,
                representative_line=entries[0].line_number,
            )
        )
    groups.sort(key=lambda g: -g.count)
    no_target = sum(1 for c in calls if c.target_text == NO_TARGET_TEXT)
    return TargetReport(
        total=len(calls),
        unique_targets=len(by_fp),
        groups=groups[:top],
        no_target_count=no_target,
    )


def _format_strategy_crosstab(strategies: Counter, *, max_keys: int = 4) -> str:
    """Render a strategy:count map compactly. Keep the top
    `max_keys` strategies; collapse the rest to `+N more`."""
    items = strategies.most_common()
    head = items[:max_keys]
    rest = items[max_keys:]
    parts = [f"{name}:{n}" for name, n in head]
    if rest:
        parts.append(f"+{sum(n for _, n in rest)} more")
    return "{" + ", ".join(parts) + "}"


def render_target_plain(report: TargetReport, *, show: bool = True) -> str:
    if report.total == 0:
        return "(no ~lemma_builder blocks found in trace)\n"
    pct_unique = 100.0 * report.unique_targets / report.total
    label_w = len("unique target monomials:")
    lines = [
        f"{'lemmas (~lemma_builder):':<{label_w}}  {report.total}",
        f"{'unique target monomials:':<{label_w}}  {report.unique_targets}  ({pct_unique:.1f}%)",
    ]
    if report.no_target_count:
        lines.append(f"{'lemmas without target:':<{label_w}}  {report.no_target_count}")
    if not report.groups:
        return "\n".join(lines) + "\n"

    covered = sum(g.count for g in report.groups)
    pct_top = 100.0 * covered / report.total
    lines.append("")
    lines.append(f"top {len(report.groups)} cover {pct_top:.1f}% of emissions:")
    for g in report.groups:
        share = 100.0 * g.count / report.total
        crosstab = _format_strategy_crosstab(g.strategies)
        lines.append(
            f"  count={g.count}  ({share:.1f}%)  "
            f"strategies={crosstab}  fp={g.fingerprint}"
        )
        if show:
            lines.append(f"      target: {g.target_text}")
    return "\n".join(lines) + "\n"


def render_target_rich(report: TargetReport, console, *, show: bool = True) -> None:
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    if report.total == 0:
        console.print("[yellow](no ~lemma_builder blocks found in trace)[/yellow]")
        return

    pct_unique = 100.0 * report.unique_targets / report.total
    overview = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
    overview.add_column("Key", style="bold")
    overview.add_column("Value")
    overview.add_row("lemmas (~lemma_builder)", str(report.total))
    overview.add_row(
        "unique target monomials", f"{report.unique_targets}  ({pct_unique:.1f}%)"
    )
    if report.no_target_count:
        overview.add_row("lemmas without target", str(report.no_target_count))
    console.print(
        Panel(
            overview,
            title=Text("nla --x-form --target-only", style="bold"),
            expand=False,
        )
    )

    if not report.groups:
        return
    covered = sum(g.count for g in report.groups)
    pct_top = 100.0 * covered / report.total
    t = Table(
        title=f"top {len(report.groups)} targets ({pct_top:.1f}% of emissions)",
        pad_edge=True,
    )
    t.add_column("count", justify="right", style="bold")
    t.add_column("share", justify="right")
    t.add_column("strategies", overflow="fold")
    t.add_column("fp", style="dim")
    for g in report.groups:
        share = f"{100.0 * g.count / report.total:.1f}%"
        t.add_row(
            str(g.count), share, _format_strategy_crosstab(g.strategies), g.fingerprint
        )
    console.print(t)
    if show:
        for g in report.groups:
            console.print(
                f"[bold]fp={g.fingerprint}[/bold]  [dim](count={g.count})[/dim]"
            )
            console.print(f"    {g.target_text}", soft_wrap=True)


def render_target_json(report: TargetReport, *, show: bool = True) -> str:
    import json

    body: dict = {
        "lemmas": report.total,
        "unique_targets": report.unique_targets,
        "no_target_count": report.no_target_count,
        "top_targets": [],
    }
    for g in report.groups:
        entry: dict = {
            "count": g.count,
            "fingerprint": g.fingerprint,
            "strategies": dict(g.strategies),
        }
        if show:
            entry["target_text"] = g.target_text
        body["top_targets"].append(entry)
    return json.dumps(body, indent=2)


# --- Auto-detect facade ------------------------------------------------------


def parse_xform_calls(
    trace_path: str | Path,
    *,
    prefer: str = "auto",
    nra_trace_path: str | Path | None = None,
    coarse: bool = False,
) -> tuple[list[NraCall], str]:
    """Pick the cheapest available x-form path; return (calls, source).

    `prefer='auto'` (default): try varmap first; fall back to [nra] data
    in `trace_path` (or `nra_trace_path`) if no varmap-bearing lemmas
    are present.

    `prefer='varmap'` / `prefer='nra'` force one path. `nra_trace_path`,
    if set, only matters when the [nra] path is selected.

    `coarse=True` swaps the per-call hashing for structural fingerprinting
    (see `_coarse_signature`). Both paths honor it.

    Returns the call list paired with the source label
    ('varmap' / 'nra') so callers can render an honest provenance note.
    """
    from lemur.nra_parsers import parse_nra_calls

    if prefer == "varmap":
        return parse_lemma_xform_calls(trace_path, coarse=coarse), "varmap"
    if prefer == "nra":
        src = nra_trace_path if nra_trace_path else trace_path
        return parse_nra_calls(src, coarse=coarse), "nra"

    # auto
    calls = parse_lemma_xform_calls(trace_path, coarse=coarse)
    if calls:
        return calls, "varmap"
    src = nra_trace_path if nra_trace_path else trace_path
    return parse_nra_calls(src, coarse=coarse), "nra"
