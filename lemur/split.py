"""
Auto-discover Boolean case-splits in an SMT2 benchmark.

Parses the benchmark via the z3 Python API, enumerates candidate Bool
predicates (reachability symbols matching `BLK__\\d+` + ITE guards),
probe-scores each via a short simplification tactic, and greedily picks a
plan of up to `log2(max_leaves)` splits. Leaves and a `plan.json` manifest
are emitted via `emit_leaves`.

Design notes:

- z3-solver is an OPTIONAL dependency declared under the `[split]` extra.
  All `import z3` calls happen inside functions so that `import lemur.split`
  does not require z3 at module load; `_import_z3()` raises a helpful
  SystemExit if it's missing.

- Probes are strictly syntactic + simplification — no solver calls. This
  keeps planning fast and bounded by `--split-probe-timeout`.

- The z3 Python API may be a different build than the z3 binary used by
  `lemur sweep` to actually solve leaves. That is FINE: split only
  manipulates formulas (parse, simplify, emit SMT2). The solver of record
  is the binary that re-parses the leaves.
"""

from __future__ import annotations

import gc
import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


LEMUR_VERSION = "0.1.0"
PLAN_VERSION = 1


class SplitError(RuntimeError):
    """Expected error during splitting (missing plan.json, invalid SMT, etc.)."""


def _import_z3():
    try:
        import z3
        return z3
    except ImportError:
        raise SystemExit(
            "lemur split requires the z3-solver Python package.\n"
            "From the Lemur checkout, install with:  uv sync --extra split"
        )


# --- Data model --------------------------------------------------------------


@dataclass
class Candidate:
    name: str
    score: float
    reduces_to_false_on: str | None  # "true" | "false" | None
    probe_ms: dict[str, float]       # {"true": ms, "false": ms}


@dataclass
class LeafSpec:
    valuation: dict[str, bool]       # {"BLK__25": true, "TB0": false, ...}
    file: str | None                 # leaf_T_F_T.smt2, None if pruned or plan_only
    pruned: bool
    reason: str | None


@dataclass
class Plan:
    source: str                      # basename of the source file as copied in
    source_abs: str                  # absolute path at emission time
    split_predicates: list[Candidate]
    leaves: list[LeafSpec]
    version: int = PLAN_VERSION
    lemur_version: str = LEMUR_VERSION
    results: dict | None = None      # reserved for Phase 2 sweep annotation
    implied_units: dict[str, bool] = field(default_factory=dict)
    # ^ Booleans the formula already forces, discovered during candidate
    #   scoring (one-sided collapse). Recorded so downstream tools can see
    #   which predicates were detected as redundant; not picked as splits.


# --- Scoring -----------------------------------------------------------------


@dataclass
class _Metrics:
    num_exprs: int = 0
    num_ites: int = 0
    num_divs: int = 0
    num_mods: int = 0
    num_nla_ite_terms: int = 0   # (div|mod|*) with at least one ITE child
    arith_max_deg: int = 0
    is_qflia: bool = False


def _walk_subgoal(z3, sg, seen: set, counters: list):
    """DAG-walk a single subgoal, accumulating structural counts in counters.
    `seen` and `counters` are mutated in place so callers can share them across
    subgoals without building giant intermediate expression lists.

    counters = [num_ites, num_divs, num_mods, num_nla_ite_terms].

    `num_nla_ite_terms` counts each term whose top-level op is div/mod/* AND
    at least one immediate child is an ITE — these are the shapes NLA solvers
    pay extra for (each ITE-wrapped factor becomes a fresh LP variable)."""
    nla_ops = (z3.Z3_OP_IDIV, z3.Z3_OP_MOD, z3.Z3_OP_MUL)
    for i in range(sg.size()):
        stack = [sg[i]]
        while stack:
            e = stack.pop()
            eid = e.get_id()
            if eid in seen:
                continue
            seen.add(eid)
            if z3.is_app(e):
                k = e.decl().kind()
                if k == z3.Z3_OP_ITE:
                    counters[0] += 1
                elif k == z3.Z3_OP_IDIV:
                    counters[1] += 1
                elif k == z3.Z3_OP_MOD:
                    counters[2] += 1
                if k in nla_ops:
                    for j in range(e.num_args()):
                        ch = e.arg(j)
                        if z3.is_app(ch) and ch.decl().kind() == z3.Z3_OP_ITE:
                            counters[3] += 1
                            break
                for j in range(e.num_args()):
                    stack.append(e.arg(j))


