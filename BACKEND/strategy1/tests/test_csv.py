import pytest

from quant_strategy import PortfolioCsvError, parse_portfolio_csv

GOOD = 'Company,Shares Held,Average Price\ncurrent_cash,2,"1,000.50 USD"\nISRG,0.55,374.21 USD\nnvda,3,120 USD\n'


def test_host_format_parses_cash_and_holdings_and_ignores_the_mode_column():
    p = parse_portfolio_csv(GOOD)
    assert p.cash == 1000.5 and p.currency == 'USD' and p.cash_mode == 0
    assert set(p.holdings) == {'ISRG', 'NVDA'} and p.holdings['ISRG'].shares == 0.55 and p.holdings['ISRG'].exchange == 'US'


def test_optional_date_column_and_bom():
    p = parse_portfolio_csv('﻿Company,Shares Held,Average Price,Date\nAAA,1,10 USD,20240105\n')
    assert str(p.holdings['AAA'].acquired_on) == '2024-01-05'


def test_errors_are_collected_with_line_numbers_and_nothing_is_returned():
    bad = 'Company,Shares Held,Average Price\nAAA,-1,10 USD\nBBB,1,x USD\nAAA,1,10 USD\ncurrent_cash,9,5 USD\n'
    with pytest.raises(PortfolioCsvError) as e:
        parse_portfolio_csv(bad)
    lines = [x.split(' ')[1] for x in e.value.problems]
    assert lines == ['2', '3']                                  # the cash row's second column (9) is not an error any more


def test_missing_columns_and_non_usd_warning():
    with pytest.raises(PortfolioCsvError):
        parse_portfolio_csv('Name,Qty\nA,1\n')
    p = parse_portfolio_csv('Company,Shares Held,Average Price\nAAA,1,10 EUR\n')
    assert p.warnings and 'EUR' in p.warnings[0]
