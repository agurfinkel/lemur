"""Tests for varmap-resolved x-form fingerprinting (no -tr:nra needed)."""

import json
from pathlib import Path

from lemur.lemma_xform import (
    _coarse_signature,
    _extract_lemma_jform,
    _extract_lemma_target_var,
    _resolve_jvars,
    build_target_report,
    parse_lemma_target_calls,
    parse_lemma_xform_calls,
    parse_xform_calls,
    render_target_json,
    render_target_plain,
)
from lemur.nra_parsers import build_xform_report

SAMPLES = Path(__file__).parent / "sample_traces"
NLA_VARMAP = SAMPLES / "nla_varmap_basic.trace"  # 3 lemmas, 2 unique
NRA_ONLY = SAMPLES / "nra_xform_repeats.trace"  # synthetic, [nra] entries only


def test_resolve_jvars_substitutes_simple():
    vm = {"j16": "R188", "j21": "R195"}
    assert _resolve_jvars("j16 + j21 <= 0", vm) == "R188 + R195 <= 0"


def test_resolve_jvars_handles_composite_expr():
    vm = {"j26": "(* R188 R195)"}
    assert _resolve_jvars("j26 >= 1", vm) == "(* R188 R195) >= 1"


def test_resolve_jvars_chained_substitution():
    # If varmap chains (j87 -> "(* R188 j82)" -> j82 -> "(div I176 R188)"),
    # the loop should resolve transitively.
    vm = {"j87": "(* R188 j82)", "j82": "(div I176 R188)"}
    assert _resolve_jvars("j87 >= 1", vm) == "(* R188 (div I176 R188)) >= 1"


def test_extract_lemma_jform_basic():
    body = (
        "propagate value - lower bound of range is above value 1\n"
        "(81) j16 >= 1\n"
        "(193) j21 >= 1\n"
        " ==> j26 >= 1\n"
        "j21 =   3112          [1, oo]\n"
    )
    preconds, concl = _extract_lemma_jform(body)
    assert preconds == ["j16 >= 1", "j21 >= 1"]
    assert concl == "j26 >= 1"


def test_parse_lemma_xform_calls_full_pass():
    calls = parse_lemma_xform_calls(NLA_VARMAP)
    assert len(calls) == 3
    # Lemmas 1 and 3 share a fingerprint; lemma 2 is unique.
    assert calls[0].fingerprint == calls[2].fingerprint
    assert calls[0].fingerprint != calls[1].fingerprint
    # Resolved constraints for lemma 1: R188 >= 1, R195 >= 1, (* R188 R195) >= 1
    assert any("R188 >= 1" in c for c in calls[0].constraints)
    assert any("R195 >= 1" in c for c in calls[0].constraints)
    assert any("(* R188 R195) >= 1" in c for c in calls[0].constraints)


def test_parse_lemma_xform_extracts_R_and_I_vars():
    calls = parse_lemma_xform_calls(NLA_VARMAP)
    # Lemma 2 references R188 (from j16) and I176 (inside (div I176 R188)).
    assert "R188" in calls[1].variables
    assert "I176" in calls[1].variables


def test_xform_report_via_varmap():
    calls = parse_lemma_xform_calls(NLA_VARMAP)
    report = build_xform_report(calls, top=10)
    assert report.total == 3
    assert report.unique_fingerprints == 2
    # One repeating fingerprint, count=2.
    assert len(report.repeats) == 1
    count, _ = report.repeats[0]
    assert count == 2


def test_parse_xform_calls_auto_picks_varmap_when_present():
    calls, source = parse_xform_calls(str(NLA_VARMAP), prefer="auto")
    assert source == "varmap"
    assert len(calls) == 3


def test_parse_xform_calls_auto_falls_back_to_nra():
    # NRA_ONLY has [nra] entries but no ~lemma_builder + varmap pairing.
    calls, source = parse_xform_calls(str(NRA_ONLY), prefer="auto")
    assert source == "nra"
    assert len(calls) == 4  # synthetic fixture has 4 nra calls


def test_parse_xform_calls_force_nra_path():
    # Forcing the nra path on an nla-only trace yields zero calls (and
    # the caller is expected to handle the empty result).
    calls, source = parse_xform_calls(str(NLA_VARMAP), prefer="nra")
    assert source == "nra"
    assert calls == []


def test_parse_xform_calls_force_varmap_path():
    calls, source = parse_xform_calls(str(NLA_VARMAP), prefer="varmap")
    assert source == "varmap"
    assert len(calls) == 3


# --- Coarse fingerprint -----------------------------------------------------


def test_coarse_collapses_standalone_integers():
    assert _coarse_signature("R188 >= 1") == "R188 >= LIT"
    assert _coarse_signature("R188 + R195 <= 0") == "R188 + R195 <= LIT"


def test_coarse_collapses_negatives_and_rationals():
    assert _coarse_signature("R7 >= -12") == "R7 >= LIT"
    assert _coarse_signature("R7 <= 5/4") == "R7 <= LIT"
    assert _coarse_signature("R7 <= -5 / 4") == "R7 <= LIT"