def _measure_apply_result(z3, result, probes: dict) -> _Metrics:
    """Sum-aggregate metrics across all subgoals of an ApplyResult.

    Takes cached probes so we don't re-construct them on every call (each
    `z3.Probe(name)` allocates a native object)."""
    m = _Metrics()
    seen: set = set()
    counters = [0, 0, 0, 0]  # ites, divs, mods, nla_ite_terms
    n = len(result)
    for i in range(n):
        sg = result[i]
        try:
            m.num_exprs += int(probes['num-exprs'](sg))
        except Exception:
            pass
        try:
            deg = int(probes['arith-max-deg'](sg))
            if deg > m.arith_max_deg:
                m.arith_max_deg = deg
        except Exception:
            pass
        try:
            m.is_qflia = m.is_qflia or bool(probes['is-qflia'](sg))
        except Exception:
            pass
        _walk_subgoal(z3, sg, seen, counters)
    m.num_ites, m.num_divs, m.num_mods, m.num_nla_ite_terms = counters
    return m


def _gain(base: _Metrics, leaf: _Metrics, *, nla_ite_weight: float = 5.0) -> float:
    return (
        0.1 * (base.num_exprs - leaf.num_exprs)
        + 5.0 * (base.num_ites - leaf.num_ites)
        + 3.0 * (base.arith_max_deg - leaf.arith_max_deg)
        + 2.0 * (base.num_divs - leaf.num_divs)
        + 2.0 * (base.num_mods - leaf.num_mods)
        + nla_ite_weight * (base.num_nla_ite_terms - leaf.num_nla_ite_terms)
        + (50.0 if leaf.is_qflia and not base.is_qflia else 0.0)
    )


# --- Candidate enumeration ---------------------------------------------------


def _collect_bool_consts(z3, exprs):
    """Return {name: BoolRef} for all declared 0-ary Bool symbols that appear
    as constants in any of `exprs` (DAG-walk)."""
    out: dict[str, object] = {}
    seen = set()
    stack = list(exprs)
    while stack:
        e = stack.pop()
        eid = e.get_id()
        if eid in seen:
            continue
        seen.add(eid)
        if z3.is_const(e) and e.sort().kind() == z3.Z3_BOOL_SORT:
            if not z3.is_true(e) and not z3.is_false(e):
                name = e.decl().name()
                if name not in out:
                    out[name] = e
        if z3.is_app(e):
            for i in range(e.num_args()):
                stack.append(e.arg(i))
    return out


def _collect_ite_guards(z3, exprs):
    """Return set of Bool-constant names used as ITE guards in `exprs`.

    Only raw declared Bool symbols count (no compound guards)."""
    names: set[str] = set()
    seen = set()
    stack = list(exprs)
    while stack:
        e = stack.pop()
        eid = e.get_id()
        if eid in seen:
            continue
        seen.add(eid)
        if z3.is_app(e):
            if e.decl().kind() == z3.Z3_OP_ITE and e.num_args() == 3:
                guard, then_br, else_br = e.arg(0), e.arg(1), e.arg(2)
                if (z3.is_const(guard)
                        and guard.sort().kind() == z3.Z3_BOOL_SORT
                        and not z3.is_true(guard) and not z3.is_false(guard)
                        and then_br.get_id() != else_br.get_id()):
                    names.add(guard.decl().name())
            for i in range(e.num_args()):
                stack.append(e.arg(i))
    return names


