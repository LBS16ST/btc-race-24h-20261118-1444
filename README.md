# Dip-Grid Bot — btc-race-24h-20261118-1444

An original long-only trading bot for the make.fun competition
"Race a BTC trading bot live for 24 hours". It trades Coinbase Wrapped BTC
(CBBTC) against USDC, one minute at a time, starting from 1,000 USDC paper
money. Python 3, standard library only; the entrypoint is `main.py`, run as
`python3 -P -s /bot/main.py`. The protocol shim in `vendor/` is the kit's
own `sdk/makefun_bars.py`, copied byte-for-byte.

## Strategy

BTC's dev window is a downtrend with sharp intraday drops that recover
toward their 60-minute average. The bot buys a drop in tranches and sells
the whole position when the price recovers to that average. It stays in
cash the rest of the time and never rides a falling market the way
buy-and-hold does.

- **Anchor:** simple moving average of the last 60 closes.
- **Entry, first tranche:** when the close is at least 70 bps below the
  SMA, buy the first tranche of the cash on hand.
- **Scale in:** each further tranche (up to 4) when the close falls another
  40 bps below the last buy's fill price. Tranches are weighted toward
  the cheaper, later buys (weights 1, 1, 2, 2 of the cash still unspent),
  so the average fill sits lower than with equal tranches and a recovery
  to the SMA earns more per dollar; it also loses less on a shallow dip,
  because less capital is committed at the higher early prices.
- **Exit the whole position when any of these holds:**
  - the close is back at or above the SMA (recovery complete);
  - the close is at least 80 bps above the notional-weighted average fill;
  - the close is at least 300 bps below the average fill (disaster stop);
  - 300 batches have passed since the first fill;
  - the batch seq reaches 1380 (forced flat before the window ends).
- **No new entries after batch seq 1320**, so no position is opened that
  cannot be exited before the forced flat.
- One symbol (CBBTC), long only, at most one order per reply,
  `max_slippage_bps` 200. All decisions are exact integer arithmetic;
  the bot reads no clock, no files and no environment and uses no
  randomness, so two runs on the same data produce identical replies.

The design was chosen by testing several original strategy families on
the dev window (plain dip-buying, turn-confirmation entries, slow
trend-following, 60-minute return regime switching, and the dip-grid).
Momentum/breakout entries and single-shot dip buys all lost money after
the ~26 bps round-trip cost; the tranched dip-grid was the only family
that stayed profitable. A later revision weights the tranches toward the
cheaper, later buys (1, 1, 2, 2 of the cash still unspent), which lifts
the dev PnL to +9.06 USDC, lowers the drawdown, and also turns a
30%-shallower version of the first-hour drop from a small loss into a
small profit; the parameters sit in the middle of a broad plateau, not
on a knife edge.

## Measured results (dev window, 1,440 bars)

Graded locally with a faithful replay of the harness's own fill model
(4 bps fee, 2 bps slippage, constant-product impact against each bar's
open depth, fills at the next bar's open, flatten at the last close),
driving the real `main.py` over stdin/stdout exactly as the harness
would. `grade-local --driver docker` could not be used because Docker
is not installed in this environment; `--driver process` only runs Node
bots.

| metric | value |
| --- | --- |
| Net PnL | **+9.06 USDC** (start 1,000 → 1,009.06) |
| Filled orders | 7 (two round trips) |
| Fees paid | 0.94 USDC |
| Max drawdown | 0.60% |
| Rejected orders | 0 |
| Determinism | two runs produced byte-identical replies |

What the 7 orders were: four weighted buys during the sharp drop in the
first hour (batch seq 59–68) — $166.67, $166.67, $333.33 and $333.33,
filled at $64,220, $63,870, $63,677 and $63,453 — sold in full at seq 85
once the price recovered to its 60-minute average, for $1,008.11; then
one $168.02 buy at seq 131, sold at seq 141 on the same recovery rule.
The bot then stayed in cash for the rest of the window — first-half
PnL +9.06 USDC, second-half PnL 0.00 USDC, no position left to flatten
at the end.

For comparison, buy-and-hold on the same window loses about 28 USDC
(BTC fell 2.8% over the window).

## Honest limits

- A dev-window profit does **not** guarantee a race-window profit. The
  race window is 24 hours of prices nobody has seen; this bot's edge on
  the dev window comes almost entirely from one event (the first-hour
  drop and its recovery). If the race window has no comparable drop, the
  bot may trade little or nothing and score near zero.
- The bot only trades CBBTC/USDC, long only, and can only profit from
  upward moves after a dip. In a steady grind down it mostly sits in
  cash; in a steady grind up without dips it may never enter.
- All numbers above are from a local replay, not from the server's
  grader. The server's dev grade is the authoritative check.
- The window-length assumptions (1,440 bars, forced flat at seq 1380,
  no entries after seq 1320) are for a 24-hour race of 1-minute bars,
  matching the dev window and the competition's stated 24-hour race.
