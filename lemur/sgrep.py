"""
Structural search over SMT2 ASTs via the z3 Python API.

`lemur sgrep` enumerates terms in a parsed goal (or post-tactic goal) that
match a small s-expression pattern DSL:

    _                    wildcard (no capture)
    ?name                capture (same name twice ⇒ id-equality unification)
    (head c1 c2 ...)     compound: top-op decl name == head, arity matches,
                         each child matches its subpattern

The matcher walks z3's AST DAG with a `seen` set so each shared subterm is
visited once. Let-bindings in the source are eliminated by z3's parser, so
no manual let-context tracking is needed.

Phase-1 scope: no type filters (`?c:Bool`), no negation (`!?n:Numeral`),
no `--show context N`. These are deferred per the request file.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable


def _import_z3():
    try:
        import z3

        return z3
    except ImportError:
        raise SystemExit(
            "lemur sgrep requires the z3-solver Python package.\n"
            "From the Lemur checkout, install with:  uv sync --extra split"
        )


# --- Pattern AST -------------------------------------------------------------


_TYPE_FILTERS = ("Bool", "Numeral", "Var", "Expr", "Eq", "Comparison")


@dataclass
class PWild:
    type_filter: str | None = None  # None = no filter; 'Expr' is also no-op
    negate: bool = False  # invert the type-filter result


@dataclass
class PCapture:
    name: str
    type_filter: str | None = None
    negate: bool = False


@dataclass
class PCompound:
    head: str
    children: list  # list[PNode]


@dataclass
class PNumeral:
    """Match a numeric literal by value. z3 IntVal/BV-value/rational
    decl names are sort names ('Int', 'bv', 'Real') rather than the
    value itself, so a bare numeric token in the pattern can't be
    handled as a 0-arity PCompound — needs a value-comparison match."""

    value: int


PNode = PWild | PCapture | PCompound | PNumeral


# --- Pattern parser ----------------------------------------------------------


class PatternError(ValueError):
    pass


_TOKEN_RE = re.compile(r"\(|\)|[^()\s]+|\s+")


def _tokenize(s: str) -> list[str]:
    out: list[str] = []
    for m in _TOKEN_RE.finditer(s):
        tok = m.group(0)
        if not tok.isspace():
            out.append(tok)
    return out


def _parse_atom(tok: str) -> PNode:
    """Parse a non-paren leaf token into PWild / PCapture / PCompound.

    Recognized forms (where TYPE ∈ {Bool, Numeral, Var, Expr}):
        _                wildcard, no filter
        _:TYPE           wildcard with positive type filter
        _:!TYPE          wildcard with negative type filter
        !_:TYPE          equivalent prefix-negation form
        ?name            capture, no filter
        ?name:TYPE       capture with positive type filter
        ?name:!TYPE      capture with negative type filter (inner `!`)
        !?name:TYPE      capture with negative type filter (prefix `!`)
        NAME             bare literal symbol — match a 0-arity expr with
                         decl name == NAME (e.g. POW2_64).

    Both negation surface forms are accepted; if both are present the
    flags XOR (so `!?x:!Numeral` ≡ `?x:Numeral` — double-negation cancels).
    """
    s = tok
    outer_neg = False
    if s.startswith("!"):
        outer_neg = True
        s = s[1:]
        if not s:
            raise PatternError(f"`!` with nothing to negate: {tok!r}")

    # Capture or wildcard?
    if s.startswith("?"):
        body = s[1:]
        name, type_filter, inner_neg = _split_type_part(body, tok)
        if not name:
            raise PatternError(f"empty capture name: {tok!r}")
        return PCapture(
            name=name, type_filter=type_filter, negate=outer_neg ^ inner_neg
        )
    if s == "_" or s.startswith("_:"):
        body = "" if s == "_" else s[2:]
        name, type_filter, inner_neg = _split_type_part(
            "_" + (":" + body if body else ""), tok
        )
        # name is '_' here; ignore. Build wildcard.
        return PWild(type_filter=type_filter, negate=outer_neg ^ inner_neg)

    # Bare literal symbol — outer negation is meaningless.
    if outer_neg:
        raise PatternError(f"`!` only applies to wildcards/captures: {tok!r}")
    if ":" in s:
        raise PatternError(f"type filter `:` not allowed on literal name: {tok!r}")
    if _is_int_token(s):
        return PNumeral(value=int(s))
    return PCompound(head=s, children=[])


def _is_int_token(s: str) -> bool:
    if not s:
        return False
    if s[0] == "-":
        return len(s) > 1 and s[1:].isdigit()
    return s.isdigit()


def _split_type_part(body: str, orig_tok: str) -> tuple[str, str | None, bool]:
    """For a body like `name`, `name:Type`, `name:!Type`, `_`, `_:Type`,
    `_:!Type`, return (name, type_filter_or_None, inner_negate).

    The body has already had the outer `!` and the leading `?` (if any)
    stripped. For wildcards, body starts with `_`.
    """
    if ":" not in body:
        return body, None, False
    name, _, type_part = body.partition(":")
    inner_neg = False
    if type_part.startswith("!"):
        inner_neg = True
        type_part = type_part[1:]
    if type_part not in _TYPE_FILTERS:
        raise PatternError(
            f"unknown type filter {type_part!r} in {orig_tok!r}; "
            f"expected one of {', '.join(_TYPE_FILTERS)}"
        )
    # Treat 'Expr' as 'no filter' (it's the documented default).
    tf = None if type_part == "Expr" else type_part
    return name, tf, inner_neg


def parse_pattern(s: str) -> PNode:
    """Parse a pattern string into a PNode. Raises PatternError on malformed
    input."""
    toks = _tokenize(s)
    if not toks:
        raise PatternError("empty pattern")
    pos = [0]

    def parse() -> PNode:
        if pos[0] >= len(toks):
            raise PatternError("unexpected end of pattern")
        t = toks[pos[0]]
        pos[0] += 1
        if t == "(":
            if pos[0] >= len(toks):
                raise PatternError("missing head after '('")
            head = toks[pos[0]]
            if (
                head in ("(", ")")
                or head.startswith("?")
                or head == "_"
                or head.startswith("!")
                or ":" in head
            ):
                raise PatternError(
                    f"compound head must be a literal symbol, got {head!r}"
                )
            pos[0] += 1
            children: list[PNode] = []
            while pos[0] < len(toks) and toks[pos[0]] != ")":
                children.append(parse())
            if pos[0] >= len(toks):
                raise PatternError(f"unclosed '(' at head {head!r}")
            pos[0] += 1  # consume ')'
            return PCompound(head=head, children=children)
        if t == ")":
            raise PatternError("unexpected ')'")
        return _parse_atom(t)

    node = parse()
    if pos[0] != len(toks):
        raise PatternError(f"trailing tokens after pattern: {' '.join(toks[pos[0] :])}")
    return node


# --- Matcher -----------------------------------------------------------------


# z3's decl().name() reports `if` for SMT-LIB `(ite ...)`. Other operators
# (and, or, =, +, -, *, div, mod, <, <=, >=, =>, ...) match SMT-LIB names
# directly. Add aliases here as more crop up; keep narrow.
_HEAD_ALIASES = {
    "ite": "if",
}


def _check_type(z3, e, tf: str | None) -> bool:
    """Return True iff `e` satisfies the type filter `tf`. tf=None means
    no filter (always passes)."""
    if tf is None:
        return True
    if tf == "Bool":
        return e.sort().kind() == z3.Z3_BOOL_SORT
    if tf == "Numeral":
        return (
            z3.is_int_value(e)
            or z3.is_rational_value(e)
            or z3.is_bv_value(e)
            or z3.is_algebraic_value(e)
        )
    if tf == "Var":
        return (
            z3.is_app(e)
            and e.decl().arity() == 0
            and e.decl().kind() == z3.Z3_OP_UNINTERPRETED
        )
    if tf == "Eq":
        return z3.is_app(e) and e.decl().kind() == z3.Z3_OP_EQ
    if tf == "Comparison":
        return z3.is_app(e) and e.decl().kind() in (
            z3.Z3_OP_LT,
            z3.Z3_OP_LE,
            z3.Z3_OP_GT,
            z3.Z3_OP_GE,
        )
    raise AssertionError(f"unknown type filter: {tf!r}")


def _type_matches(z3, e, tf: str | None, negate: bool) -> bool:
    if tf is None:
        return True
    ok = _check_type(z3, e, tf)
    return (not ok) if negate else ok


def match(z3, p: PNode, e, env: dict[str, object]) -> bool:
    """Return True iff `e` matches `p`. Mutates `env` with capture bindings;
    captures unify by z3 expression id-equality."""
    if isinstance(p, PWild):
        return _type_matches(z3, e, p.type_filter, p.negate)
    if isinstance(p, PCapture):
        if not _type_matches(z3, e, p.type_filter, p.negate):
            return False
        prev = env.get(p.name)
        if prev is not None:
            return prev.get_id() == e.get_id()
        env[p.name] = e
        return True
    if isinstance(p, PCompound):
        if not z3.is_app(e):
            return False
        head = _HEAD_ALIASES.get(p.head, p.head)
        if e.decl().name() != head:
            return False
        if e.num_args() != len(p.children):
            return False
        for sub_p, j in zip(p.children, range(e.num_args())):
            if not match(z3, sub_p, e.arg(j), env):
                return False
        return True
    if isinstance(p, PNumeral):
        if z3.is_int_value(e) or z3.is_bv_value(e):
            return e.as_long() == p.value
        if z3.is_rational_value(e):
            try:
                return e.denominator_as_long() == 1 and e.numerator_as_long() == p.value
            except Exception:
                return False
        return False
    raise AssertionError(f"unknown pattern node: {p!r}")


# --- Walker ------------------------------------------------------------------


@dataclass
class Match:
    expr: object  # z3 ExprRef
    captures: dict[str, object]  # capture name -> z3 ExprRef


def find_matches(z3, p: PNode, roots: Iterable) -> list[Match]:
    """Walk all subterms of every root, collect matches. DAG-dedup by id so
    each shared subterm is matched at most once."""
    out: list[Match] = []
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        e = stack.pop()
        eid = e.get_id()
        if eid in seen:
            continue
        seen.add(eid)
        env: dict[str, object] = {}
        if match(z3, p, e, env):
            out.append(Match(expr=e, captures=env))
        if z3.is_app(e):
            for j in range(e.num_args()):
                stack.append(e.arg(j))
    return out


# --- Tactic string parser ----------------------------------------------------


class TacticParseError(ValueError):
    pass


def parse_tactic(z3, s: str):
    """Build a z3.Tactic from a string. Phase-1 grammar:

        TACTIC := NAME | '(' 'then' NAME NAME ... ')'

    Atomic name maps to `Tactic(name)`; `(then a b c)` maps to `Then(Tactic(a),
    Tactic(b), Tactic(c))`. Other combinators (or-else, repeat, par-then, ...)
    are reserved for future versions and rejected here.
    """
    s = s.strip()
    if not s:
        raise TacticParseError("empty tactic string")
    toks = _tokenize(s)
    if toks and toks[0] != "(":
        if len(toks) != 1 or toks[0] in ("(", ")"):
            raise TacticParseError(f"unexpected tokens: {toks!r}")
        return z3.Tactic(toks[0])
    # Expect (then NAME NAME ...)
    if len(toks) < 4 or toks[0] != "(" or toks[-1] != ")":
        raise TacticParseError(f"expected NAME or '(then NAME ...)', got: {s!r}")
    if toks[1] != "then":
        raise TacticParseError(
            f"only `then` combinator supported in v1, got {toks[1]!r}"
        )
    names = toks[2:-1]
    if not names:
        raise TacticParseError("`(then ...)` needs at least one tactic")
    for n in names:
        if n in ("(", ")"):
            raise TacticParseError(f"nested combinators not supported in v1: {s!r}")
    if len(names) == 1:
        return z3.Tactic(names[0])
    return z3.Then(*[z3.Tactic(n) for n in names])


# --- Goal helpers ------------------------------------------------------------


def parse_smt2_to_goal(z3, src_path: str):
    """Parse an SMT2 file into a fresh z3 Goal."""
    asserts = z3.parse_smt2_file(src_path)
    g = z3.Goal()
    for a in asserts:
        g.add(a)
    return g


def apply_tactic_to_goal(z3, goal, tactic):
    """Apply a tactic, return a single Goal. Multi-subgoal output collapses
    to the first subgoal with a warning printed to stderr (matches the
    `_presimplify` policy).

    The intermediate ApplyResult holds native z3 refs and would otherwise
    survive in a local frame until process exit, surfacing as
    'Uncollected memory' warnings on debug builds. Drop it explicitly."""
    import sys

    r = tactic.apply(goal)
    try:
        n = len(r)
        if n == 0:
            return z3.Goal()
        if n > 1:
            print(
                f"[sgrep] warning: tactic produced {n} subgoals; using subgoal[0]",
                file=sys.stderr,
            )
        sg = r[0]
        g = z3.Goal()
        for j in range(sg.size()):
            g.add(sg[j])
        return g
    finally:
        del r


def goal_top_level_exprs(goal) -> list:
    return [goal[i] for i in range(goal.size())]


def describe_kind(z3, e) -> str:
    """One-line classification of a z3 expression for `--show kind`.
    Goal is a *summary* of compound captures so `--distinct` over deeply
    nested guards stays consumable: full subtree printing was the W2
    pain point in the v2 feedback. For atomic forms the description
    embeds the value or name; for compounds we just emit the head op."""
    if z3.is_int_value(e) or z3.is_bv_value(e):
        return f"Numeral({e.as_long()})"
    if z3.is_rational_value(e):
        return f"Numeral({e})"
    if z3.is_app(e):
        d = e.decl()
        if d.arity() == 0:
            kind = d.kind()
            if kind == z3.Z3_OP_UNINTERPRETED:
                return f"Var({d.name()})"
            if kind == z3.Z3_OP_TRUE:
                return "True"
            if kind == z3.Z3_OP_FALSE:
                return "False"
        # Wrap compound heads — bare `=` or `*` would collide with the
        # `?c=KIND` display separator and read as `?c==` / `?c=*`.
        return f"Op({d.name()})"
    return type(e).__name__


# --- Summary aggregator ------------------------------------------------------


@dataclass
class Summary:
    num_asserts: int = 0
    decls_by_sort: Counter = field(default_factory=Counter)
    top_ops: Counter = field(default_factory=Counter)
    shape_counts: dict[str, int] = field(default_factory=dict)
    max_depth: int = 0


_SUMMARY_SHAPES = [
    "(div ?a ?b)",
    "(mod ?a ?b)",
    "(ite ?c ?a ?b)",
    "(div (ite ?c ?a ?b) ?k)",
    "(mod (ite ?c ?a ?b) ?k)",
    "(* ?x (ite ?c ?a ?b))",
    "(* (ite ?c ?a ?b) ?x)",
]


def _max_depth(z3, e, memo: dict[int, int]) -> int:
    eid = e.get_id()
    if eid in memo:
        return memo[eid]
    if not z3.is_app(e) or e.num_args() == 0:
        memo[eid] = 0
        return 0
    d = 1 + max(_max_depth(z3, e.arg(j), memo) for j in range(e.num_args()))
    memo[eid] = d
    return d


def compute_summary(z3, goal) -> Summary:
    """Collect overview statistics for `goal`."""
    s = Summary(num_asserts=goal.size())
    seen: set[int] = set()
    stack = goal_top_level_exprs(goal)
    decls_seen: set[int] = set()
    while stack:
        e = stack.pop()
        eid = e.get_id()
        if eid in seen:
            continue
        seen.add(eid)
        if z3.is_app(e):
            d = e.decl()
            kind = d.kind()
            if d.arity() == 0:
                if kind == z3.Z3_OP_UNINTERPRETED:
                    did = d.get_id()
                    if did not in decls_seen:
                        decls_seen.add(did)
                        s.decls_by_sort[d.range().name()] += 1
                # ANUM/AGNUM (numeric literals) and TRUE/FALSE are constants
                # — not interesting as "top operators". Skip them.
            else:
                s.top_ops[d.name()] += 1
            for j in range(e.num_args()):
                stack.append(e.arg(j))

    # Distinct-shape counts.
    for spec in _SUMMARY_SHAPES:
        p = parse_pattern(spec)
        s.shape_counts[spec] = len(find_matches(z3, p, goal_top_level_exprs(goal)))

    # Max nesting depth across all top-level asserts.
    memo: dict[int, int] = {}
    for top in goal_top_level_exprs(goal):
        d = _max_depth(z3, top, memo)
        if d > s.max_depth:
            s.max_depth = d
    return s


# --- Pretty-printing ---------------------------------------------------------


def set_pp_aliases(z3, expand: bool) -> None:
    """Toggle z3's smt2 printer let-aliasing. `expand=True` inlines all
    shared subterms — beware exponential blowup on deeply-shared DAGs."""
    if expand:
        z3.set_param("pp.min_alias_size", 1_000_000_000)
    else:
        z3.set_param("pp.min_alias_size", 10)
