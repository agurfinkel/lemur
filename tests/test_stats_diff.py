"""Tests for cross-trace arith_conflict diff (lemur stats-diff)."""

import json
from pathlib import Path

from lemur.stats_diff import (
    DiffSubsection,
    diff_arith_conflict,
    render_json,
    render_plain,
)

SAMPLES = Path(__file__).parent / "sample_traces"
A_PATH = str(SAMPLES / "arith_conflict_diff_a.trace")
B_PATH = str(SAMPLES / "arith_conflict_diff_b.trace")


def _sub_by_label_prefix(d, prefix) -> DiffSubsection:
    for sub in d.subsections:
        if sub.label.startswith(prefix):
            return sub
    raise AssertionError(f"no subsection with prefix {prefix!r}")


def _row_by_key(sub: DiffSubsection, key: str):
    for r in sub.rows:
        if r.key == key:
            return r
    raise AssertionError(f"no row {key!r} in {[r.key for r in sub.rows]}")


def test_summary_counts_match_fixture():
    d = diff_arith_conflict(A_PATH, B_PATH, top_k=5)
    assert d.a_total == 3
    assert d.b_total == 4
    s = _sub_by_label_prefix(d, "summary")
    assert _row_by_key(s, "conflicts").a == "3"
    assert _row_by_key(s, "conflicts").b == "4"
    # A has 4 premise rows; B has 6.
    assert _row_by_key(s, "premise rows").a == "4"
    assert _row_by_key(s, "premise rows").b == "6"


def test_hot_blocks_ranked_by_max_per_side():
    d = diff_arith_conflict(A_PATH, B_PATH, top_k=5)
    hot = _sub_by_label_prefix(d, "hot blocks")
    keys_in_order = [r.key for r in hot.rows]
    # BLK__99_y (max=3 in B) ranks above BLK__65_x (max=2 in A).
    assert keys_in_order[:2] == ["BLK__99_y", "BLK__65_x"]
    a99 = _row_by_key(hot, "BLK__99_y")
    assert a99.a.startswith("0")
    assert a99.b.startswith("3")
    assert a99.delta == "+3"  # A=0, so no percent shown
    a65 = _row_by_key(hot, "BLK__65_x")
    assert a65.a.startswith("2")
    assert a65.b.startswith("1")


def test_top_constants_pretty_prints_and_ranks():
    d = diff_arith_conflict(A_PATH, B_PATH, top_k=5)
    consts = _sub_by_label_prefix(d, "top constants")
    keys = [r.key for r in consts.rows]
    # 2^256 ranks first (max count 2 in B), 2^64 second (max 1 in A).
    assert any("2^256" in k for k in keys[:1])
    assert any("2^64" in k for k in keys[:2])


def test_premise_shapes_uses_pp_for_percent_delta():
    d = diff_arith_conflict(A_PATH, B_PATH, top_k=5)
    shapes = _sub_by_label_prefix(d, "premise shapes")
    # A: 1 clean, 2 ite, 1 mod_div, 0 mixed (of 4 rows).
    # B: 0 clean, 4 ite, 2 mod_div, 0 mixed (of 6 rows).
    clean = _row_by_key(shapes, "clean_linear")
    assert clean.a.startswith("1 (")
    assert clean.b.startswith("0 (")
    assert clean.delta.endswith("pp")
    assert clean.delta.startswith("-")

    ite = _row_by_key(shapes, "ite_wrapped")
    assert ite.delta.endswith("pp")
    assert ite.delta.startswith("+")

    mixed = _row_by_key(shapes, "mixed")
    assert mixed.delta == "="


def test_render_plain_has_all_sections():
    d = diff_arith_conflict(A_PATH, B_PATH, top_k=5)
    out = render_plain(d)
    assert "## summary" in out
    assert "## hot blocks" in out
    assert "## top constants" in out
    assert "## premise shapes" in out
    assert "# A:" in out and "# B:" in out


def test_render_json_round_trip():
    d = diff_arith_conflict(A_PATH, B_PATH, top_k=5)
    obj = json.loads(render_json(d))
    assert obj["a_total"] == 3
    assert obj["b_total"] == 4
    assert len(obj["subsections"]) == 4
    labels = [s["label"] for s in obj["subsections"]]
    assert any(line.startswith("summary") for line in labels)
    assert any(line.startswith("hot blocks") for line in labels)


def test_top_k_caps_ranked_subsections():
    d = diff_arith_conflict(A_PATH, B_PATH, top_k=1)
    hot = _sub_by_label_prefix(d, "hot blocks")
    consts = _sub_by_label_prefix(d, "top constants")
    assert len(hot.rows) == 1
    assert len(consts.rows) == 1


def test_errors_on_missing_arith_conflict_in_a(tmp_path):
    empty = tmp_path / "empty.trace"
    empty.write_text("")
    import pytest

    with pytest.raises(ValueError, match="no \\[arith_conflict\\]"):
        diff_arith_conflict(str(empty), B_PATH)


def test_self_diff_zero_deltas():
    d = diff_arith_conflict(A_PATH, A_PATH, top_k=5)
    s = _sub_by_label_prefix(d, "summary")
    for r in s.rows:
        assert r.delta == "="
    shapes = _sub_by_label_prefix(d, "premise shapes")
    for r in shapes.rows:
        assert r.delta in ("=", "+0.0pp", "-0.0pp")
