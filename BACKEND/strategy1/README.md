# strategy1：加强型 QQQ

这是 Bot 当前开放的策略 1，对应可安装的 `quant_strategy` Python 包。策略 2 仍在研发中。

## 技术说明

R5 + K1A + K5x leader-rotation strategy (ETF pool, 5 leaders per theme, 7 extra tech ETFs, overlap removed), ported from the QuantConnect version to live data:
Alpaca (prices, **read only**), SEC XBRL (point-in-time fundamentals), SEC N-PORT (ETF holdings).

- **Library, not a bot.** No Telegram, no database, no second process: the host bot imports it (see `docs/INTEGRATION.md`).
- **Suggestions only.** There is no order endpoint anywhere in the code (tested).
- **Shared run + per-user signals.** One market run per month; each user's signals use only that user's portfolio (the D component is per user).
- **Same settings as the QuantConnect baseline**: `decision_frequency='monthly'`, `min_buy_score=0` (no total-score floor), both in `config.STRATEGY` and part of the strategy version. This module only supports the monthly run.
- **CSV compatible** with the host bot (`Company,Shares Held,Average Price` + `current_cash,<mode>,<amount> USD`).

## Use

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp .env.example .env            # Alpaca keys, SEC contact
.venv/bin/pytest -q
.venv/bin/quant-strategy smoke                   # checks the connections
.venv/bin/quant-strategy refresh-pool            # leaders pool from the latest N-PORT filings
.venv/bin/quant-strategy run --csv portfolio.csv # one run + that portfolio's signals
```

## Layout

`quant_strategy/`: `config` · `logic` (score, K1A, K5x, sizing; same as the QC port) · `universe_data` · `pool` · `sec_fundamentals` · `alpaca_client` · `engine` (shared run) ·
`signals` (per user) · `portfolio_csv` · `versioning` · `messages` (plain text) · `cli`.

## Honest limits

Parameters were chosen on 2010-2025 data that was reused many times; 2020-2025 is not independent out-of-sample evidence. Past results are not a forecast. USD only, foreign filers not scored.
