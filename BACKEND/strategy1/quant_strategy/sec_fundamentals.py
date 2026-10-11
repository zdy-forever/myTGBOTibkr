"""Fundamentals from SEC XBRL 'companyfacts' (public). Point-in-time: only facts FILED strictly before `as_of` are used.
Produces the inputs of the research scoring (P3 company quality, V earnings yield, F quarterly revenue / net-income growth with the symmetric growth formula,
which stays defined for negative earnings). Foreign filers without US-GAAP facts (ADRs such as TSM or ASML) are reported as unscorable, never guessed."""
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

REV_TAGS = ('Revenues', 'RevenueFromContractWithCustomerExcludingAssessedTax', 'RevenueFromContractWithCustomerIncludingAssessedTax', 'SalesRevenueNet', 'SalesRevenueGoodsNet')
NI_TAGS = ('NetIncomeLoss', 'ProfitLoss', 'NetIncomeLossAvailableToCommonStockholdersBasic')
CFO_TAGS = ('NetCashProvidedByUsedInOperatingActivities', 'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations')
EPS_TAGS = ('EarningsPerShareDiluted', 'EarningsPerShareBasicAndDiluted', 'EarningsPerShareBasic')
FORMS = {'10-K', '10-Q'}


def _d(text):
    return datetime.strptime(text[:10], '%Y-%m-%d').date()


def _rows(facts, tag, unit, as_of):
    out = []
    for r in facts.get('facts', {}).get('us-gaap', {}).get(tag, {}).get('units', {}).get(unit, []):
        if 'start' not in r or 'end' not in r or 'filed' not in r or r.get('form', '').removesuffix('/A') not in FORMS:
            continue
        if _d(r['filed']) >= as_of:
            continue
        out.append(dict(start=_d(r['start']), end=_d(r['end']), val=float(r['val']), filed=_d(r['filed'])))
    return out


def _by_end(rows, lo, hi):
    """{end: value} for durations in [lo, hi] days; a later filing of the same period (restatement) wins, as it was public by `as_of`."""
    best = {}
    for r in sorted(rows, key=lambda r: r['filed']):
        if lo <= (r['end'] - r['start']).days <= hi:
            best[r['end']] = r['val']
    return best


def annual_series(facts, tags, unit, as_of):
    candidates = [_by_end(_rows(facts, t, unit, as_of), 350, 380) for t in tags]
    candidates = [c for c in candidates if c]
    return max(candidates, key=lambda c: max(c)) if candidates else {}


def quarter_series(facts, tags, unit, as_of):
    """Direct 3-month facts plus a derived Q4 (fiscal year - Q1..Q3) where the three quarters are present."""
    best = {}
    for t in tags:
        rows = _rows(facts, t, unit, as_of)
        q, a = _by_end(rows, 80, 100), _by_end(rows, 350, 380)
        if not q:
            continue
        for end, total in a.items():
            if end in q:
                continue
            inside = [e for e in q if end - timedelta(days=335) < e < end - timedelta(days=30)]
            if len(inside) == 3:
                q[end] = total - sum(q[e] for e in inside)
        if not best or max(q) > max(best):
            best = q
    return best


def sym_growth(a, b):
    """2(a-b)/(|a|+|b|): bounded, symmetric, defined for negative values; 0 when both are 0."""
    den = abs(a) + abs(b)
    return 0.0 if den == 0 else 2.0 * (a - b) / den


def _near(series, target, tolerance):
    hits = [e for e in series if abs((e - target).days) <= tolerance]
    return series[min(hits, key=lambda e: abs((e - target).days))] if hits else None


def yoy(series, end):
    base = _near(series, end - timedelta(days=365), 25)
    return None if base is None else sym_growth(series[end], base)


