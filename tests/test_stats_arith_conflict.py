"""Tests for the arith_conflict analyzer in lemur/stats.py."""

from pathlib import Path

from lemur.parsers import group_by_tag, parse_trace
from lemur.stats import analyze_arith_conflict, build_stats_output

FIXTURE = Path(__file__).parent / "sample_traces" / "arith_conflict_basic.trace"

POW_2_256 = (
    "115792089237316195423570985008687907853269984665640564039457584007913129639936"
)


def _entries():
    by_tag = group_by_tag(list(parse_trace(FIXTURE)))
    return by_tag["arith_conflict"]


def _section(result, label_prefix):
    for label, rows in result:
        if label.startswith(label_prefix):
            return dict(rows)
    raise AssertionError(
        f"no subsection starting with {label_prefix!r} in {[line for line, _ in result]}"
    )


def test_summary_counts():
    result = analyze_arith_conflict(_entries(), top_k=5)
    s = _section(result, "summary")
    assert s["conflicts"] == "3"
    assert s["distinct blocks"] == "3"
    assert s["premise rows"] == "12"


def test_hot_blocks_uses_full_names():
    result = analyze_arith_conflict(_entries(), top_k=5)
    rows = dict(_section(result, "hot blocks").items())
    # Each fixture conflict mentions exactly one BLK__ family (or none), so all
    # three full-name blocks tie at count 1.
    assert "BLK__121_1_0_0_0_1475" in rows
    assert "BLK__109_1_0_0_0_1459" in rows
    assert "BLK__147_1_0_0_0_1495" in rows
    # Counts are stringified as "N (PCT%)".
    assert all(v.startswith("1 (") for v in rows.values())


def test_top_constants_pretty_prints_2_256():
    result = analyze_arith_conflict(_entries(), top_k=5)
    rows = dict(_section(result, "top constants").items())
    # 2^256 appears in conflicts @55 and @213 -> count 2; pretty-printed.
    matching = [k for k in rows if k.startswith("2^256")]
    assert matching, f"no 2^256 row in {list(rows)}"
    assert rows[matching[0]].startswith("2 (")


def test_premise_shape_histogram():
    result = analyze_arith_conflict(_entries(), top_k=5)
    rows = dict(_section(result, "premise shapes").items())
    # Hand-counted from the fixture: 2 clean_linear, 2 ite_wrapped, 3 mod_div, 5 mixed.
    assert rows["clean_linear"].startswith("2 (")
    assert rows["ite_wrapped"].startswith("2 (")
    assert rows["mod_div_wrapped"].startswith("3 (")
    assert rows["mixed"].startswith("5 (")


def test_top_k_caps_ranked_subsections():
    result = analyze_arith_conflict(_entries(), top_k=2)
    # hot blocks should now have at most 2 rows.
    for label, rows in result:
        if label.startswith("hot blocks"):
            assert len(rows) <= 2
        if label.startswith("top constants"):
            assert len(rows) <= 2


def test_build_stats_output_emits_one_section_per_subsection():
    out = build_stats_output(FIXTURE)
    titles = [t for t, _ in out.sections]
    # Expect: Summary + 4 arith_conflict subsections.
    arith_titles = [t for t in titles if t.startswith("[arith_conflict]")]
    assert len(arith_titles) == 4
    assert any("summary" in t for t in arith_titles)
    assert any("hot blocks" in t for t in arith_titles)
    assert any("top constants" in t for t in arith_titles)
    assert any("premise shapes" in t for t in arith_titles)


def test_top_k_threads_through_build_stats_output():
    out = build_stats_output(FIXTURE, top_k=1)
    for title, rows in out.sections:
        if title.startswith("[arith_conflict] hot blocks"):
            assert len(rows) == 1
        if title.startswith("[arith_conflict] top constants"):
            assert len(rows) == 1
