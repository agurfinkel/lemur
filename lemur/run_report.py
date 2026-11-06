"""Evidence-based summaries of Z3 stdout, verbose progress, and text traces.

Verbose formats verified against Z3 e8b9320: tactic/tactic.cpp,
sat/sat_solver.cpp, and smt/smt_context_pp.cpp. Unknown shapes stay visible.
Reported scope durations can overlap; SAT elapsed time is search-local.
"""

import re
from collections import Counter
from pathlib import Path

from lemur.trace_stream import trace_events
from lemur.z3_stats import parse_z3_stats

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
KV = re.compile(rf":([\w.-]+)\s+({NUMBER}|true|false)(?=\s|\)|$)")
ROW = re.compile(r"^\((sat|smt)\.stats\s+(.+)\)$")
PHASE = re.compile(r"^\(([\w.-]+)\s+(:.+)\)$")
RESULT = re.compile(r"^(sat|unsat|unknown|timeout)\s*$")


def value(text):
    if text in ("true", "false"):
        return text == "true"
    try:
        return int(text)
    except ValueError:
        return float(text)


def fields(text):
    return {key: value(val) for key, val in KV.findall(text)}


def progress(engine, body):
    """Recognize verified numeric layouts; never guess unfamiliar columns."""
    parts = body.split()
    lengths = (7, 9) if engine == "sat" else (8, 9)
    if len(parts) not in lengths:
        return None
    try:
        if engine == "sat":
            conflicts, decisions, restarts = map(int, parts[:3])
            given, binary = map(int, parts[3].split("/"))
            learned, learned_binary = map(int, parts[4].split("/"))
            data = dict(
                conflicts=conflicts,
                decisions=decisions,
                restarts=restarts,
                given_clauses=given,
                given_binary_clauses=binary,
                learned_clauses=learned,
                learned_binary_clauses=learned_binary,
                base_units=int(parts[5]),
                gc_clauses=int(parts[6]),
            )
            if len(parts) == 9:
                data.update(
                    memory_mib=float(parts[7]), search_elapsed_s=float(parts[8])
                )
        else:
            restarts, conflicts, decisions, propagations = map(int, parts[:4])
            clauses, binary, units = map(int, parts[4].split("/"))
            lemmas, binary_lemmas = map(int, parts[5].split("/"))
            data = dict(
                restarts=restarts,
                conflicts=conflicts,
                decisions=decisions,
                propagations=propagations,
                auxiliary_and_binary_clauses=clauses,
                binary_clauses=binary,
                base_units=units,
                lemmas=lemmas,
                binary_lemmas=binary_lemmas,
                simplifications=int(parts[6]),
                deleted_clauses=int(parts[7]),
            )
            if len(parts) == 9:
                data["memory_mib"] = float(parts[8])
        return data
    except ValueError:
        return None


def parse_verbose(text):
    """Return line-located evidence, including unrecognized/partial records."""
    records = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        start = i
        raw = lines[i]
        stripped = raw.strip()
        i += 1
        if not stripped:
            continue
        record = {"line_start": start + 1, "line_end": i, "raw": raw}
        if stripped == "(sat-status":
            block = [raw]
            while i < len(lines) and lines[i].lstrip().startswith(":"):
                block.append(lines[i])
                i += 1
                if lines[i - 1].rstrip().endswith(")"):
                    break
            joined = "\n".join(block)
            record.update(
                kind="circuit",
                engine="sat",
                complete=joined.rstrip().endswith(")"),
                fields=fields(joined),
                line_end=i,
                raw=joined,
            )
        elif stripped in ("(sat.solver)", "(smt.searching)", "(smt.tactic start)"):
            record.update(
                kind="engine",
                engine="sat" if stripped.startswith("(sat.") else "smt",
                event="tactic_start"
                if stripped == "(smt.tactic start)"
                else "search_start",
            )
        elif match := ROW.fullmatch(stripped):
            engine, body = match.groups()
            if body.startswith(":"):
                record.update(kind="header", engine=engine)
            elif (data := progress(engine, body)) is not None:
                record.update(kind="progress", engine=engine, fields=data)
            else:
                record.update(kind="unparsed", reason="unknown_progress_layout")
        elif (match := PHASE.fullmatch(stripped)) and fields(match[2]):
            record.update(kind="phase", name=match[1], fields=fields(match[2]))
        else:
            record.update(kind="unparsed")
        records.append(record)
    return records


def trace_summary(path):
    sites = Counter()
    kinds = Counter()
    incomplete = []
    size = 0
    with Path(path).open("rb") as source:
        for event in trace_events(source):
            kinds[event["kind"]] += 1
            size = event["span"]["byte_end"]
            if event["kind"] != "trace":
                continue
            sites[(event["tag"], event["function"], event["source_file"])] += 1
            if not event["complete"]:
                incomplete.append(
                    {key: event[key] for key in ("seq", "tag", "reason", "span")}
                )
    return {
        "bytes": size,
        "records": dict(kinds),
        "sites": [
            {"tag": tag, "function": fn, "source_file": src, "count": count}
            for (tag, fn, src), count in sites.most_common()
        ],
        "incomplete": incomplete,
    }


