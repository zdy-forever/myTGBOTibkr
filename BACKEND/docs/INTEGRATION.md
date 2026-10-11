# Integrating the strategy module into the multi-user bot (Phase 2)

The module is a **library**. It has no Telegram, no database and no scheduler, and it starts no process: the host bot (single process, long polling) imports it.
It never places an order; every output is a suggestion with a disclaimer.

## Contract

```python
from quant_strategy import StrategyEngine, AlpacaReadOnly, SecClient, config, parse_portfolio_csv, signals_for_user

settings = config.from_env()
engine = StrategyEngine(settings, AlpacaReadOnly(settings.alpaca_key, settings.alpaca_secret, settings.alpaca_trading_url, settings.alpaca_data_url, settings.alpaca_feed),
                        SecClient(settings.sec_user_agent, config.CACHE_DIR))

run = engine.run(extra_symbols=all_symbols_held_by_any_user)    # ONE shared run (blocking, minutes on a cold cache) -> RunResult
payload = run.to_dict()                                         # plain JSON: store it
us = signals_for_user(run, portfolio, settings, deposit_usd=None, blocked=None)   # PER USER, fast, no network -> UserSignals
us.to_dict()                                                    # plain JSON: store it
```

| Piece | Input | Output | Network |
|---|---|---|---|
| `StrategyEngine.run` | symbols to price in addition to the pool | `RunResult` (status `ok`/`stale`/`failed`, pool, per-stock P/V/F/Q/beta, prices, notes, strategy name + version) | Alpaca read, SEC, N-PORT (pool only, monthly) |
| `signals_for_user` | the shared run + **one** user's portfolio | `UserSignals` (status `ok`/`no_candidates`/`stale_data`/`run_failed`, list of `Signal`, summary, notes, disclaimer) | none |
| `parse_portfolio_csv` | the host CSV text | `Portfolio` (or `PortfolioCsvError` with line numbers) | none |

The portfolio can also be built from the host's DB rows: `Portfolio(holdings={'ISRG': Holding('ISRG', 0.55, 374.21)}, cash=1000.0)`.
`run` never raises: a failure comes back as `status='failed'` with a note, so the host can store it.

## Mapping used by the bot (BOT/src/quantbot/strategy_service.py)

- `strategy_runs`: one shared run (`trading_date` = last price bar, `strategy_version`, `status` completed/failed/skipped, `metadata_json` = `RunResult.to_dict()`).
- `signals`: one shared reference row per stock that appears in some user's suggestion (price, raw P/V/F, `action` = `HOLD`: no shared action, because buy/sell depends on each user's portfolio).
- `user_signals`: one row per user and stock with the user's own `action` and a Chinese `recommendation` text. Users are scored one at a time from their own positions and cash only; the money to invest is the user's cash balance.

## User isolation

`signals_for_user` reads only the portfolio passed in and the shared run. The D (diversification) component, the only one that depends on a portfolio, is computed per user and is not
stored in the shared run. Do not pass one user's portfolio when computing another's, and do not put users' holdings in log lines or notifications of other users.
`extra_symbols` asks for prices and SEC fundamentals, with no quantities or owners. Outside-pool components live in `RunResult.holding_symbols`, separately from the buy pool;
missing-data reasons live in `holding_unscored`. Their peer reference is the existing buy pool; they never enter its peer samples. Per-user scoring includes only the caller's own
outside holdings, which may be sold but never become buy candidates. Legacy run payloads without these fields remain readable.

## Example handlers (python-telegram-bot, adapt names to the host)

```python
async def signals_cmd(update, context):
    user = load_user(update.effective_user.id)                       # host code
    run = latest_ok_run()                                            # newest strategy_runs row with status 'ok', rebuilt with RunResult.from_dict
    if run is None:
        return await update.message.reply_text('No strategy run is available yet.')
    us = signals_for_user(run, portfolio_of(user), settings, deposit_usd=plan_amount(user), blocked=recent_sales(user))
    save_user_signals(user, run, us)
    await update.message.reply_text(messages.signals_text(us))        # plain text, no markup

async def runstrategy_cmd(update, context):                          # admin only (server-side whitelist, as the host already does)
    run = await asyncio.to_thread(engine.run, held_symbols_of_all_users())
    save_run(run)
    await update.message.reply_text(messages.run_text(run))
```

`engine.run` blocks for minutes on a cold SEC cache, so call it through `asyncio.to_thread` or the host's job queue, never inside the event loop.

## Scheduling

The host's hourly loop uses each user's selected monthly US trading-day ordinal (default 1, set by `/auto_notice_date`). On each scheduled day it runs the shared analysis once,
then computes `user_signals` only for that day's subscribers. Per-user completion months and attempt counters are persisted in `notification_preferences`; a completed market run alone
does not complete every user's month. Months without the selected ordinal are skipped. Market calendar failures are retried without guessing holiday dates.
Run it in the same process; no second bot process is needed. Only the pool refresh (N-PORT, from the Hugging Face mirror) touches the larger download; it happens at most once per 400 days or on `refresh_pool=True`.

## `blocked`

Names a user sold recently are not bought back for 180 days. The host knows this from its `transactions` table: pass `{symbol: datetime_until_blocked}`; omit it if not needed.

## Limits worth knowing

- USD only: other currencies are ignored with a note, never converted.
- Free IEX volume understates liquidity: set `DOLLAR_VOLUME_MULTIPLIER` or use the `sip` feed.
- Names without SEC US-GAAP data (foreign filers) are listed as not scored, never guessed.
- Research results (2010-2025) were selected on historical data; this is not a forecast and not investment advice.