def test_coarse_collapses_bignum_underscores():
    # z3's bignums print with underscores; the regex must absorb them.
    assert (
        _coarse_signature("(* R188 R195) <= 115_792_089_237") == "(* R188 R195) <= LIT"
    )


def test_coarse_preserves_smt_identifier_digits():
    # R/I/x/j var IDs and CANON-style enode names must NOT be normalized
    # — they identify the structural variable, not snapshot data.
    assert _coarse_signature("R188 >= 1") == "R188 >= LIT"
    assert _coarse_signature("I176 + I99 = 0") == "I176 + I99 = LIT"
    assert _coarse_signature("x12 * x42 >= 1") == "x12 * x42 >= LIT"
    assert _coarse_signature("(* CANON123!!8) <= 0") == "(* CANON123!!8) <= LIT"


def test_coarse_alpha_renames_aux_bool_ids_by_first_appearance():
    # First #-id seen becomes #A0, second becomes #A1, etc.
    out = _coarse_signature("(or #4422 #4427)")
    assert out == "(or #A0 #A1)"
    # Repeated reference reuses the same alias.
    out = _coarse_signature("(or #4422 (and #4427 #4422))")
    assert out == "(or #A0 (and #A1 #A0))"


def test_coarse_state_is_per_signature_not_global():
    # Each call to _coarse_signature starts the rename map at #A0.
    out1 = _coarse_signature("(or #4422 #4427)")
    out2 = _coarse_signature("(or #9999 #1000)")
    assert out1 == out2 == "(or #A0 #A1)"


def test_coarse_collapses_two_threshold_variants_to_one_shape():
    # Two emissions of the "same shape, different threshold" pattern.
    a = _coarse_signature("(* R188 R195) >= 1")
    b = _coarse_signature("(* R188 R195) >= 7")
    assert a == b == "(* R188 R195) >= LIT"


def test_coarse_collapses_aux_id_reorderings_to_one_shape():
    # Two emissions of the "same shape, different aux Bools in the same
    # positions". Crucial for the long-tail conclusions where the lemma
    # builder emits disjunctions over fresh auxiliaries on each call.
    a = _coarse_signature("(or #4422 #4427) ==> (* R188 R195) >= 1")
    b = _coarse_signature("(or #5500 #5503) ==> (* R188 R195) >= 7")
    assert a == b


def test_coarse_does_not_collapse_distinct_monomial_targets():
    # Different R/I bracket → different shape, even after coarsening.
    a = _coarse_signature("(* R188 R195) >= 1")
    b = _coarse_signature("(* R188 R200) >= 1")
    assert a != b


def test_parse_lemma_xform_calls_coarse_buckets_threshold_drift():
    # The basic fixture has 3 lemmas: 1 and 3 are identical (same fp
    # already under fine), 2 differs. Under coarse the same picture
    # should hold for THIS fixture (literals are all `1`, so coarse =
    # fine on the constant). This test is about pipeline correctness:
    # the coarse path runs end-to-end and produces the same kind of
    # NraCall list.
    calls = parse_lemma_xform_calls(NLA_VARMAP, coarse=True)
    assert len(calls) == 3
    assert calls[0].fingerprint == calls[2].fingerprint
    assert calls[0].fingerprint != calls[1].fingerprint
    # Display side reflects coarsening: every numeric literal is LIT.
    assert all("1" not in c or "LIT" in c for c in calls[0].constraints), calls[
        0
    ].constraints
    assert any("LIT" in c for c in calls[0].constraints)


def test_parse_xform_calls_threads_coarse_through_auto_path():
    fine, _ = parse_xform_calls(str(NLA_VARMAP), prefer="auto", coarse=False)
    coarse, _ = parse_xform_calls(str(NLA_VARMAP), prefer="auto", coarse=True)
    # Same call count, but per-call signatures differ once a literal
    # appears in the resolved string.
    assert len(fine) == len(coarse) == 3
    assert any("LIT" in c for c in coarse[0].constraints)
    assert not any("LIT" in c for c in fine[0].constraints)


# --- Target-only ("bracket") view -------------------------------------------


def test_extract_target_var_picks_prioritized_monomial():
    # j26 is in the conclusion AND has a monomial def, so it's prioritized.
    body = (
        "propagate value\n"
        "(81) j16 >= 1\n"
        "(193) j21 >= 1\n"
        " ==> j26 >= 1\n"
        "j16 := j10 * j15\n"
        "j26 := j16 * j21\n"
    )
    assert _extract_lemma_target_var(body, "j26 >= 1") == "j26"


def test_extract_target_var_falls_back_to_first_monomial_when_none_in_concl():
    # Conclusion mentions only j99 (no := def for j99); first def is j26.
    body = "strategy\n ==> j99 >= 1\nj26 := j16 * j21\n"
    assert _extract_lemma_target_var(body, "j99 >= 1") == "j26"


