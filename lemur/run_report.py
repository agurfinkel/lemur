"""Evidence-based summaries of Z3 stdout, verbose progress, and text traces.

Verbose formats verified against Z3 e8b9320: tactic/tactic.cpp,
sat/sat_solver.cpp, and smt/smt_context_pp.cpp. Unknown shapes stay visible.
Reported scope durations can overlap; SAT elapsed time is search-local.
"""

import json
import re
import textwrap
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


def diagnose_termination(stdout, stderr, verdict, metadata=None):
    """Report explicit diagnostics without guessing caps or process exit status."""
    evidence = []
    for stream, text in (("stdout", stdout), ("stderr", stderr)):
        for line, raw in enumerate(text.splitlines(), 1):
            token = raw.strip()
            error = re.fullmatch(r'\(error\s+"(.*)"\)', token, re.IGNORECASE)
            error_prefix = re.fullmatch(r"error:\s*(.+)", token, re.IGNORECASE)
            message = error[1] if error else error_prefix[1] if error_prefix else None
            if token.lower() in ("timeout", "timeout (process killed)"):
                cause = "timeout"
            elif (
                message is not None and "out of memory" in message.lower()
            ) or token.lower() == "out of memory":
                cause = "out_of_memory"
            elif message is not None:
                cause = "solver_error"
            else:
                continue
            evidence.append(
                {"cause": cause, "stream": stream, "line": line, "text": raw}
            )
    process = {}
    if metadata is not None:
        rc = metadata.get("returncode")
        killed = metadata.get("timed_out_by_runner")
        process = {"returncode": rc, "timed_out_by_runner": killed}
        if killed is True and not any(e["cause"] == "timeout" for e in evidence):
            evidence.append(
                {
                    "cause": "timeout",
                    "stream": "run_metadata",
                    "line": None,
                    "text": "timed_out_by_runner=true",
                }
            )
        if type(rc) is int and rc != 0 and not evidence:
            evidence.append(
                {
                    "cause": "signal" if rc < 0 else "process_error",
                    "stream": "run_metadata",
                    "line": None,
                    "text": f"returncode={rc}",
                }
            )
        limits = {}
        invocation = metadata.get("invocation")
        argv = invocation.get("argv", []) if isinstance(invocation, dict) else []
        for arg in argv if isinstance(argv, list) else []:
            if not isinstance(arg, str):
                continue
            if arg.startswith("memory_max_size="):
                limits["memory_max_size"] = arg.split("=", 1)[1]
            elif arg.startswith("-T:"):
                limits["timeout_s"] = arg[3:]
        process["configured_limits"] = limits
    causes = sorted({e["cause"] for e in evidence})
    cause = (
        causes[0]
        if len(causes) == 1
        else "multiple"
        if causes
        else "result_reported"
        if verdict
        else "unknown"
    )
    return {"cause": cause, "reported_causes": causes, "evidence": evidence, **process}


def build_report(stdout=None, stderr=None, trace=None, run_metadata=None):
    """Build a report without assuming absent counters imply absent activity."""
    out = Path(stdout).read_text(errors="replace") if stdout else ""
    err = Path(stderr).read_text(errors="replace") if stderr else ""
    results = [
        {"line": n, "result": line.strip()}
        for n, line in enumerate(out.splitlines(), 1)
        if RESULT.fullmatch(line.strip())
    ]
    verdict = results[-1]["result"] if results else None
    metadata = json.loads(Path(run_metadata).read_text()) if run_metadata else None
    if metadata is not None and (
        not isinstance(metadata, dict) or metadata.get("schema_version") != 1
    ):
        raise ValueError(
            "unsupported run metadata: expected an object with schema_version=1"
        )
    termination = diagnose_termination(out, err, verdict, metadata)
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
        warnings.append(
            "No result token recorded; termination reason is unknown."
            if termination["cause"] == "unknown"
            else "No result token recorded; termination diagnostics are reported separately."
        )
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
            "run_metadata": str(run_metadata) if run_metadata else None,
        },
        "verdict": verdict,
        "termination": termination,
        "results": results,
        "statistics": stats,
        "run_metadata": metadata,
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
            "termination": "Explicit diagnostics only; result_reported does not establish a clean process exit. Out of memory does not identify a configured cap versus host exhaustion.",
            "trace": "Event counts are not time measurements; tracing and expression printing perturb execution.",
        },
    }


def summarize_report(report):
    """Select compact structured findings; full reports retain raw evidence."""
    engines = {}
    for name, state in report["engines"].items():
        last = state["last_progress"]
        keys = (
            "conflicts",
            "decisions",
            "restarts",
            "propagations",
            "search_elapsed_s",
            "memory_mib",
        )
        engines[name] = {
            "search_observed": state["search_observed"],
            "progress_rows": state["progress_rows"],
            "last_progress": None
            if last is None
            else {
                "line": last["line_start"],
                "fields": {k: last["fields"][k] for k in keys if k in last["fields"]},
            },
        }
    circuits = [r for r in report["verbose"] if r["kind"] == "circuit"]
    circuit = None
    if circuits:
        first = circuits[0]
        circuit = {
            "line": first["line_start"],
            "complete": first["complete"],
            "fields": first["fields"],
        }
    phases = [
        r for r in report["verbose"] if r["kind"] == "phase" and "time" in r["fields"]
    ]
    phases = sorted(phases, key=lambda r: r["fields"]["time"], reverse=True)[:8]
    stats = {
        k: v
        for k, v in (report["statistics"] or {}).items()
        if k.startswith(("array-", "bv-"))
        or k in ("time", "total-time", "max-memory", "rlimit-count")
    }
    trace = report["trace"]
    return {
        "schema": 1,
        "detail": "summary",
        "inputs": report["inputs"],
        "verdict": report["verdict"],
        "termination": report["termination"],
        "result_count": len(report["results"]),
        "engines": engines,
        "first_circuit": circuit,
        "largest_scopes": [
            {
                "name": r["name"],
                "line": r["line_start"],
                "duration_s": r["fields"]["time"],
                "num_exprs": r["fields"].get("num-exprs"),
            }
            for r in phases
        ],
        "selected_statistics": stats if report["statistics"] is not None else None,
        "trace": None
        if trace is None
        else {
            "bytes": trace["bytes"],
            "records": trace["records"],
            "incomplete_blocks": len(trace["incomplete"]),
            "top_sites": trace["sites"][:8],
        },
        "unparsed_verbose_records": report["unparsed_verbose_records"],
        "warnings": report["warnings"],
        "semantics": report["semantics"],
    }


