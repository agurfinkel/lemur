"""
Round-productivity statistics for `lemur nla` summary view.

Each `core::check` round in nla_solver concludes with a trace line of
the form

    sz = N, m_to_refine = K STATUS

inside a `[nla_solver] patch_monomials_on_to_refine` block, where:

  * N = m_to_refine queue size at start of round
  * K = m_to_refine queue size at end of round
  * STATUS ∈ {less, same, more} — sign of K - N

For each trace we compute:

  * **Productivity rate** = `less` rounds / classified rounds.
    Macroscopic round-level success: fraction of rounds where the
    queue actually shrunk.
  * **Eviction yield** = (sum of N-K over `less`/`same`/`more`
    rounds) / total ~lemma_builder emissions. Per-lemma utility in
    monomials/lemma units. The proposal's empirical table on the
    kvault VC: bad-seed ≈ 0.24, good seed ≈ 0.60, winner ≈ 0.67 —
    sharper signal than productivity rate, but in unfamiliar units.

Both default-on as `lemur nla TRACE` summary lines once parsing is
wired. Help text frames this as a per-instance exploration tool, not
a general-purpose verdict; thresholds (0.35 / 0.40) are tentative on
N=3 traces from one VC.

See ../z3-research/lemur/burst-stats-proposal.md (the v1 burst-stats
postmortem and v2 productivity pivot) for the empirical table.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from lemur.parsers import TraceEntry

# `sz = 7, m_to_refine = 1 less` — N, K, status. Tolerant on whitespace
# around `=` and on optional trailing punctuation (the trace has bare
# words; older builds might wrap in parens).
_PRODUCTIVITY_LINE_RE = re.compile(
    r"sz\s*=\s*(?P<start>\d+)\s*,\s*"
    r"m_to_refine\s*=\s*(?P<end>\d+)\s*"
    r"\(?(?P<status>less|same|more)\)?"
)


@dataclass
class ProductivityStats:
    """Aggregate round-classification + per-lemma yield for one trace."""

    classified_rounds: int  # rounds with a parseable status line
    status_counts: Counter = field(default_factory=Counter)
    total_evictions: int = 0  # sum(start - end) across rounds
    total_lemmas: int = 0  # ~lemma_builder block count
    available: bool = True  # False when the trace lacks status lines

    @property
    def productivity_rate(self) -> float | None:
        """Fraction of classified rounds with status=less. None when no
        rounds were observed (avoids ZeroDivisionError on empty traces)."""
        if not self.classified_rounds:
            return None
        return self.status_counts.get("less", 0) / self.classified_rounds

    @property
    def eviction_yield(self) -> float | None:
        """Average monomials-evicted per emitted lemma. None when no
        lemmas were emitted; that's a degenerate trace where the metric
        has no meaning anyway."""
        if not self.total_lemmas:
            return None
        return self.total_evictions / self.total_lemmas

    @property
    def status_share(self) -> dict[str, float]:
        """{less, same, more} as fractions of classified_rounds. Returns
        an empty dict when nothing was classified."""
        if not self.classified_rounds:
            return {}
        return {s: n / self.classified_rounds for s, n in self.status_counts.items()}


def compute_productivity_stats(entries: list[TraceEntry]) -> ProductivityStats:
    """Walk nla_solver trace entries; return aggregate productivity.

    `entries` should already be filtered to tag == 'nla_solver' (caller's
    responsibility — the existing `_render_summary` code path does this).
    Counts ~lemma_builder blocks for the per-lemma yield denominator and
    parses `sz = N, m_to_refine = K STATUS` inside
    patch_monomials_on_to_refine bodies.

    `available=False` indicates the trace has no parseable status lines —
    older z3 builds, or builds where the relevant trace tag wasn't
    enabled. The renderer should print "unavailable" rather than zeros.
    """
    counts: Counter = Counter()
    classified = 0
    evictions = 0
    lemma_count = 0
    saw_any_status = False

    for e in entries:
        if e.function == "~lemma_builder":
            lemma_count += 1
            continue
        if e.function != "patch_monomials_on_to_refine":
            continue
        for line in e.body.splitlines():
            m = _PRODUCTIVITY_LINE_RE.search(line)
            if not m:
                continue
            saw_any_status = True
            start = int(m.group("start"))
            end = int(m.group("end"))
            status = m.group("status")
            counts[status] += 1
            classified += 1
            evictions += max(start - end, 0)

    return ProductivityStats(
        classified_rounds=classified,
        status_counts=counts,
        total_evictions=evictions,
        total_lemmas=lemma_count,
        available=saw_any_status,
    )