def test_extract_target_var_falls_back_to_concl_jvar_when_no_monomials():
    # No monomial defs at all.
    body = "strategy\n ==> j42 >= 1\n"
    assert _extract_lemma_target_var(body, "j42 >= 1") == "j42"


def test_extract_target_var_returns_none_when_nothing_extractable():
    body = "strategy\n ==> false\n"
    assert _extract_lemma_target_var(body, "false") is None


def test_parse_lemma_target_calls_full_pass():
    # The basic fixture: 3 lemmas. Lemma 1 and 3 share target j26 →
    # (* R188 R195); lemma 2 has target j87 → (* R188 (div I176 R188)).
    calls = parse_lemma_target_calls(NLA_VARMAP, coarse=False)
    assert len(calls) == 3
    assert calls[0].target_var == "j26"
    assert calls[0].target_text == "(* R188 R195)"
    assert calls[2].target_text == "(* R188 R195)"
    # Same target → same fingerprint.
    assert calls[0].fingerprint == calls[2].fingerprint
    # Different target → different fingerprint.
    assert calls[1].target_text == "(* R188 (div I176 R188))"
    assert calls[1].fingerprint != calls[0].fingerprint


def test_parse_lemma_target_calls_strategy_extraction():
    calls = parse_lemma_target_calls(NLA_VARMAP, coarse=False)
    # All three fixture lemmas have the same "propagate value..." prefix
    # strategy line; the trailing literal varies (1, 2, 3) and gets
    # stripped as the lemma_id by _strategy_from_body.
    assert all("propagate value" in c.strategy for c in calls)


def test_parse_lemma_target_calls_coarse_strips_literals_from_target():
    # In the basic fixture targets contain no literals, so coarse=False
    # and coarse=True produce identical text. This confirms the pipeline
    # plumbs `coarse` end-to-end without smashing target text when
    # there's nothing to coarsen.
    fine = parse_lemma_target_calls(NLA_VARMAP, coarse=False)
    coarse = parse_lemma_target_calls(NLA_VARMAP, coarse=True)
    assert [c.target_text for c in fine] == [c.target_text for c in coarse]


def test_target_collapses_diff_conclusion_same_target():
    # Synthetic: two lemmas, same target j26 / (* R188 R195), different
    # conclusions. Under --x-form they're distinct fingerprints; under
    # --target-only they collapse to one.
    import tempfile

    trace = (
        "-------- [nla_solver] ~lemma_builder a.cpp:1 ---------\n"
        "binomial sign anchor\n"
        "(81) j16 >= 1\n"
        " ==> j26 >= 1\n"
        "j26 := j16 * j21\n"
        "------------------------------------------------\n"
        "-------- [nla_solver] false_case_of_check_nla a.cpp:2 ---------\n"
        "varmap: j16=1: R188 j21=2: R195 j26=3: (* R188 R195)\n"
        "------------------------------------------------\n"
        "-------- [nla_solver] ~lemma_builder a.cpp:1 ---------\n"
        "low>val\n"
        "(81) j16 >= 99\n"
        " ==> j26 >= 7\n"
        "j26 := j16 * j21\n"
        "------------------------------------------------\n"
        "-------- [nla_solver] false_case_of_check_nla a.cpp:2 ---------\n"
        "varmap: j16=1: R188 j21=2: R195 j26=3: (* R188 R195)\n"
        "------------------------------------------------\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".trace", delete=False) as f:
        f.write(trace)
        path = f.name
    calls = parse_lemma_target_calls(path, coarse=True)
    assert len(calls) == 2
    assert calls[0].fingerprint == calls[1].fingerprint
    report = build_target_report(calls, top=10)
    assert report.unique_targets == 1
    assert report.groups[0].count == 2
    # Strategy crosstab records both strategies under the single target.
    assert dict(report.groups[0].strategies) == {
        "binomial sign anchor": 1,
        "low>val": 1,
    }


def test_render_target_plain_includes_strategy_crosstab_and_target_text():
    calls = parse_lemma_target_calls(NLA_VARMAP, coarse=False)
    report = build_target_report(calls, top=10)
    out = render_target_plain(report, show=True)
    assert "lemmas (~lemma_builder):" in out
    assert "unique target monomials:" in out
    assert "strategies=" in out
    # Top group's target text appears under its row.
    assert "target: " in out


def test_render_target_json_emits_strategies_and_target_text_with_show():
    calls = parse_lemma_target_calls(NLA_VARMAP, coarse=False)
    report = build_target_report(calls, top=10)
    obj = json.loads(render_target_json(report, show=True))
    assert obj["lemmas"] == 3
    assert obj["unique_targets"] == 2
    top = obj["top_targets"][0]
    assert "count" in top and "fingerprint" in top
    assert "strategies" in top and isinstance(top["strategies"], dict)
    assert "target_text" in top
    # show=False drops target_text but keeps the rest.
    obj2 = json.loads(render_target_json(report, show=False))
    assert "target_text" not in obj2["top_targets"][0]


def test_render_target_plain_handles_empty():
    out = render_target_plain(build_target_report([], top=10))
    assert "no ~lemma_builder" in out
