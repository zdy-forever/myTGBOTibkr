"""ETF leaders pool from SEC N-PORT filings (public). Each ETF's latest filing gives its holdings; the top `leaders_per_group` by weight are its leaders, with the
same overlap rule as the QuantConnect version (the extra tech ETFs skip names already taken, the original 9 are never changed). The research re-selects once a year, so
the pool is refreshed at most once a year or on demand (/refreshpool). The result is stored in pool.json and can also be edited by hand."""
import io
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from . import config
from .logic import assign_leaders
from .universe_data import ETFS, EXTRA_SPARE, EXTRA_TECH_ETFS

POOL_FILE = config.DATA_DIR / 'pool.json'
REPO = 'trader298/sec-nport'
SERIES = {'ROBO': 'S000042659', 'XBI': 'S000010018', 'BUG': 'S000066713', 'GRID': 'S000026919', 'UFO': 'S000061584', 'NLR': 'S000018475', 'XAR': 'S000012318',
          'IYW': 'S000004329', 'SOXX': 'S000004354', 'IGV': 'S000004355', 'SMH': 'S000034411', 'SKYY': 'S000033237', 'FDN': 'S000012479', 'HACK': 'S000082278',
          'BOTZ': 'S000054693', 'FINX': 'S000054691'}
_DROP = {'INC', 'INCORPORATED', 'CORP', 'CORPORATION', 'CO', 'COMPANY', 'LTD', 'LIMITED', 'PLC', 'HOLDINGS', 'HOLDING', 'GROUP', 'THE', 'NV', 'SA', 'LLC', 'LP', 'CLASS', 'COM', 'NEW', 'ADR', 'DE'}


def normalise_name(name):
    tokens = re.sub(r'[^A-Z0-9 ]', ' ', str(name).upper().replace('&', ' AND ').replace('.', '')).split()      # dots removed first: N.V. -> NV
    return ' '.join(t for t in tokens if t not in _DROP)


def clean_ticker(value):
    if value is None or (isinstance(value, float) and value != value):
        return None
    v = re.sub(r'\s+(US|UW|UN|UQ)$', '', str(value).upper().strip())
    return v if re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,9}', v) else None


def primary_tickers(rows):
    """{any ticker: the first (primary) ticker of the same company}: two share classes of one company (GOOG / GOOGL) are one name."""
    first, out = {}, {}
    for r in rows:
        cik = r.get('cik')
        first.setdefault(cik, r['ticker'].upper())
        out[r['ticker'].upper()] = first[cik]
    return out


def name_to_ticker(rows):
    """{normalised SEC title: ticker} using the first listing in the SEC file (the primary listing); ambiguous titles keep the first."""
    out = {}
    for r in rows:
        out.setdefault(normalise_name(r['name']), r['ticker'].upper())
    return out


def _url(table, year, quarter, revision='main'):
    return f'https://huggingface.co/datasets/{REPO}/resolve/{revision}/{table}/year={year}/quarter={quarter}/data.parquet'


def read_metadata(table, year, quarter, session=None):
    response = (session or requests).get(_url(table, year, quarter), timeout=(15, 120))
    response.raise_for_status()
    return pd.read_parquet(io.BytesIO(response.content))


def read_filtered(table, year, quarter, column, values, columns):
    import fsspec
    import pyarrow.parquet as pq
    fs = fsspec.filesystem('http', block_size=1 << 20)
    with fs.open(_url(table, year, quarter), 'rb') as handle:
        return pq.read_table(handle, filters=[(column, 'in', list(values))], columns=columns).to_pandas()


def quarters_back(today, count=6):
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(count):
        yield y, q
        q -= 1
        if q == 0:
            y, q = y - 1, 4


def latest_filings(series_ids, today, reader=read_metadata):
    """{series_id: dict(accession, report_date, year, quarter)}: the newest filing of each series found while walking back quarter by quarter."""
    found = {}
    for year, quarter in quarters_back(today):
        missing = [s for s in series_ids if s not in found]
        if not missing:
            break
        try:
            info, sub = reader('FUND_REPORTED_INFO', year, quarter), reader('SUBMISSION', year, quarter)
        except Exception:
            continue
        f = info[info.SERIES_ID.isin(missing)].merge(sub, on='ACCESSION_NUMBER')
        f['REPORT_DATE'] = pd.to_datetime(f.REPORT_DATE)
        for r in f.sort_values('REPORT_DATE').groupby('SERIES_ID').tail(1).itertuples():
            found[r.SERIES_ID] = dict(accession=r.ACCESSION_NUMBER, report_date=str(r.REPORT_DATE.date()), year=year, quarter=quarter)
    return found