def render_plain(report):
    """Render a human summary; JSON retains the complete evidence records."""

    def number(value):
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, int):
            return f"{value:,}"
        if isinstance(value, float):
            return f"{value:,.2f}"
        return str(value)

    def details(data):
        for key, val in data.items():
            label = {
                "search_elapsed_s": "SAT elapsed (s)",
                "memory_mib": "memory (MiB)",
            }.get(key, key.replace("_", " "))
            lines.append(f"    {label}: {number(val)}")

    lines = [f"Result: {report['verdict'] or 'not recorded'}"]
    termination = report["termination"]
    if termination["cause"] not in ("result_reported", "unknown"):
        labels = {
            "out_of_memory": "out of memory",
            "timeout": "timeout",
            "solver_error": "solver error",
            "multiple": "multiple diagnostics",
            "signal": "terminated by signal",
            "process_error": "process error",
        }
        lines.append(f"Termination: {labels[termination['cause']]}")
        for item in termination["evidence"][:4]:
            location = (
                f"{item['stream']} line {item['line']}"
                if item["line"] is not None
                else item["stream"]
            )
            lines.append(
                textwrap.fill(
                    f"  {location}: {item['text'].strip()}",
                    width=88,
                    subsequent_indent="    ",
                )
            )
    if "returncode" in termination:
        lines.append(f"Process exit code: {termination['returncode']}")
        for name, val in termination["configured_limits"].items():
            lines.append(f"Configured {name}: {val}")
    for engine, data in report["engines"].items():
        observed = (
            "search observed" if data["search_observed"] else "tactic entry observed"
        )
        lines.extend(
            [
                "",
                f"Engine {engine.upper()}: {observed}; {data['progress_rows']} progress rows",
            ]
        )
        if last := data["last_progress"]:
            lines.append(f"  Last progress (stderr line {last['line_start']}):")
            keys = (
                "conflicts",
                "decisions",
                "restarts",
                "propagations",
                "search_elapsed_s",
                "memory_mib",
            )
            details({k: last["fields"][k] for k in keys if k in last["fields"]})
    if not report["engines"]:
        lines.append("Engine: not established by verbose evidence")
    circuits = [r for r in report["verbose"] if r["kind"] == "circuit"]
    if circuits:
        first = circuits[0]
        state = "complete" if first["complete"] else "partial"
        lines.extend(["", f"First SAT circuit snapshot ({state}):"])
        labels = {
            "vars": "allocated variables",
            "elim-vars": "eliminated variables",
            "binary-clauses": "binary clauses",
            "ternary-clauses": "ternary clauses",
            "clauses": "other clauses",
            "memory": "memory MiB",
        }
        details(
            {
                label: first["fields"][key]
                for key, label in labels.items()
                if key in first["fields"]
            }
        )
    phases = [
        r for r in report["verbose"] if r["kind"] == "phase" and "time" in r["fields"]
    ]
    if phases:
        lines.extend(
            [
                "",
                "Largest reported scope durations (may overlap; do not add):",
                f"  {'Scope':<24} {'Seconds':>9} {'Expressions':>13} {'Line':>7}",
            ]
        )
        for row in sorted(phases, key=lambda r: r["fields"]["time"], reverse=True)[:8]:
            fields = row["fields"]
            expressions = number(fields["num-exprs"]) if "num-exprs" in fields else "—"
            lines.append(
                f"  {row['name']:<24} {number(fields['time']):>9} {expressions:>13} {row['line_start']:>7}"
            )
    important = {
        k: v
        for k, v in (report["statistics"] or {}).items()
        if k.startswith(("array-", "bv-"))
        or k in ("time", "total-time", "max-memory", "rlimit-count")
    }
    if important:
        lines.extend(["", "Selected final statistics (missing counters are unknown):"])
        details(important)
    if trace := report["trace"]:
        lines.extend(
            [
                "",
                f"Trace: {trace['bytes']:,} bytes; {len(trace['incomplete']):,} incomplete blocks",
            ]
        )
        details(trace["records"])
        for site in trace["sites"][:8]:
            lines.append(f"    {site['tag']}/{site['function']}: {site['count']:,}")
    lines.extend(
        [
            "",
            f"Unparsed verbose records: {report['unparsed_verbose_records']:,} (preserved in JSON)",
        ]
    )
    for warning in report["warnings"]:
        lines.append(
            textwrap.fill("Note: " + warning, width=88, subsequent_indent="  ")
        )
    lines.extend(
        [
            "",
            "SAT elapsed time is search-local; memory is tracked MiB, not process RSS.",
            "Use --json for full evidence, all counters, and source lines.",
        ]
    )
    return "\n".join(lines)
