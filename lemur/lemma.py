"""
Analyzer for `~lemma_builder` trace entries.

Parses structured lemma information: strategy, preconditions, conclusion,
variable assignments (value/bounds/definition/root), and monomial detection.

Ported from CertoraTimeouts/trace_agents with adaptations for lemur's
TraceEntry format.
"""

import re
from dataclasses import dataclass
from typing import Iterator

from lemur.parsers import TraceEntry

# --- Models ---


@dataclass(frozen=True)
class VariableAssignment:
    """Structured data from the lemma assignment table."""

    name: str
    value: str | None
    bounds: str | None
    definition: str | None
    root: str | None
    is_basic: bool = False


@dataclass(frozen=True)
class Precondition:
    """A lemma precondition like (344) j135 >= 16384."""

    index: int | None
    expression: str


@dataclass(frozen=True)
class Monomial:
    """A detected monomial assignment (variable with multiplication)."""

    variable: str
    expression: str


@dataclass(frozen=True)
class LemmaRecord:
    """Structured representation of a `~lemma_builder` entry."""

    entry: TraceEntry
    strategy: str
    lemma_id: int | None
    preconditions: list[Precondition]
    conclusion: str | None
    variables: list[VariableAssignment]
    monomials: list[Monomial]


# --- Regex patterns ---

_INTEGER_RE = re.compile(r"(\d+)$")
DEF_RE = re.compile(r"^(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*:=\s*(?P<expr>.+)$")
ASSIGN_RE = re.compile(r"^(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<rest>.+)$")
ROOT_RE = re.compile(r"^root\s*=\s*(?P<root>.+)$")
VAR_TOKEN_RE = re.compile(r"j\d+")
PRECONDITION_RE = re.compile(r"^\((?P<index>\d+)\)\s*(?P<expr>.+)$")


# --- Analyzer ---


class LemmaAnalyzer:
    """Extract structured lemma information from trace entries."""

    def __init__(self, entries: list[TraceEntry]):
        self._entries = entries

    def extract(self) -> Iterator[LemmaRecord]:
        for entry in self._entries:
            if entry.function != "~lemma_builder":
                continue
            yield self._to_record(entry)

    def _to_record(self, entry: TraceEntry) -> LemmaRecord:
        lines = entry.body_lines()
        if not lines:
            return LemmaRecord(
                entry=entry,
                strategy="",
                lemma_id=None,
                preconditions=[],
                conclusion=None,
                variables=[],
                monomials=[],
            )

        strategy_line = lines[0].strip()
        strategy, lemma_id = _parse_strategy(strategy_line)

        preconditions: list[Precondition] = []
        conclusion: str | None = None
        assignment_lines: list[str] = []

        for line in lines[1:]:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("==>") or stripped.startswith("=>"):
                conclusion = stripped.lstrip("=").lstrip(">").strip()
                continue
            if stripped.startswith("(") and conclusion is None:
                match = PRECONDITION_RE.match(stripped)
                if match:
                    preconditions.append(
                        Precondition(
                            index=int(match.group("index")),
                            expression=_normalize_space(match.group("expr")),
                        )
                    )
                else:
                    preconditions.append(
                        Precondition(
                            index=None,
                            expression=_normalize_space(stripped),
                        )
                    )
                continue
            assignment_lines.append(stripped)

        variables, monomials = _parse_variable_assignments(assignment_lines, conclusion)

        return LemmaRecord(
            entry=entry,
            strategy=strategy,
            lemma_id=lemma_id,
            preconditions=preconditions,
            conclusion=conclusion,
            variables=variables,
            monomials=monomials,
        )


def _parse_strategy(line: str) -> tuple[str, int | None]:
    line = line.strip()
    if not line:
        return "", None
    match = _INTEGER_RE.search(line)
    if match and match.start() > 0:
        strategy = line[: match.start()].strip()
        lemma_id = int(match.group(1))
        return strategy, lemma_id
    return line, None


