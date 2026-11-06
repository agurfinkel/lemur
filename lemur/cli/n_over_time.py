"""lemur n-over-time: SAT level N(t) per NLA emission across one or more traces.

Single subcommand, four output formats (--format table|json|html|png):

- table : Rich/plain per-trace stats (round counts, climb / end-drop /
  round-length / N-at-emission quartiles).
- json  : structured dump of every RoundData field, keyed by trace label.
- html  : plotly interactive subplot per trace; green ▲=round-start,
  gold band=round span, red ▼=post-POP level after round-ending POP.
- png   : matplotlib static image with the same markers.

Required trace tags::

    -tr:decide -tr:pop_scope -tr:nla_solver -tr:arith_conflict

Capture them via `lemur sweep --trace nla_solver,decide,pop_scope,arith_conflict`
or pass them directly to z3. Note that `-tr:nla_solver` adds ~5x runtime
overhead on hard benchmarks; the four tags together are what this view
needs.
"""

import csv
import io
import json
import sys
from pathlib import Path

from lemur.round import (
    RoundData, parse_round_data, quartiles,
    lemma_ranges_per_round, RoundLemmaRange,
)
from lemur.table import make_console
from lemur.cli import agent_help


def register(subparsers):
    p = subparsers.add_parser(
        'n-over-time',
        help='SAT level N(t) per NLA emission — plot or stats',
        epilog='AI agents: use `lemur n-over-time --agent` for terse usage guide.',
    )
    agent_help.add_agent_flag(p, 'n-over-time')
    p.add_argument('traces', nargs='+', metavar='TRACE',
                   help='One or more .z3-trace files. Each becomes a '
                        'subplot row (plot) or a section (stats).')
    p.add_argument('--label', action='append', default=None, metavar='LABEL',
                   help='Label for a TRACE, paired by position. Default: '
                        'trace file stem. Repeatable.')
    p.add_argument('--format', '-f',
                   choices=['table', 'json', 'html', 'png'], default=None,
                   help='Output format. Default: table on TTY, json otherwise. '
                        'html/png require --out PATH.')
    p.add_argument('--out', '-o', default=None, metavar='PATH',
                   help='Output file path (required for html/png).')
    p.add_argument('--shared-y', action='store_true',
                   help='Plot formats: share y-axis across subplots.')
    p.add_argument('--lemmas-per-round', action='store_true',
                   help='With --format json: include each round\'s lemma '
                        'list (strategy, conclusion, sizes) per trace. '
                        'Adds ~20-100x to JSON size on lemma-heavy traces.')
    p.add_argument('--no-color', action='store_true',
                   help='Disable color in rich table output.')
    p.set_defaults(func=run)


def run(args):
    trace_paths = [Path(t) for t in args.traces]
    for tp in trace_paths:
        if not tp.exists():
            print(f"Error: trace file not found: {tp}", file=sys.stderr)
            sys.exit(1)

    labels = args.label or []
    if labels and len(labels) != len(trace_paths):
        print(f"Error: --label given {len(labels)} times but {len(trace_paths)} "
              f"traces provided. --label is paired by position.", file=sys.stderr)
        sys.exit(2)

    fmt = args.format
    if fmt is None:
        fmt = 'table' if sys.stdout.isatty() else 'json'

    if fmt in ('html', 'png') and not args.out:
        print(f"Error: --format {fmt} requires --out PATH.", file=sys.stderr)
        sys.exit(2)

    if args.lemmas_per_round and fmt != 'json':
        print("Error: --lemmas-per-round only applies with --format json.",
              file=sys.stderr)
        sys.exit(2)

    round_datas: list[RoundData] = []
    for i, tp in enumerate(trace_paths):
        label = labels[i] if labels else tp.stem
        round_datas.append(parse_round_data(tp, label=label))

    if fmt == 'table':
        _render_table(round_datas, plain=False, no_color=args.no_color,
                      out_path=args.out)
    elif fmt == 'json':
        lemma_data = None
        if args.lemmas_per_round:
            lemma_data = _collect_lemmas_per_round(trace_paths, round_datas)
        _render_json(round_datas, out_path=args.out, lemma_data=lemma_data)
    elif fmt == 'html':
        _render_html(round_datas, out_path=args.out, shared_y=args.shared_y)
    elif fmt == 'png':
        _render_png(round_datas, out_path=args.out, shared_y=args.shared_y)
    else:
        raise ValueError(f"unknown format: {fmt}")