def _collect_fixed_bools(z3, goal) -> dict[str, bool]:
    """Return {name: value} for Bool constants pinned by a top-level unit
    assertion. Matches exactly `(assert B)` → True and `(assert (not B))` →
    False. Guards against re-splitting files that `lemur split` has already
    cut: the injected `(assert B)` / `(assert (not B))` lines would otherwise
    make B look like a high-scoring split (one branch collapses trivially)
    when in fact half the leaves would be redundant and the other half pruned.
    """
    fixed: dict[str, bool] = {}
    for i in range(goal.size()):
        e = goal[i]
        if (z3.is_const(e)
                and e.sort().kind() == z3.Z3_BOOL_SORT
                and not z3.is_true(e) and not z3.is_false(e)):
            fixed[e.decl().name()] = True
        elif z3.is_not(e):
            inner = e.arg(0)
            if (z3.is_const(inner)
                    and inner.sort().kind() == z3.Z3_BOOL_SORT
                    and not z3.is_true(inner) and not z3.is_false(inner)):
                fixed[inner.decl().name()] = False
    return fixed


def _enumerate_candidates(z3, goal, name_pattern: str,
                          restrict_pattern: str | None = None) -> list[str]:
    """Return an ordered list of Bool candidate names.

    `name_pattern` classifies reachability-style names so they sort ahead of
    ITE guards. `restrict_pattern`, if set, is a hard allowlist: every
    candidate (reachability or guard) must match it.
    """
    asserts = [goal[i] for i in range(goal.size())]
    bool_consts = _collect_bool_consts(z3, asserts)
    ite_guards = _collect_ite_guards(z3, asserts)
    fixed = _collect_fixed_bools(z3, goal)

    pat = re.compile(name_pattern)
    restrict = re.compile(restrict_pattern) if restrict_pattern else None

    def _keep(n: str) -> bool:
        if n in fixed:
            return False
        if restrict is not None and not restrict.search(n):
            return False
        return True

    reachability = [n for n in bool_consts if pat.search(n) and _keep(n)]
    guards = [n for n in ite_guards if n not in reachability and _keep(n)]

    # Deterministic ordering: reachability first (sorted), then guards.
    return sorted(reachability) + sorted(guards)


# --- Tactics -----------------------------------------------------------------


def _simplify_tactic(z3, timeout_s: float):
    """Build the standard simplification tactic with a per-apply timeout."""
    t = z3.Then(
        z3.Tactic('simplify'),
        z3.Tactic('propagate-values'),
        z3.Tactic('solve-eqs'),
        z3.Tactic('elim-uncnstr'),
    )
    return z3.TryFor(t, int(timeout_s * 1000))


def _score_with_assumption(z3, goal, name: str, value: bool, tactic,
                           base_metrics: _Metrics, probes: dict,
                           *, nla_ite_weight: float = 5.0):
    """Clone `goal`, add `C=value`, apply `tactic`, compute the gain score.

    Returns (gain: float, collapsed: bool, elapsed_ms: float). The
    ApplyResult and temporary Goal are scoped to this function and dropped
    before return so z3 native refs don't accumulate across iterations."""
    g2 = z3.Goal()
    for i in range(goal.size()):
        g2.add(goal[i])
    g2.add(z3.Bool(name) if value else z3.Not(z3.Bool(name)))
    start = time.monotonic()
    try:
        r = tactic.apply(g2)
    except z3.Z3Exception:
        return 0.0, False, (time.monotonic() - start) * 1000
    elapsed_ms = (time.monotonic() - start) * 1000
    try:
        collapsed = any(r[i].inconsistent() for i in range(len(r)))
        if collapsed:
            # Caller branches on the bool flag; gain is unused for collapses.
            return 0.0, True, elapsed_ms
        leaf_metrics = _measure_apply_result(z3, r, probes)
    finally:
        # Explicit release — these hold native z3 refs we don't need anymore.
        del r
        del g2
    return _gain(base_metrics, leaf_metrics, nla_ite_weight=nla_ite_weight), False, elapsed_ms


