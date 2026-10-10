from datetime import date

import pandas as pd

from quant_strategy import pool
from quant_strategy.universe_data import ETFS, EXTRA_TECH_ETFS


def test_name_normalisation_and_ticker_cleaning():
    assert pool.normalise_name('NVIDIA CORPORATION') == pool.normalise_name('NVIDIA Corp.') == 'NVIDIA'
    assert pool.normalise_name('NXP Semiconductors NV') == pool.normalise_name('NXP Semiconductors N.V.') == 'NXP SEMICONDUCTORS'
    assert pool.normalise_name('Johnson & Johnson Inc') == 'JOHNSON AND JOHNSON'
    assert pool.clean_ticker('AAPL US') == 'AAPL' and pool.clean_ticker(None) is None and pool.clean_ticker('123') is None


def make_readers(holdings):
    """holdings: {series: [(issuer, weight, ticker_or_None)]} -> fake metadata / filtered readers."""
    accs = {s: f'ACC-{s}' for s in holdings}

    def metadata(table, year, quarter):
        if table == 'FUND_REPORTED_INFO':
            return pd.DataFrame(dict(SERIES_ID=list(accs), ACCESSION_NUMBER=list(accs.values())))
        return pd.DataFrame(dict(ACCESSION_NUMBER=list(accs.values()), REPORT_DATE='2026-03-31', FILING_DATE='2026-05-25'))

    rows, idrows, n = [], [], 0
    for s, items in holdings.items():
        for issuer, weight, ticker in items:
            n += 1
            rows.append(dict(ACCESSION_NUMBER=accs[s], HOLDING_ID=n, ISSUER_NAME=issuer, PERCENTAGE=weight, ASSET_CAT='EC', CURRENCY_CODE='USD'))
            idrows.append(dict(HOLDING_ID=n, IDENTIFIER_TICKER=ticker))

    def filtered(table, year, quarter, column, values, columns):
        frame = pd.DataFrame(rows if table == 'FUND_REPORTED_HOLDING' else idrows)
        return frame[frame[column].isin(values)][columns]
    return metadata, filtered


def test_build_pool_uses_per_etf_depth_dedupes_share_classes_and_extras():
    series = pool.SERIES
    h = {s: [] for s in series.values()}
    h[series['IYW']] = [('NVIDIA CORP', 17, 'NVDA'), ('ALPHABET INC', 9, None), ('ALPHABET INC', 8, 'GOOG'), ('MICROSOFT CORP', 7, 'MSFT'), ('APPLE INC', 6, 'AAPL'), ('BROADCOM', 5, 'AVGO')]
    h[series['SOXX']] = [('NVIDIA CORP', 9, 'NVDA'), ('BROADCOM', 8, 'AVGO'), ('MICRON', 7, 'MU'), ('AMD', 6, 'AMD'), ('QUALCOMM', 5, 'QCOM')]
    # an EXTRA etf with many names: the first ones duplicate the originals and must be skipped; depth must be leaders+spare, not leaders
    h[series['SMH']] = [('NVIDIA CORP', 20, 'NVDA'), ('BROADCOM', 9, 'AVGO'), ('TAIWAN SEMI', 8, 'TSM'), ('ASML', 7, 'ASML'), ('KLA', 6, 'KLAC'), ('LAM', 5, 'LRCX'), ('AMAT', 4, 'AMAT')]
    rows = [dict(cik=1, name='Alphabet Inc.', ticker='GOOGL'), dict(cik=1, name='Alphabet Inc.', ticker='GOOG'), dict(cik=2, name='Unrelated', ticker='UNR')]
    meta, filt = make_readers(h)
    built = pool.build_pool(rows, today=date(2026, 10, 10), leaders=3, extra=True, dedupe=True, filings_reader=meta, holdings_reader=filt)
    assert built['leaders']['IYW'] == ['NVDA', 'GOOGL', 'MSFT']                                  # Alphabet counted once, resolved through the SEC title, top 3 only
    assert built['leaders']['SOXX'] == ['NVDA', 'AVGO', 'MU']                                    # originals are never deduplicated against each other
    assert built['leaders']['SMH'] == ['TSM', 'ASML', 'KLAC'] and built['skipped_duplicates']['SMH'] == ['NVDA', 'AVGO']
    base = pool.build_pool(rows, today=date(2026, 10, 10), leaders=3, extra=False, filings_reader=meta, holdings_reader=filt)
    assert 'SMH' not in base['leaders'] and base['leaders']['IYW'] == built['leaders']['IYW']   # switching the extras off leaves the originals unchanged


def test_unresolved_issuers_are_reported_not_guessed():
    series = pool.SERIES
    h = {s: [] for s in series.values()}
    h[series['XBI']] = [('Mystery Therapeutics Inc', 3, None), ('GILEAD', 2, 'GILD')]
    meta, filt = make_readers(h)
    built = pool.build_pool([dict(cik=9, name='Other', ticker='OTH')], today=date(2026, 10, 10), leaders=3, extra=False, filings_reader=meta, holdings_reader=filt)
    assert built['leaders']['XBI'] == ['GILD'] and built['unresolved_issuers']['XBI'] == ['Mystery Therapeutics Inc']


def test_sleeves_and_age():
    p = dict(built='2026-01-01T00:00:00+00:00', leaders={'IYW': ['NVDA', 'MSFT'], 'SOXX': ['NVDA']})
    assert pool.sleeves(p) == {'NVDA': {'IYW', 'SOXX'}, 'MSFT': {'IYW'}}


def test_share_classes_of_one_company_are_one_name_across_etfs():
    series = pool.SERIES
    h = {s: [] for s in series.values()}
    h[series['IYW']] = [('ALPHABET INC', 9, 'GOOGL'), ('MICROSOFT', 8, 'MSFT'), ('APPLE', 7, 'AAPL')]
    h[series['FDN']] = [('ALPHABET INC', 9, 'GOOG'), ('AMAZON', 8, 'AMZN'), ('NETFLIX', 7, 'NFLX')]
    rows = [dict(cik=1, name='Alphabet Inc.', ticker='GOOGL'), dict(cik=1, name='Alphabet Inc.', ticker='GOOG')]
    meta, filt = make_readers(h)
    built = pool.build_pool(rows, today=date(2026, 10, 10), leaders=3, extra=True, dedupe=True, filings_reader=meta, holdings_reader=filt)
    assert built['leaders']['FDN'] == ['AMZN', 'NFLX'] and 'GOOG' not in built['leaders']['FDN']          # Alphabet already led IYW as GOOGL