_QUART_KEYS = ('n', 'min', 'q1', 'median', 'q3', 'max', 'mean')

_METRIC_LABELS = (
    ('round climb (max-N)',       'round_climbs'),
    ('end-drop (N - target)',     'end_drops'),
    ('internal pop drop',         'internal_pop_drops'),
    ('round length (events)',     'round_lengths'),
    ('N at NLA emission',         'n_series'),
)


def _render_table(round_datas, *, plain: bool, no_color: bool, out_path: str | None):
    # Plain CSV when piped, when --out is set (going to file), or when caller
    # forces it. Rich panels only when going to an interactive terminal.
    if plain or out_path or not sys.stdout.isatty():
        # Plain CSV: per-trace totals + quartile rows.
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['label', 'metric', 'value'])
        for rd in round_datas:
            for k, v in rd.totals.items():
                w.writerow([rd.label, k, v])
            w.writerow([rd.label, 'max_decision_level', rd.max_decision_level])
        w.writerow(['label', 'metric', *_QUART_KEYS])
        for rd in round_datas:
            for label, attr in _METRIC_LABELS:
                q = quartiles(getattr(rd, attr))
                w.writerow([rd.label, label, *(q[k] for k in _QUART_KEYS)])
        text = buf.getvalue()
        if out_path:
            Path(out_path).write_text(text)
            print(f"wrote {out_path}", file=sys.stderr)
        else:
            sys.stdout.write(text)
        return

    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text as RichText

    console = make_console(no_color=no_color)
    for rd in round_datas:
        tot = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
        tot.add_column("Key", style="bold")
        tot.add_column("Value", justify='right')
        for k, v in rd.totals.items():
            tot.add_row(k, str(v))
        tot.add_row("max decision level", str(rd.max_decision_level))

        qt = Table(show_header=True, box=None, pad_edge=False, padding=(0, 1))
        qt.add_column("metric", style="bold")
        for col in _QUART_KEYS:
            qt.add_column(col, justify='right')
        for label, attr in _METRIC_LABELS:
            q = quartiles(getattr(rd, attr))
            qt.add_row(label, *(str(q[k]) for k in _QUART_KEYS))

        console.print(Panel(
            Group(tot, RichText(""), qt),
            title=RichText(rd.label, style="bold"),
            expand=False,
        ))


def _render_json(round_datas, *, out_path: str | None, lemma_data=None):
    out = {}
    for rd in round_datas:
        entry = {
            'totals': rd.totals,
            'max_decision_level': rd.max_decision_level,
            'n_series': rd.n_series,
            'round_starts': [list(s) for s in rd.round_starts],
            'pop_marks': [list(m) for m in rd.pop_marks],
            'round_climbs': rd.round_climbs,
            'internal_pop_drops': rd.internal_pop_drops,
            'end_drops': rd.end_drops,
            'round_lengths': rd.round_lengths,
            'quartiles': {
                attr: quartiles(getattr(rd, attr))
                for _, attr in _METRIC_LABELS
            },
        }
        if lemma_data is not None and rd.label in lemma_data:
            entry['rounds'] = lemma_data[rd.label]
        out[rd.label] = entry
    text = json.dumps(out, indent=2)
    if out_path:
        Path(out_path).write_text(text)
        print(f"wrote {out_path}", file=sys.stderr)
    else:
        print(text)