def build_report(stdout=None, stderr=None, trace=None):
    """Build a report without assuming absent counters imply absent activity."""
    out = Path(stdout).read_text(errors="replace") if stdout else ""
    err = Path(stderr).read_text(errors="replace") if stderr else ""
    results = [
        {"line": n, "result": line.strip()}
        for n, line in enumerate(out.splitlines(), 1)
        if RESULT.fullmatch(line.strip())
    ]
    verdict = results[-1]["result"] if results else None
    stats = parse_z3_stats(out)
    records = parse_verbose(err)
    engines = {}
    for record in records:
        if record["kind"] not in ("engine", "progress"):
            continue
        engine = record["engine"]
        state = engines.setdefault(
            engine,
            {
                "search_observed": False,
                "first_evidence_line": record["line_start"],
                "progress_rows": 0,
                "last_progress": None,
            },
        )
        if record["kind"] == "progress":
            state["progress_rows"] += 1
            state["last_progress"] = record
            state["search_observed"] = True
        elif record["event"] == "search_start":
            state["search_observed"] = True
    warnings = []
    if not results:
        warnings.append("No result token recorded; termination reason is unknown.")
    if len(results) > 1:
        warnings.append(
            "Multiple result tokens: verdict is the last; evidence spans the whole log."
        )
    if stats is None:
        warnings.append("No final statistics block recorded.")
    if engines.get("sat", {}).get("search_observed") and not any(
        key.startswith("sat-") for key in (stats or {})
    ):
        warnings.append(
            "SAT search is observed, but final SAT counters are unavailable. "
            "A hard timeout can expose an empty or stale SAT-tactic statistics cache."
        )
    unknown = sum(r["kind"] == "unparsed" for r in records)
    if any(r.get("reason") == "unknown_progress_layout" for r in records):
        warnings.append(
            "Unrecognized progress layout retained; no column meanings inferred."
        )
    trace_data = trace_summary(trace) if trace else None
    if trace_data and trace_data["incomplete"]:
        warnings.append(
            "Trace contains incomplete blocks; printing may have been interrupted."
        )
    return {
        "schema": 1,
        "inputs": {
            "stdout": str(stdout) if stdout else None,
            "stderr": str(stderr) if stderr else None,
            "trace": str(trace) if trace else None,
        },
        "verdict": verdict,
        "results": results,
        "statistics": stats,
        "engines": engines,
        "verbose": records,
        "unparsed_verbose_records": unknown,
        "trace": trace_data,
        "warnings": warnings,
        "semantics": {
            "phase_time": "Reported scope seconds; scopes may overlap and exit on exceptions. Do not sum.",
            "sat_time": "Elapsed since SAT init_search, including inprocessing; may reset across checks.",
            "smt_time": "SMT progress rows have no time column.",
            "memory": "Z3 tracked allocation MiB, not process RSS.",
            "counts": "Inventories may shrink; logs can span multiple checks. Missing means unknown, not zero.",
            "trace": "Event counts are not time measurements; tracing and expression printing perturb execution.",
        },
    }


def render_plain(report):
    lines = [f"Result: {report['verdict'] or 'not recorded'}"]
    for engine, data in report["engines"].items():
        observed = (
            "search observed" if data["search_observed"] else "tactic entry observed"
        )
        lines.append(
            f"Engine {engine.upper()}: {observed}; {data['progress_rows']} progress rows"
        )
        if last := data["last_progress"]:
            lines.append(
                f"  Last progress (stderr line {last['line_start']}): {last['fields']}"
            )
    if not report["engines"]:
        lines.append("Engine: not established by verbose evidence")
    circuits = [r for r in report["verbose"] if r["kind"] == "circuit"]
    if circuits:
        first = circuits[0]
        lines.append(
            f"First SAT circuit snapshot (complete={first['complete']}): {first['fields']}"
        )
    phases = [
        r for r in report["verbose"] if r["kind"] == "phase" and "time" in r["fields"]
    ]
    if phases:
        lines.append("Largest reported scope durations (may overlap; not additive):")
        for row in sorted(phases, key=lambda r: r["fields"]["time"], reverse=True)[:8]:
            lines.append(f"  line {row['line_start']} {row['name']}: {row['fields']}")
    important = {
        k: v
        for k, v in (report["statistics"] or {}).items()
        if k.startswith(("array-", "bv-"))
        or k in ("time", "total-time", "max-memory", "rlimit-count")
    }
    if important:
        lines.append(f"Final statistics (absent counters are unknown): {important}")
    if trace := report["trace"]:
        lines.append(
            f"Trace: {trace['bytes']} bytes; records={trace['records']}; incomplete={len(trace['incomplete'])}"
        )
        for site in trace["sites"][:8]:
            lines.append(f"  {site['tag']}/{site['function']}: {site['count']}")
    lines.append(
        f"Unparsed verbose records retained in JSON: {report['unparsed_verbose_records']}"
    )
    lines.extend(f"Note: {warning}" for warning in report["warnings"])
    lines.append(
        "Times: scope durations may overlap; SAT progress time is search-local. Memory: tracked MiB, not RSS."
    )
    return "\n".join(lines)