def _apply_measure(z3, goal, tactic, probes: dict) -> _Metrics:
    """Apply `tactic` to `goal`, measure, and drop the ApplyResult before
    returning so no native ref leaks out."""
    try:
        r = tactic.apply(goal)
    except z3.Z3Exception:
        # Fall back to measuring the unsimplified goal.
        m = _Metrics()
        seen: set = set()
        counters = [0, 0, 0]
        try:
            m.num_exprs = int(probes['num-exprs'](goal))
        except Exception:
            pass
        try:
            m.arith_max_deg = int(probes['arith-max-deg'](goal))
        except Exception:
            pass
        try:
            m.is_qflia = bool(probes['is-qflia'](goal))
        except Exception:
            pass
        _walk_subgoal(z3, goal, seen, counters)
        m.num_ites, m.num_divs, m.num_mods = counters
        return m
    try:
        return _measure_apply_result(z3, r, probes)
    finally:
        del r


def _presimplify(z3, goal, tactic):
    """Apply `tactic` once to `goal`. Return (new_goal, status_dict).

    On success with a single resulting subgoal, return that subgoal as the
    new goal — Booleans resolved by propagate-values / solve-eqs are gone
    from it, so they won't appear as split candidates downstream.

    On multi-subgoal output (disjunctive split — uncommon for our chain) or
    on z3 exception, fall back to the original goal. `status` carries
    counters/flags for the caller to log."""
    status = {
        'asserts_in': goal.size(),
        'asserts_out': goal.size(),
        'subgoals': 1,
        'fallback': False,
        'reason': None,
        'elapsed_ms': 0.0,
    }
    t0 = time.monotonic()
    try:
        r = tactic.apply(goal)
    except z3.Z3Exception as e:
        status['fallback'] = True
        status['reason'] = f"tactic exception: {e}"
        status['elapsed_ms'] = (time.monotonic() - t0) * 1000
        return goal, status
    try:
        n_sub = len(r)
        status['subgoals'] = n_sub
        if n_sub != 1:
            status['fallback'] = True
            status['reason'] = f"{n_sub} subgoals"
            status['elapsed_ms'] = (time.monotonic() - t0) * 1000
            return goal, status
        sg = r[0]
        new_goal = z3.Goal()
        for j in range(sg.size()):
            new_goal.add(sg[j])
        status['asserts_out'] = new_goal.size()
        status['elapsed_ms'] = (time.monotonic() - t0) * 1000
        return new_goal, status
    finally:
        del r


def _is_pruned(z3, goal, valuation: dict, tactic) -> bool:
    """Return True if `goal ∧ valuation` simplifies to a false-collapsed goal.
    ApplyResult is strictly local so no refs leak."""
    g = z3.Goal()
    for i in range(goal.size()):
        g.add(goal[i])
    for name, v in valuation.items():
        g.add(z3.Bool(name) if v else z3.Not(z3.Bool(name)))
    try:
        r = tactic.apply(g)
    except Exception:
        return False
    try:
        return any(r[i].inconsistent() for i in range(len(r)))
    finally:
        del r
        del g


# --- Planning ----------------------------------------------------------------


def _ceil_log2(n: int) -> int:
    n = max(1, n)
    return (n - 1).bit_length()