def _collect_lemmas_per_round(trace_paths, round_datas):
    """Build per-trace `[{round, lemma_start, lemma_end, closed, lemmas: [...]}]`
    lists. Each lemma record carries its 1-based original index, strategy,
    conclusion text, and size counts (preconds / vars / monomials)."""
    from lemur.parsers import parse_trace, group_by_tag, collect_varmap
    from lemur.lemma import LemmaAnalyzer
    from lemur.report import humanize_varmap, humanize_constants

    out: dict[str, list[dict]] = {}
    for tp, rd in zip(trace_paths, round_datas):
        entries = list(parse_trace(tp))
        nla_entries = group_by_tag(entries).get('nla_solver', [])
        varmap = humanize_varmap(collect_varmap(nla_entries))
        lemmas = list(LemmaAnalyzer(nla_entries).extract())
        ranges = lemma_ranges_per_round(rd)

        round_blobs = []
        for r in ranges:
            recs = lemmas[r.lemma_start - 1 : r.lemma_end]
            round_blobs.append({
                'round': r.round,
                'lemma_start': r.lemma_start,
                'lemma_end': r.lemma_end,
                'closed': r.closed,
                'lemmas': [
                    {
                        'index': r.lemma_start + j,
                        'strategy': rec.strategy or '',
                        'conclusion': humanize_constants(
                            _apply_varmap(rec.conclusion or '', varmap)
                        ),
                        'preconds': len(rec.preconditions),
                        'vars': len(rec.variables),
                        'monomials': len(rec.monomials),
                    }
                    for j, rec in enumerate(recs)
                ],
            })
        out[rd.label] = round_blobs
    return out


def _apply_varmap(s: str, varmap: dict[str, str]) -> str:
    """Light-weight varmap substitution for JSON output. (report.py keeps
    the heavyweight pipeline; we only need the simple j-name swap here.)"""
    if not varmap or not s:
        return s
    import re as _re
    return _re.sub(
        r'\bj\d+\b',
        lambda m: varmap.get(m.group(0), m.group(0)),
        s,
    )


def _require(import_target: str, extra: str = 'plot'):
    """Lazy-import a plotting backend with a clear install hint on failure."""
    try:
        return __import__(import_target)
    except ImportError as e:
        print(
            f"Error: {import_target} is required for this format. "
            f"From the Lemur checkout, install with: uv sync --extra {extra}",
            file=sys.stderr,
        )
        raise SystemExit(1) from e


_PLOT_COLORS_HEX = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd']
_PLOT_COLORS_MPL = ['tab:blue', 'tab:orange', 'tab:green', 'tab:red', 'tab:purple']


