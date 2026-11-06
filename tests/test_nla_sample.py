"""Tests for `lemur nla --sample STRATEGY=N`."""

from dataclasses import dataclass

import pytest

from lemur.cli.nla import _apply_sample, _parse_sample_spec


@dataclass
class _Stub:
    strategy: str
    tag: int = 0  # unique per record so list ops compare by identity


def _stubs(strategies):
    return [_Stub(strategy=s, tag=i) for i, s in enumerate(strategies)]


def test_parse_sample_spec_valid():
    assert _parse_sample_spec("nlsat=4") == ("nlsat", 4)
    assert _parse_sample_spec("ord-binom=10") == ("ord-binom", 10)
    # case-insensitive
    assert _parse_sample_spec("NLSAT=2") == ("nlsat", 2)


def test_parse_sample_spec_rejects_missing_eq():
    with pytest.raises(SystemExit) as exc:
        _parse_sample_spec("noequals")
    assert exc.value.code == 2


def test_parse_sample_spec_rejects_non_int_n():
    with pytest.raises(SystemExit) as exc:
        _parse_sample_spec("nlsat=abc")
    assert exc.value.code == 2


def test_parse_sample_spec_rejects_zero_or_negative():
    with pytest.raises(SystemExit):
        _parse_sample_spec("nlsat=0")
    with pytest.raises(SystemExit):
        _parse_sample_spec("nlsat=-1")


def test_apply_sample_picks_evenly_spread_indices():
    # 10 lemmas, all matching, n=4 -> i*10/4 -> 0, 2, 5, 7
    records = _stubs(["nlsat"] * 10)
    picked = _apply_sample(records, "nlsat", 4)
    assert len(picked) == 4
    assert [r.tag for r in picked] == [0, 2, 5, 7]


def test_apply_sample_returns_all_when_n_exceeds_total():
    records = _stubs(["nlsat"] * 3)
    picked = _apply_sample(records, "nlsat", 5)
    assert len(picked) == 3


def test_apply_sample_filters_by_substring():
    records = _stubs(["ord-binom", "grob-q", "ord-foo", "grob-f", "ord-bar"])
    picked = _apply_sample(records, "ord", 2)
    # 3 matches at indices 0, 2, 4 in records; n=2 -> i*3/2 -> 0, 1
    # which means picked[0] = matches[0] = records[0], picked[1] = matches[1] = records[2]
    assert [r.strategy for r in picked] == ["ord-binom", "ord-foo"]


def test_apply_sample_empty_when_no_match():
    records = _stubs(["ord-binom", "grob-q"])
    picked = _apply_sample(records, "nlsat", 4)
    assert picked == []


def test_apply_sample_skips_records_without_strategy():
    # Records with strategy=None or '' shouldn't crash the matcher.
    records = [_Stub(strategy=None), _Stub(strategy=""), _Stub(strategy="nlsat")]
    picked = _apply_sample(records, "nlsat", 5)
    assert len(picked) == 1
    assert picked[0].strategy == "nlsat"


def test_apply_sample_dedups_when_indices_collide():
    # n > total can collide via integer division. _apply_sample is supposed
    # to short-circuit when n >= total to avoid this, but test the boundary.
    records = _stubs(["nlsat"] * 4)
    picked = _apply_sample(records, "nlsat", 4)
    # All 4 picked, no dups
    assert len(picked) == 4
    assert len(set(id(r) for r in picked)) == 4


def test_apply_sample_substring_match_is_case_insensitive():
    records = _stubs(["NLSAT-Hard", "nlsat-easy", "ord-binom"])
    picked = _apply_sample(records, "nlsat", 5)
    assert len(picked) == 2