def build_plan(
    src_path: str,
    *,
    max_leaves: int = 32,
    threshold: float = 10.0,
    probe_timeout: float = 5.0,
    name_pattern: str = r'BLK__\d+',
    restrict_pattern: str | None = None,
    nla_ite_weight: float = 5.0,
    log=None,
) -> Plan:
    """Parse `src_path`, enumerate candidates, score + greedy plan.

    `log(msg)` if provided is called with short progress messages; use
    `print` with `file=sys.stderr` to surface them to the user.

    `restrict_pattern`, if set, is a hard allowlist regex applied to every
    candidate name (reachability and ITE guards alike), on top of
    `name_pattern`. Use it to force a split on a specific predicate.

    `nla_ite_weight` is the score weight for the (div|mod|mul)-of-ITE
    shrinkage axis. Set to 0 to disable that axis."""
    z3 = _import_z3()
    try:
        if restrict_pattern is not None:
            re.compile(restrict_pattern)
    except re.error as e:
        raise SplitError(f"invalid --split-only regex {restrict_pattern!r}: {e}") from e
    timings: dict[str, float] = {}

    t0 = time.monotonic()
    try:
        asserts = z3.parse_smt2_file(src_path)
    except z3.Z3Exception as e:
        raise SplitError(f"failed to parse {src_path}: {e}") from e

    goal = z3.Goal()
    for a in asserts:
        goal.add(a)
    del asserts
    timings['parse'] = time.monotonic() - t0
    if log: log(f"parse: {timings['parse']*1000:.0f}ms ({goal.size()} asserts)")

    tactic = _simplify_tactic(z3, probe_timeout)
    probes = {
        'num-exprs': z3.Probe('num-exprs'),
        'arith-max-deg': z3.Probe('arith-max-deg'),
        'is-qflia': z3.Probe('is-qflia'),
    }

    # Pre-simplify the goal once. propagate-values + solve-eqs in the chain
    # eliminate Booleans pinned by syntactic units or by value/equality
    # propagation, so they don't enter candidate enumeration. A separate
    # tactic with a longer budget — the per-candidate `tactic` budget is
    # tighter than what we want here.
    presimplify_tactic = _simplify_tactic(z3, probe_timeout * 4)
    goal, ps = _presimplify(z3, goal, presimplify_tactic)
    timings['presimplify'] = ps['elapsed_ms'] / 1000
    if log:
        if ps['fallback']:
            log(f"presimplify: fallback to original goal ({ps['reason']}, "
                f"{ps['elapsed_ms']:.0f}ms)")
        else:
            log(f"presimplify: {ps['asserts_in']}→{ps['asserts_out']} asserts "
                f"({ps['elapsed_ms']:.0f}ms)")
    del presimplify_tactic

    t0 = time.monotonic()
    current_goal = goal
    base_metrics = _apply_measure(z3, current_goal, tactic, probes)
    timings['base-simplify'] = time.monotonic() - t0
    if log: log(f"base-simplify: {timings['base-simplify']*1000:.0f}ms "
                f"(exprs={base_metrics.num_exprs} ites={base_metrics.num_ites} "
                f"deg={base_metrics.arith_max_deg})")

    plan_cands: list[Candidate] = []
    picked_names: set[str] = set()
    implied_units: dict[str, bool] = {}
    max_splits = _ceil_log2(max_leaves)

    greedy_t0 = time.monotonic()
    picks_made = 0
    while picks_made < max_splits:
        t_iter = time.monotonic()
        names = [n for n in _enumerate_candidates(
                    z3, current_goal, name_pattern, restrict_pattern)
                 if n not in picked_names]
        if not names:
            if log: log(f"iter {picks_made+1}: no candidates, stopping")
            break
        if log: log(f"iter {picks_made+1}: scoring {len(names)} candidates ...")

        # Bucket each candidate as either an implication to harvest (one
        # side collapses) or a productive split (both sides alive).
        productive: list[tuple[str, float, dict[str, float], bool]] = []
        # tuple: (name, score, probe_ms, true_branch_is_harder)
        harvested: list[tuple[str, bool]] = []  # (name, forced_value)

        for n in names:
            gain_t, c_t, ms_t = _score_with_assumption(
                z3, current_goal, n, True, tactic, base_metrics, probes,
                nla_ite_weight=nla_ite_weight)
            gain_f, c_f, ms_f = _score_with_assumption(
                z3, current_goal, n, False, tactic, base_metrics, probes,
                nla_ite_weight=nla_ite_weight)

            if c_t and c_f:
                # Both branches contradictory — the conjunction so far is
                # unsat. Skip; don't pick.
                picked_names.add(n)
                continue
            if c_t:
                harvested.append((n, False))  # n=False is forced
                picked_names.add(n)
                continue
            if c_f:
                harvested.append((n, True))   # n=True is forced
                picked_names.add(n)
                continue

            # Both branches alive — genuine split candidate.
            score = min(gain_t, gain_f)
            harder_true = gain_t < gain_f
            productive.append((n, score,
                               {'true': round(ms_t, 1), 'false': round(ms_f, 1)},
                               harder_true))

        # If any implications were discovered, fold them in and re-enumerate
        # without consuming a max_splits slot. The re-enumeration may surface
        # NEW productive candidates (or new collapses) once units propagate.
        if harvested:
            if log:
                for n, v in harvested:
                    log(f"iter {picks_made+1}: harvested {n}={str(v).lower()} "
                        f"(one-sided collapse)")
            for n, v in harvested:
                implied_units[n] = v
            next_goal = z3.Goal()
            for i in range(current_goal.size()):
                next_goal.add(current_goal[i])
            for n, v in harvested:
                next_goal.add(z3.Bool(n) if v else z3.Not(z3.Bool(n)))
            current_goal = next_goal
            del next_goal
            base_metrics = _apply_measure(z3, current_goal, tactic, probes)
            del productive
            del harvested
            gc.collect()
            continue

        if not productive:
            if log: log(f"iter {picks_made+1}: no productive candidates, stopping")
            break

        # Sort by score desc; tiebreak by name for determinism.
        productive.sort(key=lambda s: (-s[1], s[0]))
        best_name, best_score, best_ms, best_harder = productive[0]

        if best_score < threshold:
            if log: log(f"iter {picks_made+1}: best score {best_score:.1f} < "
                        f"threshold {threshold}, stopping")
            break

        plan_cands.append(Candidate(
            name=best_name,
            score=round(best_score, 3),
            reduces_to_false_on=None,
            probe_ms=best_ms,
        ))
        picked_names.add(best_name)
        picks_made += 1

        if log:
            log(f"iter {picks_made}: picked {best_name} (score {best_score:.1f}) "
                f"in {(time.monotonic()-t_iter)*1000:.0f}ms")

        del productive
        del harvested
        gc.collect()

        # Step context: assume the harder branch, re-measure base.
        # Fresh-then-del idiom keeps only one reference to the new Goal in
        # this frame; otherwise the prior binding leaks at shutdown.
        next_goal = z3.Goal()
        for i in range(current_goal.size()):
            next_goal.add(current_goal[i])
        next_goal.add(z3.Bool(best_name) if best_harder else z3.Not(z3.Bool(best_name)))
        current_goal = next_goal
        del next_goal
        base_metrics = _apply_measure(z3, current_goal, tactic, probes)
    timings['greedy'] = time.monotonic() - greedy_t0

    # --- Build leaf manifest (2^k valuations over the chosen plan) ---
    t0 = time.monotonic()
    leaves = _build_leaf_specs(z3, goal, plan_cands, tactic, log=log)
    timings['leaves'] = time.monotonic() - t0

    plan = Plan(
        source=Path(src_path).name,
        source_abs=str(Path(src_path).resolve()),
        split_predicates=plan_cands,
        leaves=leaves,
        implied_units=implied_units,
    )

    if log:
        total_ms = sum(timings.values()) * 1000
        parts = "  ".join(f"{k}={v*1000:.0f}ms" for k, v in timings.items())
        log(f"done: total={total_ms:.0f}ms  ({parts})")

    # Explicitly release z3 native refs before z3's atexit handler fires.
    # Without this, stale refs in local frames can trigger "Uncollected memory"
    # warnings from z3 on shutdown (esp. in debug builds with
    # Z3_DEBUG_REF_COUNTER). Multi-pass gc catches cycles involving Python
    # wrappers around native objects.
    del current_goal
    del goal
    del tactic
    del probes
    del base_metrics
    for _ in range(3):
        gc.collect()

    return plan


