"""Tests for the x-form nlsat-fingerprint parser and report."""

import json
from pathlib import Path

from lemur.nra_parsers import (
    _fingerprint,
    _normalize_constraint,
    build_xform_report,
    parse_nra_calls,
    render_xform_json,
    render_xform_plain,
)

SAMPLES = Path(__file__).parent / "sample_traces"
BASIC = SAMPLES / "nra_xform_basic.trace"  # real z3 trace, 6 unique calls
REPEATS = SAMPLES / "nra_xform_repeats.trace"  # synthetic, 3-of-4 calls share fp


def test_normalize_strips_negation_wrapper():
    assert _normalize_constraint("!(x0 - x1 < 0)") == "not x0 - x1 < 0"
    assert _normalize_constraint("x0 + 1 > 0") == "x0 + 1 > 0"
    # Nested parens kept intact
    assert (
        _normalize_constraint("!(- x9 - 7 x8 + x3 x5 > 0)")
        == "not - x9 - 7 x8 + x3 x5 > 0"
    )


def test_fingerprint_is_order_independent():
    a = ["c1", "c2", "c3"]
    b = ["c3", "c1", "c2"]
    assert _fingerprint(a) == _fingerprint(b)
    assert _fingerprint(["c1", "c2"]) != _fingerprint(["c1", "c3"])


def test_parse_nra_calls_basic():
    calls = parse_nra_calls(BASIC)
    assert len(calls) == 6
    # Each call has constraints, x-vars, and a result
    for c in calls:
        assert len(c.constraints) > 0
        assert all(v.startswith("x") for v in c.variables)
        assert c.result == "l_false"
    # Real z3 trace: every call should be unique (this benchmark closes
    # without re-asking nlsat).
    fps = {c.fingerprint for c in calls}
    assert len(fps) == 6


def test_parse_nra_calls_finds_repeats():
    calls = parse_nra_calls(REPEATS)
    assert len(calls) == 4
    # Calls 1, 2, 4 share a fingerprint; call 3 is distinct.
    by_fp = {}
    for c in calls:
        by_fp.setdefault(c.fingerprint, []).append(c.index)
    counts = sorted([len(v) for v in by_fp.values()], reverse=True)
    assert counts == [3, 1]


def test_normalization_collapses_constraint_order_within_call():
    # Calls 1 and 2 in REPEATS list the same constraints in opposite order.
    # After sort, they fingerprint identically.
    calls = parse_nra_calls(REPEATS)
    assert calls[0].fingerprint == calls[1].fingerprint
    assert calls[0].constraints == calls[1].constraints  # sorted tuple


def test_results_attached_correctly():
    calls = parse_nra_calls(REPEATS)
    assert [c.result for c in calls] == ["l_false", "l_false", "l_undef", "l_false"]


def test_variables_extracted_per_call():
    calls = parse_nra_calls(REPEATS)
    assert calls[0].variables == ("x0", "x1")
    # Call 3: x2, x3 (note "x3 x2" is a product term)
    assert calls[2].variables == ("x2", "x3")


def test_report_unique_count_and_repeats():
    calls = parse_nra_calls(REPEATS)
    report = build_xform_report(calls, top=10)
    assert report.total == 4
    assert report.unique_fingerprints == 2
    # One repeat surfaces (count=3); the unique call (count=1) is below
    # the >=2 cutoff.
    assert len(report.repeats) == 1
    count, call = report.repeats[0]
    assert count == 3
    assert call.size == 2
    assert call.variables == ("x0", "x1")


def test_report_size_distribution():
    calls = parse_nra_calls(REPEATS)
    report = build_xform_report(calls)
    # Sizes: 2, 2, 3, 2 -> min 2, median 2, max 3
    assert report.size_min == 2
    assert report.size_max == 3
    assert report.size_median == 2


def test_report_no_calls_is_clean():
    report = build_xform_report([], top=10)
    assert report.total == 0
    assert report.unique_fingerprints == 0
    assert report.repeats == []
    out = render_xform_plain(report)
    assert "no nlsat calls" in out


def test_render_plain_basic_shape():
    calls = parse_nra_calls(REPEATS)
    report = build_xform_report(calls, top=10)
    out = render_xform_plain(report)
    # The label-width for the 'unique fingerprints:' line drives column
    # width; the count must be present and so must the unique-fp line.
    assert "nlsat calls:" in out
    assert " 4" in out
    assert "unique fingerprints:" in out
    assert " 2" in out  # 2 unique
    assert "count=3" in out
    assert "l_false=3" in out
    assert "l_undef=1" in out


def test_render_json_keys():
    calls = parse_nra_calls(REPEATS)
    report = build_xform_report(calls, top=10)
    obj = json.loads(render_xform_json(report))
    assert obj["nlsat_calls"] == 4
    assert obj["unique_fingerprints"] == 2
    assert len(obj["top_repeats"]) == 1
    r = obj["top_repeats"][0]
    assert r["count"] == 3
    assert r["size"] == 2
    assert r["variables"] == ["x0", "x1"]
    # Without --show, no constraints field is emitted.
    assert "constraints" not in r


def test_render_plain_show_dumps_constraints_under_each_repeat():
    calls = parse_nra_calls(REPEATS)
    report = build_xform_report(calls, top=10)
    out = render_xform_plain(report, show=True)
    # Top-repeats header still present, plus the constraints body indented
    # below each row so the user can read what shape this fingerprint
    # encodes.
    assert "top repeats:" in out
    assert "count=3" in out
    rep_call = report.repeats[0][1]
    for c in rep_call.constraints:
        assert c in out
    # Without --show, no constraint body.
    plain = render_xform_plain(report, show=False)
    for c in rep_call.constraints:
        assert plain.count(c) == 0


def test_render_json_show_includes_constraints():
    calls = parse_nra_calls(REPEATS)
    report = build_xform_report(calls, top=10)
    obj = json.loads(render_xform_json(report, show=True))
    r = obj["top_repeats"][0]
    assert "constraints" in r
    assert r["constraints"] == list(report.repeats[0][1].constraints)


def test_top_caps_repeats():
    # All 4 distinct repeats with count >= 2 fitting in top=10 by default;
    # cap to top=0 to drop them all.
    calls = parse_nra_calls(REPEATS)
    report = build_xform_report(calls, top=0)
    assert report.repeats == []
