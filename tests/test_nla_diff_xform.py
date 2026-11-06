"""Tests for the x-form upgrade of lemur nla-diff."""

from pathlib import Path

from lemur.nla_diff import compute_metrics, diff

SAMPLES = Path(__file__).parent / "sample_traces"
NLA_VARMAP = SAMPLES / "nla_varmap_basic.trace"  # 3 lemmas, 2 unique
NRA_REPEATS = SAMPLES / "nra_xform_repeats.trace"  # nra-only, 4 calls, 2 unique
NRA_BASIC = SAMPLES / "nra_xform_basic.trace"  # both tags, no real repeats


def _label_to_row(rows, prefix):
    """Find the first row whose label starts with prefix."""
    for r in rows:
        if r.label.startswith(prefix):
            return r
    raise AssertionError(f"no row {prefix!r} in {[r.label for r in rows]}")


def test_compute_metrics_picks_varmap_when_available():
    m = compute_metrics(str(NLA_VARMAP))
    assert m.xform_source == "varmap"
    assert m.nlsat_calls == 3
    assert len(m.nlsat_fingerprints) == 2  # 2 unique


def test_compute_metrics_falls_back_to_nra_when_no_varmap():
    m = compute_metrics(str(NRA_REPEATS))
    assert m.xform_source == "nra"
    assert m.nlsat_calls == 4
    assert len(m.nlsat_fingerprints) == 2


def test_diff_uses_varmap_label_when_both_sides_varmap():
    m_a = compute_metrics(str(NLA_VARMAP))
    m_b = compute_metrics(str(NLA_VARMAP))
    rows = diff(m_a, m_b, top=5)
    units = _label_to_row(rows, "lemmas (varmap-resolved)")
    assert units.a == 3 and units.b == 3
    fp_row = _label_to_row(rows, "top-fp(1)")
    assert fp_row.a == 2 and fp_row.b == 2  # the count=2 repeating lemma


def test_diff_uses_nra_label_when_both_sides_nra():
    m_a = compute_metrics(str(NRA_REPEATS))
    m_b = compute_metrics(str(NRA_REPEATS))
    rows = diff(m_a, m_b, top=5)
    units = _label_to_row(rows, "nlsat calls (nra)")
    assert units.a == 4 and units.b == 4
    fp_row = _label_to_row(rows, "top-fp(1)")
    assert fp_row.a == 3 and fp_row.b == 3


def test_diff_handles_b_only_repeats():
    # NRA_BASIC has 6 nlsat calls all unique (auto -> nra path because
    # the trace has nla_solver+nra but the lemmas don't repeat in
    # varmap-resolved form);
    # NRA_REPEATS has 4 calls, 2 unique.
    # We force varmap on A (which has 0 lemmas) and nra on B by feeding
    # different fixtures.
    m_a = compute_metrics(str(NRA_BASIC))  # falls back to varmap on its lemmas
    m_b = compute_metrics(str(NRA_REPEATS))  # nra fallback
    rows = diff(m_a, m_b, top=5)
    fp_row = _label_to_row(rows, "top-fp(1)")
    assert fp_row.b >= 2  # B has the repeating fp


def test_diff_handles_no_xform_data(monkeypatch):
    # Synthesise the empty case directly: both metrics have no xform data.
    m_a = compute_metrics(str(NRA_BASIC))
    m_b = compute_metrics(str(NRA_BASIC))
    m_a.nlsat_calls = 0
    m_a.nlsat_fingerprints.clear()
    m_a.nlsat_fp_to_call.clear()
    m_a.xform_source = None
    m_b.nlsat_calls = 0
    m_b.nlsat_fingerprints.clear()
    m_b.nlsat_fp_to_call.clear()
    m_b.xform_source = None
    rows = diff(m_a, m_b, top=5)
    fallback = _label_to_row(rows, "x-form units")
    assert fallback.a == "n/a" and fallback.b == "n/a"
    assert "neither trace had" in fallback.delta