def metrics(facts, as_of, price):
    """(dict, None) on success, (None, reason) when the company cannot be scored from public SEC data."""
    if 'us-gaap' not in facts.get('facts', {}):
        return None, 'no_us_gaap_facts'
    rev, ni, cfo = (annual_series(facts, t, 'USD', as_of) for t in (REV_TAGS, NI_TAGS, CFO_TAGS))
    eps = annual_series(facts, EPS_TAGS, 'USD/shares', as_of)
    if not rev:
        return None, 'no_annual_revenue'
    end = max(rev)
    if (as_of - end).days > 550:
        return None, 'stale_annual_report'
    old = [e for e in rev if e < end and 2.0 <= (end - e).days / 365.25 <= 3.2]
    start = min(old, key=lambda e: abs((end - e).days / 365.25 - 3.0)) if old else None
    if start is None or rev[start] <= 0 or rev[end] <= 0:
        return None, 'short_or_nonpositive_revenue_history'
    years = (end - start).days / 365.25
    if end not in ni or end not in cfo or end not in eps:
        return None, 'missing_annual_' + ','.join(n for n, s in (('net_income', ni), ('cash_flow', cfo), ('eps', eps)) if end not in s)
    out = dict(rev3=(rev[end] / rev[start]) ** (1 / years) - 1, margin=ni[end] / rev[end], cfo_ratio=cfo[end] / rev[end], eps=eps[end],
               ey=eps[end] / price if price and price > 0 else float('nan'), fiscal_year_end=end)
    rq, nq = quarter_series(facts, REV_TAGS, 'USD', as_of), quarter_series(facts, NI_TAGS, 'USD', as_of)
    if not rq or not nq:
        return None, 'missing_quarterly'
    q_end = max(rq)
    if (as_of - q_end).days > 250 or q_end not in nq:
        return None, 'stale_or_unaligned_quarter'
    prior = [e for e in rq if 70 <= (q_end - e).days <= 110 and e in nq]
    if not prior:
        return None, 'no_previous_quarter'
    p_end = max(prior)
    vals = dict(s_rev_now=yoy(rq, q_end), s_rev_prev=yoy(rq, p_end), s_ni_now=yoy(nq, q_end), s_ni_prev=yoy(nq, p_end))
    if any(v is None for v in vals.values()):
        return None, 'missing_year_ago_quarter'
    out.update(vals, quarter_end=q_end)
    return out, None


class SecClient:
    """Rate-limited (<= ~8 requests/s) SEC downloader with an on-disk cache."""

    def __init__(self, user_agent, cache_dir, session=None, ttl_days=7, pause=0.13):
        self.headers = {'User-Agent': user_agent}
        self.cache = Path(cache_dir) / 'sec'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.session, self.ttl, self.pause = session or requests.Session(), timedelta(days=ttl_days), pause

    def _fetch(self, url, name):
        path = self.cache / name
        if path.exists() and datetime.now() - datetime.fromtimestamp(path.stat().st_mtime) < self.ttl:
            return json.loads(path.read_text())
        for attempt in range(3):
            response = self.session.get(url, headers=self.headers, timeout=60)
            time.sleep(self.pause)
            if response.status_code == 200:
                path.write_text(response.text)
                return response.json()
            if response.status_code not in (429, 500, 502, 503):
                break
            time.sleep(2 + attempt)
        if path.exists():                       # stale cache beats no data; the caller can see the age through the file time
            return json.loads(path.read_text())
        raise RuntimeError(f'SEC request failed: {url} (HTTP {response.status_code})')

    def tickers(self):
        """{ticker: cik} and {normalised title: [tickers]} from the SEC ticker file (file order = primary listing first)."""
        raw = self._fetch('https://www.sec.gov/files/company_tickers_exchange.json', 'company_tickers_exchange.json')
        rows = [dict(zip(raw['fields'], r)) for r in raw['data']]
        return {r['ticker'].upper(): int(r['cik']) for r in rows}, rows

    def companyfacts(self, cik):
        return self._fetch(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{int(cik):010d}.json', f'facts_{int(cik):010d}.json')