def _build_leaf_specs(z3, original_goal, plan_cands: list[Candidate], tactic,
                      *, log=None) -> list[LeafSpec]:
    """For each valuation tuple, detect pruning. Emit LeafSpec list.

    Optimization: if a picked predicate P is known to reduce the goal to
    false when P=v0, then every valuation with P=v0 is trivially pruned
    without running the tactic. This typically eliminates the vast
    majority of tactic applies — on Certora VCs, 4 of 5 predicates tend
    to have collapsing sides."""
    k = len(plan_cands)
    specs: list[LeafSpec] = []
    if k == 0:
        return specs

    # Precompute each predicate's "collapse valuation" (the valuation that
    # simplifies to false for *this* predicate in isolation). A leaf's
    # valuation matching any such condition is doomed.
    collapse_on: list[bool | None] = []
    for cand in plan_cands:
        if cand.reduces_to_false_on == 'true':
            collapse_on.append(True)
        elif cand.reduces_to_false_on == 'false':
            collapse_on.append(False)
        else:
            collapse_on.append(None)

    tactic_calls = 0
    skipped = 0

    for bits in range(1 << k):
        valuation: dict[str, bool] = {}
        label_parts: list[str] = []
        for i, cand in enumerate(plan_cands):
            v = bool(bits & (1 << i))
            valuation[cand.name] = v
            label_parts.append('T' if v else 'F')
        label = '_'.join(label_parts)

        # Fast path: any predicate known to collapse on `v` dooms this leaf.
        pruned = False
        for i, c_on in enumerate(collapse_on):
            if c_on is not None and label_parts[i] == ('T' if c_on else 'F'):
                pruned = True
                skipped += 1
                break

        if not pruned:
            pruned = _is_pruned(z3, original_goal, valuation, tactic)
            tactic_calls += 1
            if tactic_calls % 8 == 0:
                gc.collect()

        specs.append(LeafSpec(
            valuation=valuation,
            file=None if pruned else f"leaf_{label}.smt2",
            pruned=pruned,
            reason="reduces to false" if pruned else None,
        ))

    if log:
        log(f"leaves: {1 << k} enumerated; {skipped} pruned by "
            f"predicate-collapse fast-path, {tactic_calls} tactic checks ran")

    return specs


