"""Plain-text rendering (no markup, so no escaping problems) for the host bot's /signals, /runstrategy and admin views. Every suggestion ends with the same disclaimer."""
from . import logic
from .signals import DISCLAIMER, user_scores


def _components(c):
    """Native points (V 25, F 15, D 10, Q 5 at most); P is shown reversed (30 - raw P, higher is better, must be >= 15 to be buyable)."""
    return f"P(rev) {logic.NATIVE['P'] - c['p']:.1f}  V {c['v']:.1f}  F {c['f']:.1f}  D {c['d']:.1f}  Q {c['q']:.0f}" if c else ''


def signals_text(us):
    if us.status == 'run_failed':
        return 'The strategy run failed, so there are no signals today.\n' + '\n'.join(us.notes[:3])
    if us.status == 'stale_data':
        return 'Market data is stale, so no signals are given.\n' + '\n'.join(us.notes[:3])
    s = us.summary
    lines = [f"Strategy {us.strategy['name']} v{us.strategy['version']}  run {us.run_id[:8]}", f"Your portfolio ${s['equity']:,.0f} (cash ${s['cash']:,.0f}); target weight per stock {s['target_weight']:.1%}"]
    if not us.signals:
        lines.append('No action suggested.')
    for g in us.signals:
        lines.append(f"{g.action} {g.symbol}  ${g.amount_usd:,.0f} (~{g.est_shares:.2f} sh)  [{g.kind}]" + (f"  score {g.score:.1f}" if g.score is not None else ''))
        lines.append(f'   {g.reason}')
        if g.components:
            lines.append(f'   {_components(g.components)}')
    if s.get('top_candidates'):
        lines.append('Top buyable: ' + ', '.join(f'{sym} {sc}' for sym, sc in s['top_candidates'][:6]))
    lines += [f'note: {n}' for n in us.notes[:5]] + [DISCLAIMER]
    return '\n'.join(lines)


def rank_text(run, held_values=None, n=15):
    scores = user_scores(run, held_values or {})
    rows = sorted(scores.items(), key=lambda kv: (-kv[1]['total'], kv[0]))[:n]
    lines = [f'Ranking, last price bar {run.as_of_bar}, run {run.run_id[:8]} ({run.status})', 'sym   total  gate  sleeves        P(rev)    V    F    D    Q']
    for s, sc in rows:
        lines.append(f"{s:<6}{sc['total']:5.1f}  {'ok ' if sc['gate_ok'] else 'NO '}  {'/'.join(sc['sleeves'])[:14]:<14} {logic.NATIVE['P'] - sc['p_native']:6.1f} {sc['v_native']:4.1f} {sc['f_native']:4.1f} {sc['d_native']:4.1f} {sc['q_native']:3.0f}")
    lines.append('V/F/D/Q in native points; total is the strategy score (0-100)')
    lines.append(f'scored {len(scores)} of {len(run.symbols) + len(run.unscored)} pool names; gate NO = reversed P below 15 (not buyable)')
    if run.unscored:
        lines.append('not scored: ' + ', '.join(f'{s}({r})' for s, r in sorted(run.unscored.items())[:12]))
    return '\n'.join(lines)


def pool_text(pool):
    lines = [f"Pool built {pool['built'][:10]}  leaders per theme {pool['leaders_per_group']}  extra tech ETFs {pool['extra_tech_etfs']}  dedupe {pool['dedupe_overlap']}"]
    for etf, syms in pool['leaders'].items():
        skipped = pool['skipped_duplicates'].get(etf)
        filing = pool['filings'].get(etf, {}).get('report_date', '?')
        lines.append(f"{etf:<5} {', '.join(syms)}" + (f"  (skipped duplicates: {', '.join(skipped)})" if skipped else '') + f'  [filing {filing}]')
    if pool.get('missing_filings'):
        lines.append('no filing found for: ' + ', '.join(pool['missing_filings']))
    if pool.get('unresolved_issuers'):
        lines.append('issuers without a ticker (skipped): ' + '; '.join(f"{e}: {', '.join(v)}" for e, v in pool['unresolved_issuers'].items()))
    return '\n'.join(lines)


def run_text(run):
    return '\n'.join([f"Strategy run {run.run_id[:8]}  {run.strategy['name']} v{run.strategy['version']}  status {run.status}", f'Last price bar {run.as_of_bar}; scored {len(run.symbols)} / {len(run.symbols) + len(run.unscored)}; started {run.started_at}',
                      *[f'note: {n}' for n in run.notes[:8]]])
