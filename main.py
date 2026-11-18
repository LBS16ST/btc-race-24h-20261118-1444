"""Dip-grid bot for make.fun btc-race-24h-20261118-1444 (CBBTC/USDC, 1m bars).

Original strategy, designed and tuned on the published 24h dev window
(1,440 one-minute bars) with an exact local replay of the harness's fill
model (4 bps fee, 2 bps slippage, constant-product impact vs the bar's open
depth, fills at the next bar's open).

Idea: BTC's dev window is a downtrend with sharp intraday drops that
recover toward their 60-minute average. The bot buys a drop in tranches
and sells the whole position when the price recovers to its 60-minute
simple moving average (or on a profit target / disaster stop / time exit /
forced flat near the end of the window). It stays in cash the rest of the
time, so it never rides a falling market the way buy-and-hold does.

Rules (all integer arithmetic, fully deterministic — no clock, no files,
no environment, no randomness):
  * Anchor: SMA of the last 60 closes.
  * First tranche: when the close is at least 70 bps below the SMA, buy
    the first tranche of the cash on hand.
  * Scale in: each further tranche when the close falls another 40 bps
    below the last buy's fill price. Tranches are WEIGHTED toward the
    cheaper, later buys (weights 1, 1, 2, 2 of the cash still unspent),
    so the notional-weighted average fill sits lower than with equal
    tranches and a recovery to the SMA earns more per dollar deployed;
    it also loses less when a dip is shallow, because less capital is
    committed at the higher early prices.
  * Exit the whole position when any of these holds:
  * Exit the whole position when any of these holds:
      - close back at or above the SMA (recovery complete);
      - close at least 80 bps above the notional-weighted average fill;
      - close at least 300 bps below the average fill (disaster stop);
      - 300 bars since the first fill;
      - batch seq >= 1380 (forced flat before the window ends).
  * No new entries after batch seq 1320, so no position is opened that
    cannot be exited before the forced flat.
  * One symbol (CBBTC), long only, at most one order per reply,
    max_slippage_bps 200.

Measured on the dev window with the local replay (see README.md):
net PnL +9.06 USDC, 7 filled orders, max drawdown 0.60%.
That is a dev-window measurement only; the race window is unseen prices
and this bot may lose money there.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- strategy parameters (tuned on the dev window; see README.md) ---
SMA_N = 60            # anchor: simple moving average of the last 60 closes
DIP0_BPS = 70         # first tranche: close this far below the SMA
STEP_BPS = 40         # further tranches: close this far below the last fill
# Tranche weights (fractions of the cash still unspent, in order): the
# later tranches are twice the early ones, so more capital goes in at the
# cheaper prices of a deepening dip. TRANCHES is the number of weights.
TRANCHE_WEIGHTS = (1, 1, 2, 2)
TRANCHES = len(TRANCHE_WEIGHTS)
TARGET_BPS = 80       # exit: close this far above the average fill
STOP_BPS = 300        # exit: close this far below the average fill
TIME_EXIT_BARS = 300  # exit: this many batches after the first fill
FLAT_BY_SEQ = 1380    # exit: sell everything once the batch seq reaches this
NO_ENTRY_AFTER_SEQ = 1320  # do not open (or add to) a position after this seq
MAX_SLIPPAGE_BPS = 200
MIN_NOTIONAL = 1000000  # 1 USDC, in quote units (the harness's minimum)


class DipGridBot:
    def __init__(self):
        self.closes = []          # int closes, latest last
        self.buys = []            # (fill price, notional) of the open position
        self.first_seq = None     # seq of the batch that delivered the first fill
        self.last_buy_price = None

    def _average_fill(self):
        """Notional-weighted average fill price of the open position (int)."""
        total = 0
        weighted = 0
        for price, notional in self.buys:
            total += notional
            weighted += price * notional
        return weighted, total

    def _tranche_amount(self, cash, n_buys):
        """Cash to spend on tranche n_buys (0-based): its weight over the
        weights still to go, so the whole cash is deployed across the
        tranches and the later (cheaper) ones get twice the early ones."""
        weights = TRANCHE_WEIGHTS[n_buys:]
        return cash * TRANCHE_WEIGHTS[n_buys] // sum(weights)

    def on_batch(self, state, batch):
        # Fold in this batch's fills (buys record their fill price; a sell
        # empties the position). Rejected fills are ignored: the tranche
        # simply was not bought, and the next batch may try again.
        for fill in batch.get("fills", []):
            if fill.get("status") != "filled" or fill.get("symbol") != SYMBOL:
                continue
            if fill["side"] == "buy":
                price = int(fill["price"])
                self.buys.append((price, int(fill["notional_quote_units"])))
                self.last_buy_price = price
                if self.first_seq is None:
                    self.first_seq = state.seq
            else:
                self.buys = []
                self.first_seq = None
                self.last_buy_price = None
        # The harness is the source of truth for whether we hold anything.
        if SYMBOL not in state.positions:
            self.buys = []
            self.first_seq = None
            self.last_buy_price = None

        bar = state.latest.get(SYMBOL)
        if bar is None:
            return []
        close = int(bar["c"])
        self.closes.append(close)

        if state.warming_up:
            return []
        if len(self.closes) < SMA_N:
            return []

        # Anchor: sum of the last SMA_N closes (kept as an integer sum so
        # every comparison below is exact integer arithmetic).
        sma_sum = sum(self.closes[-SMA_N:])
        n_buys = len(self.buys)
        seq = state.seq

        if n_buys == 0:
            if seq > NO_ENTRY_AFTER_SEQ:
                return []
            # First tranche: close at least DIP0_BPS below the SMA.
            if close * SMA_N * 10000 < sma_sum * (10000 - DIP0_BPS):
                amount = self._tranche_amount(state.cash, 0)
                if amount >= MIN_NOTIONAL:
                    return [{"side": "buy", "symbol": SYMBOL,
                             "notional_quote_units": amount,
                             "max_slippage_bps": MAX_SLIPPAGE_BPS}]
            return []

        # Holding: exits first, then a possible scale-in.
        weighted, total = self._average_fill()
        if total == 0:
            return []
        # Recovery: close back at or above the SMA.
        if close * SMA_N >= sma_sum:
            return self._sell_all(state)
        # Profit target on the average fill.
        if close * total * 10000 >= weighted * (10000 + TARGET_BPS):
            return self._sell_all(state)
        # Disaster stop on the average fill.
        if close * total * 10000 <= weighted * (10000 - STOP_BPS):
            return self._sell_all(state)
        # Time exit.
        if self.first_seq is not None and seq - self.first_seq >= TIME_EXIT_BARS:
            return self._sell_all(state)
        # Forced flat before the window ends.
        if seq >= FLAT_BY_SEQ:
            return self._sell_all(state)
        # Scale in: close a further STEP_BPS below the last buy's fill.
        if (n_buys < TRANCHES and seq <= NO_ENTRY_AFTER_SEQ
                and self.last_buy_price is not None
                and close * 10000 <= self.last_buy_price * (10000 - STEP_BPS)):
            amount = self._tranche_amount(state.cash, n_buys)
            if amount >= MIN_NOTIONAL:
                return [{"side": "buy", "symbol": SYMBOL,
                         "notional_quote_units": amount,
                         "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []

    def _sell_all(self, state):
        holding = state.positions.get(SYMBOL)
        if holding is None or holding.qty <= 0:
            return []
        return [{"side": "sell", "symbol": SYMBOL, "qty": holding.qty,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]


def main():
    bot = DipGridBot()
    makefun_bars.run(bot.on_batch)


if __name__ == "__main__":
    main()