def _render_html(round_datas, *, out_path: str, shared_y: bool):
    _require('plotly')
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(
        rows=len(round_datas), cols=1,
        shared_xaxes=False, shared_yaxes=shared_y,
        subplot_titles=[rd.label for rd in round_datas],
        vertical_spacing=0.08,
    )

    # Gold round-span bands are batched into one fig.update_layout(shapes=...)
    # call after the per-trace loop. add_vrect appends + revalidates layout
    # shapes per call, which is O(N²) at thousands of rounds; one assignment
    # at the end is O(N).
    band_shapes: list[dict] = []

    for i, rd in enumerate(round_datas):
        row = i + 1
        color = _PLOT_COLORS_HEX[i % len(_PLOT_COLORS_HEX)]
        xs = list(range(1, len(rd.n_series) + 1))

        fig.add_trace(
            go.Scatter(
                x=xs, y=rd.n_series, mode='lines',
                line=dict(color=color, width=1),
                name=f'{rd.label} N',
                hovertemplate='emission #%{x}<br>N=%{y}<extra></extra>',
                legendgroup=rd.label, showlegend=True,
            ),
            row=row, col=1,
        )

        xref = 'x' if row == 1 else f'x{row}'
        yref = 'y domain' if row == 1 else f'y{row} domain'
        for (start_idx, _start_N), pop in zip(rd.round_starts, rd.pop_marks):
            band_shapes.append(dict(
                type='rect',
                xref=xref, yref=yref,
                x0=start_idx, x1=pop[0],
                y0=0, y1=1,
                fillcolor='gold', opacity=0.10,
                line=dict(width=0),
                layer='below',
            ))

        # Dotted gray drop from prev_N down to post-pop level.
        if rd.pop_marks:
            drop_x: list[int | None] = []
            drop_y: list[int | None] = []
            for idx, prev_N, post in rd.pop_marks:
                drop_x.extend([idx, idx, None])
                drop_y.extend([prev_N, post, None])
            fig.add_trace(
                go.Scatter(
                    x=drop_x, y=drop_y, mode='lines',
                    line=dict(color='gray', width=0.6, dash='dot'),
                    name=f'{rd.label} drop', legendgroup=rd.label,
                    showlegend=False, hoverinfo='skip',
                ),
                row=row, col=1,
            )

        # Red down-triangle at every round-ending pop.
        if rd.pop_marks:
            fig.add_trace(
                go.Scatter(
                    x=[m[0] for m in rd.pop_marks],
                    y=[m[2] for m in rd.pop_marks],
                    mode='markers',
                    marker=dict(color='red', size=8, symbol='triangle-down'),
                    name=f'{rd.label} pop-of-N',
                    legendgroup=rd.label, showlegend=True,
                    customdata=[[m[1]] for m in rd.pop_marks],
                    hovertemplate=(
                        'emission #%{x}<br>'
                        'prev N=%{customdata[0]}<br>'
                        'post-POP level=%{y}<extra></extra>'
                    ),
                ),
                row=row, col=1,
            )

        fig.update_xaxes(title_text='NLA emission index', row=row, col=1)
        fig.update_yaxes(title_text='N (level)', row=row, col=1)

    fig.update_layout(
        title=(
            'N over time per trace — line: N at each NLA emission. '
            'Gold band=round span. '
            'Red ▼=post-POP level after round-ending POP.'
        ),
        height=380 * len(round_datas) + 80,
        hovermode='closest', plot_bgcolor='#fafafa',
    )
    fig.update_xaxes(showgrid=True, gridcolor='#eee')
    fig.update_yaxes(showgrid=True, gridcolor='#eee')

    if band_shapes:
        fig.update_layout(shapes=band_shapes)

    fig.write_html(
        out_path,
        include_plotlyjs='cdn',
        config={'scrollZoom': True, 'displayModeBar': True},
    )
    print(f"wrote {out_path}", file=sys.stderr)


def _render_png(round_datas, *, out_path: str, shared_y: bool):
    _require('matplotlib')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    n = len(round_datas)
    fig, axes = plt.subplots(
        n, 1, figsize=(13, 4 * n),
        sharex=False, sharey=shared_y, squeeze=False,
    )

    for i, rd in enumerate(round_datas):
        ax = axes[i, 0]
        color = _PLOT_COLORS_MPL[i % len(_PLOT_COLORS_MPL)]
        xs = list(range(1, len(rd.n_series) + 1))
        ax.plot(
            xs, rd.n_series, linewidth=0.6, alpha=0.7, color=color,
            label=(f'{rd.label}  (n={len(rd.n_series)} emissions, '
                   f'{len(rd.round_starts)} rounds, '
                   f'{len(rd.pop_marks)} closed)'),
        )

        for (start_idx, _start_N), pop in zip(rd.round_starts, rd.pop_marks):
            ax.axvspan(start_idx, pop[0], color='gold', alpha=0.10)

        for idx, prev_N, post in rd.pop_marks:
            ax.vlines(idx, post, prev_N, colors='gray', linewidth=0.4,
                      alpha=0.35, linestyles=':')

        if rd.pop_marks:
            ax.scatter(
                [m[0] for m in rd.pop_marks],
                [m[2] for m in rd.pop_marks],
                s=18, color='red', alpha=0.7, marker='v',
                edgecolors='none', zorder=3,
                label='pop-of-N (post-POP level)',
            )

        ax.set_xlabel('NLA emission index')
        ax.set_ylabel('N (level at NLA emission)')
        ax.set_title(rd.label)
        ax.grid(True, alpha=0.3)
        ax.legend(loc='upper left', fontsize='small')

    fig.suptitle(
        'N over time — line=N at each NLA emission, '
        'gold band=round span, '
        'red ▼=post-POP level (round-ending POP)',
        fontsize='medium', y=1.0,
    )
    plt.tight_layout()
    plt.savefig(out_path, dpi=120)
    plt.close(fig)
    print(f"wrote {out_path}", file=sys.stderr)