# --- Emission ----------------------------------------------------------------


def emit_leaves(plan: Plan, src_path: str, out_dir: str, plan_only: bool = False) -> None:
    """Copy source, write plan.json, emit non-pruned leaves as SMT2 files."""
    from lemur.smt_inject import make_split_smt

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Copy source verbatim into the output directory.
    shutil.copy2(src_path, out / plan.source)

    if plan_only:
        # Strip filenames; files don't exist on disk.
        for leaf in plan.leaves:
            if not leaf.pruned:
                leaf.file = None
    else:
        for leaf in plan.leaves:
            if leaf.pruned or leaf.file is None:
                continue
            injection_lines = []
            for cand in plan.split_predicates:
                v = leaf.valuation[cand.name]
                if v:
                    injection_lines.append(f"(assert {cand.name})")
                else:
                    injection_lines.append(f"(assert (not {cand.name}))")
            injection = '\n'.join(injection_lines)
            make_split_smt(src_path, injection, str(out / leaf.file))

    (out / 'plan.json').write_text(_plan_to_json(plan))


# --- plan.json (de)serialization --------------------------------------------


def _plan_to_json(plan: Plan) -> str:
    d = {
        "version": plan.version,
        "lemur_version": plan.lemur_version,
        "source": plan.source,
        "source_abs": plan.source_abs,
        "split_predicates": [asdict(c) for c in plan.split_predicates],
        "leaves": [asdict(l) for l in plan.leaves],
        "results": plan.results,
        "implied_units": plan.implied_units,
    }
    return json.dumps(d, indent=2) + "\n"


def read_plan(path_or_dir: str) -> Plan:
    """Read a plan.json from a directory or a direct file path."""
    p = Path(path_or_dir)
    if p.is_dir():
        p = p / 'plan.json'
    if not p.exists():
        raise SplitError(f"plan.json not found at {p}")
    try:
        d = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise SplitError(f"cannot read plan.json at {p}: {e}") from e
    if not isinstance(d, dict):
        raise SplitError(f"{p}: plan.json root is not an object")
    ver = d.get('version')
    if ver is not None and int(ver) > PLAN_VERSION:
        raise SplitError(
            f"{p}: plan.json version {ver} is newer than this lemur supports "
            f"(max {PLAN_VERSION}). Upgrade lemur."
        )
    cands = [Candidate(**c) for c in d.get('split_predicates', [])]
    leaves = [LeafSpec(**l) for l in d.get('leaves', [])]
    return Plan(
        source=d.get('source', ''),
        source_abs=d.get('source_abs', ''),
        split_predicates=cands,
        leaves=leaves,
        version=d.get('version', PLAN_VERSION),
        lemur_version=d.get('lemur_version', LEMUR_VERSION),
        results=d.get('results'),
        implied_units=d.get('implied_units') or {},
    )