def ranked_holdings(filings, name_map, top, reader=read_filtered):
    """{series_id: [(ticker, weight_pct, issuer)]} for common equities in USD, best first, top[series] + a few spare rows (the caller merges share classes and then trims); unresolved issuers returned separately."""
    ranked, unresolved = {}, {}
    frame = pd.DataFrame([dict(series=s, **f) for s, f in filings.items()])
    for (year, quarter), group in frame.groupby(['year', 'quarter']):
        acc = {r.accession: r.series for r in group.itertuples()}
        hold = reader('FUND_REPORTED_HOLDING', year, quarter, 'ACCESSION_NUMBER', list(acc), ['ACCESSION_NUMBER', 'HOLDING_ID', 'ISSUER_NAME', 'PERCENTAGE', 'ASSET_CAT', 'CURRENCY_CODE'])
        hold = hold[hold.ASSET_CAT.eq('EC') & hold.CURRENCY_CODE.eq('USD')].copy()
        hold['PERCENTAGE'] = pd.to_numeric(hold.PERCENTAGE, errors='coerce')
        hold['series'] = hold.ACCESSION_NUMBER.map(acc)
        hold = hold.sort_values('PERCENTAGE', ascending=False)
        hold = hold[hold.groupby('series').cumcount() < hold.series.map(lambda s: top[s] + 6)]      # a few spare rows: duplicates / unresolved names are dropped later
        ids = reader('IDENTIFIERS', year, quarter, 'HOLDING_ID', hold.HOLDING_ID.tolist(), ['HOLDING_ID', 'IDENTIFIER_TICKER'])
        tick = dict(zip(ids.HOLDING_ID, ids.IDENTIFIER_TICKER))
        for r in hold.itertuples():
            ticker = clean_ticker(tick.get(r.HOLDING_ID)) or name_map.get(normalise_name(r.ISSUER_NAME))
            if not ticker:
                unresolved.setdefault(r.series, []).append(r.ISSUER_NAME)
            elif ticker not in [t for t, _, _ in ranked.get(r.series, [])]:           # two share classes of one company (e.g. Alphabet) count once
                ranked.setdefault(r.series, []).append((ticker, float(r.PERCENTAGE), r.ISSUER_NAME))
    return ranked, unresolved


def build_pool(sec_rows, today=None, leaders=None, extra=None, dedupe=None, filings_reader=read_metadata, holdings_reader=read_filtered):
    s = config.STRATEGY
    leaders = leaders or s['leaders_per_group']
    extra = s['extra_tech_etfs'] if extra is None else extra
    dedupe = s['dedupe_overlap'] if dedupe is None else dedupe
    today = today or date.today()
    etfs = list(ETFS) + (list(EXTRA_TECH_ETFS) if extra else [])
    sid = {e: SERIES[e] for e in etfs}
    filings = latest_filings(list(sid.values()), today, filings_reader)
    top = {sid[e]: leaders if e in ETFS else leaders + EXTRA_SPARE for e in etfs if sid[e] in filings}
    ranked, unresolved = ranked_holdings(filings, name_to_ticker(sec_rows), top, holdings_reader)
    canon = primary_tickers(sec_rows)
    by_etf = {}
    for e in etfs:
        seen = []
        for t, _, _ in ranked.get(sid[e], []):
            t = canon.get(t, t)
            if t not in seen:
                seen.append(t)
        by_etf[e] = seen[:leaders if e in ETFS else leaders + EXTRA_SPARE]          # trim AFTER merging share classes, so a second class never costs a place
    chosen, skipped = assign_leaders(by_etf, [e for e in etfs if e in ETFS], [e for e in etfs if e not in ETFS], leaders, dedupe)
    return dict(built=datetime.now(timezone.utc).isoformat(timespec='seconds'), leaders_per_group=leaders, extra_tech_etfs=extra, dedupe_overlap=dedupe,
                filings={e: dict(filings[sid[e]], series=sid[e]) for e in etfs if sid[e] in filings}, missing_filings=[e for e in etfs if sid[e] not in filings],
                leaders=chosen, skipped_duplicates=skipped, unresolved_issuers={e: unresolved.get(sid[e], []) for e in etfs if unresolved.get(sid[e])})


def load_pool(path=POOL_FILE):
    return json.loads(Path(path).read_text()) if Path(path).exists() else None


def save_pool(pool, path=POOL_FILE):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(pool, indent=2))


def sleeves(pool):
    """{ticker: set of ETF sleeves it leads}."""
    out = {}
    for etf, syms in pool['leaders'].items():
        for s in syms:
            out.setdefault(s, set()).add(etf)
    return out


def pool_age_days(pool, today=None):
    return ((today or datetime.now(timezone.utc)) - datetime.fromisoformat(pool['built'])).days