def _parse_variable_assignments(
    lines: list[str],
    conclusion: str | None,
) -> tuple[list[VariableAssignment], list[Monomial]]:

    def _fresh_info() -> dict:
        return {
            "definition": None,
            "value": None,
            "bounds": None,
            "root": None,
            "basic": None,
        }

    data: dict[str, dict] = {}
    order: list[str] = []
    current_var: str | None = None

    for line in lines:
        def_match = DEF_RE.match(line)
        if def_match:
            var = def_match.group("var")
            expr = def_match.group("expr").strip()
            info = data.setdefault(var, _fresh_info())
            if info["definition"] is None:
                info["definition"] = _normalize_space(expr)
            current_var = var
            if var not in order:
                order.append(var)
            continue

        root_match = ROOT_RE.match(line)
        if root_match and current_var:
            root_val = root_match.group("root").strip()
            info = data.setdefault(current_var, _fresh_info())
            if info["root"] is None:
                info["root"] = _normalize_space(root_val)
            continue

        assign_match = ASSIGN_RE.match(line)
        if assign_match:
            var = assign_match.group("var")
            rest = assign_match.group("rest").strip()
            info = data.setdefault(var, _fresh_info())
            value, bounds, trailing_def, is_basic = _split_value_bounds(rest)
            if info["value"] is None:
                info["value"] = value
            if info["bounds"] is None:
                info["bounds"] = bounds
            if trailing_def and info["definition"] is None:
                info["definition"] = trailing_def
            if is_basic:
                info["basic"] = True
            elif info["basic"] is None:
                info["basic"] = False
            current_var = var
            if var not in order:
                order.append(var)
            continue

        # Fallback: attach to current variable
        if current_var:
            info = data.setdefault(current_var, _fresh_info())
            if info["definition"] is None:
                info["definition"] = _normalize_space(line)

    assignments: list[VariableAssignment] = []
    for var in order:
        details = data.get(var, {})
        assignments.append(
            VariableAssignment(
                name=var,
                value=details.get("value"),
                bounds=details.get("bounds"),
                definition=details.get("definition"),
                root=details.get("root"),
                is_basic=bool(details.get("basic")),
            )
        )

    # Detect monomials (variables with * in definition)
    conclusion_vars = set(VAR_TOKEN_RE.findall(conclusion or ""))
    prioritized: list[Monomial] = []
    others: list[Monomial] = []

    for assignment in assignments:
        definition = assignment.definition or ""
        if "*" not in definition:
            continue
        monomial = Monomial(variable=assignment.name, expression=definition)
        if conclusion_vars and assignment.name in conclusion_vars:
            prioritized.append(monomial)
        else:
            others.append(monomial)

    monomials = prioritized + others
    return assignments, monomials


def _split_value_bounds(rest: str) -> tuple[str | None, str | None, str | None, bool]:
    """Parse 'value  base  [lo, hi]  var := expr' from assignment RHS."""
    bounds = None
    trailing_def = None
    is_basic = False

    bounds_start = rest.find("[")
    trailing = ""
    if bounds_start != -1:
        bounds_end = rest.find("]", bounds_start)
        if bounds_end != -1:
            bounds = rest[bounds_start : bounds_end + 1].strip()
            trailing = rest[bounds_end + 1 :].strip()
            rest = rest[:bounds_start].strip()
    value = _normalize_space(rest) or None

    if value:
        tokens = value.split()
        if tokens and tokens[-1].lower() == "base":
            is_basic = True
            tokens = tokens[:-1]
            value = " ".join(tokens) or None

    if trailing:
        normalized_trailing = _normalize_space(trailing)
        if normalized_trailing.startswith(":="):
            trailing_def = normalized_trailing[2:].strip() or None
        else:
            idx = normalized_trailing.find(":=")
            if idx != -1:
                trailing_def = normalized_trailing[idx + 2 :].strip() or None

    return value, bounds, trailing_def, is_basic


def _normalize_space(text: str) -> str:
    return " ".join(text.split())
