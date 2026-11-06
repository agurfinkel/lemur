"""Tests for round-productivity / eviction-yield statistics."""

from lemur.parsers import TraceEntry, parse_trace
from lemur.productivity import compute_productivity_stats


def _entries(trace_text: str) -> list[TraceEntry]:
    """Helper: parse an inline trace string into TraceEntry list."""
    import io

    return list(parse_trace(io.StringIO(trace_text)))


HEADER_PATCH = "-------- [nla_solver] patch_monomials_on_to_refine /p:1 ---------"
HEADER_LEMMA = "-------- [nla_solver] ~lemma_builder /p:2 ---------"
FOOTER = "------------------------------------------------"


def _patch_block(line: str) -> str:
    return f"{HEADER_PATCH}\n{line}\n{FOOTER}\n"


def _lemma_block() -> str:
    return f"{HEADER_LEMMA}\nstrategy 1\n ==> j1 >= 1\n{FOOTER}\n"


def test_empty_trace_reports_unavailable():
    s = compute_productivity_stats([])
    assert s.available is False
    assert s.classified_rounds == 0
    assert s.productivity_rate is None
    assert s.eviction_yield is None


def test_all_less_rounds_one_hundred_percent_productivity():
    trace = (
        _patch_block("sz = 5, m_to_refine = 2 less")
        + _patch_block("sz = 3, m_to_refine = 1 less")
        + _lemma_block() * 4
    )
    s = compute_productivity_stats(_entries(trace))
    assert s.available is True
    assert s.classified_rounds == 2
    assert s.productivity_rate == 1.0
    # Evictions: 5-2=3, 3-1=2 → 5 total / 4 lemmas = 1.25
    assert s.total_evictions == 5
    assert s.total_lemmas == 4
    assert s.eviction_yield == 1.25


def test_all_same_rounds_zero_productivity_and_zero_yield():
    trace = (
        _patch_block("sz = 4, m_to_refine = 4 same")
        + _patch_block("sz = 6, m_to_refine = 6 same")
        + _lemma_block() * 3
    )
    s = compute_productivity_stats(_entries(trace))
    assert s.classified_rounds == 2
    assert s.productivity_rate == 0.0
    assert s.total_evictions == 0
    assert s.eviction_yield == 0.0


def test_mixed_status_share_sums_to_one():
    trace = (
        _patch_block("sz = 5, m_to_refine = 2 less")
        + _patch_block("sz = 5, m_to_refine = 5 same")
        + _patch_block("sz = 5, m_to_refine = 5 same")
        + _patch_block("sz = 5, m_to_refine = 5 same")
        + _lemma_block() * 8
    )
    s = compute_productivity_stats(_entries(trace))
    assert s.classified_rounds == 4
    # 1/4 = 0.25 less, 3/4 = 0.75 same
    assert s.productivity_rate == 0.25
    assert s.status_share == {"less": 0.25, "same": 0.75}
    # Single eviction (3) over 8 lemmas → 0.375 yield
    assert s.eviction_yield == 0.375


def test_more_status_supported_even_if_rare():
    # m_to_refine *can* grow if init_to_refine re-scans mid-round on
    # some builds; the classifier should accept it without crashing.
    trace = _patch_block("sz = 2, m_to_refine = 5 more") + _lemma_block()
    s = compute_productivity_stats(_entries(trace))
    assert s.status_counts["more"] == 1
    # No evictions when end > start (max(start-end, 0) clamps).
    assert s.total_evictions == 0


def test_unparseable_status_lines_dont_classify():
    # patch_monomials_on_to_refine block but the body line doesn't
    # match the format — typo, older z3 build, whatever. The parser
    # must skip the line, not crash, and not over-count.
    trace = (
        _patch_block("sz = whatever, m_to_refine = ??? less")  # malformed
        + _patch_block("sz = 5, m_to_refine = 2 less")  # valid
        + _lemma_block() * 1
    )
    s = compute_productivity_stats(_entries(trace))
    assert s.classified_rounds == 1
    assert s.productivity_rate == 1.0


def test_lemmas_outside_patch_blocks_count_for_yield():
    # Lemma blocks are anywhere in the nla_solver entry stream, not
    # only inside patch blocks. The denominator must count all of them.
    trace = (
        _lemma_block() * 3
        + _patch_block("sz = 4, m_to_refine = 0 less")
        + _lemma_block() * 2
    )
    s = compute_productivity_stats(_entries(trace))
    assert s.total_lemmas == 5
    # 4 evictions / 5 lemmas = 0.8 yield
    assert s.eviction_yield == 0.8


def test_no_lemmas_yields_none_not_zero():
    trace = _patch_block("sz = 3, m_to_refine = 1 less")
    s = compute_productivity_stats(_entries(trace))
    # Round is classified, but yield denominator is zero — should be
    # None (degenerate trace), not 0.0.
    assert s.classified_rounds == 1
    assert s.eviction_yield is None


def test_real_combo_all_bad_seed_trace():
    """End-to-end: the bad-seed trace at /tmp/lemur-coarse-fp/combo-bad
    should reproduce roughly the proposal's table — productivity well
    below 35 %, yield well below 0.40. Numbers will differ from the
    proposal's T:12 run since the test trace was captured at T:30, but
    the qualitative signal must hold."""
    import os

    path = "/tmp/lemur-coarse-fp/combo-bad/.z3-trace"
    if not os.path.exists(path):
        return  # local-machine fixture; skip on CI / fresh checkouts
    entries = [e for e in parse_trace(path) if e.tag == "nla_solver"]
    s = compute_productivity_stats(entries)
    assert s.available is True
    assert s.classified_rounds > 1000
    assert 0.10 <= (s.productivity_rate or 0) <= 0.30
    assert 0.10 <= (s.eviction_yield or 0) <= 0.30
