"""Deep-dip bounce rider for btc-race-24h-20261115-0828 (agent-fun/bars-replay/v1).

Strategy, in one paragraph: long only, one position, full size. Wait for an
extreme washout -- the close at least 180 bps below its 45-bar high -- and buy
only once the price has already bounced at least 30 bps off its 10-bar low, so
the entry catches the bounce instead of the falling knife. Hold for the ride
up: sell at +350 bps over the entry fill, at -250 bps under it, or after 120
bars, whichever comes first. Five bars of cooldown after every sell.

Why this shape: on the dev window BTC fell about 4% with one violent V-bounce
(+352 bps in 30 minutes off the $62,082 low). Small dips keep falling (the
30-minute forward return after a -50..-100 bps drop is negative), so ordinary
dip-buying bleeds the ~30-42 bps round-trip cost; only extreme drops bounce,
and the money is in holding the bounce wide instead of scalping +50 bps.
Backtested on the frozen dev window (exact harness fill model: 4 bps fee,
2 bps slippage, constant-product impact vs the bar open depth, 1-bar latency):
net PnL +$45.05, 2 roundtrips, 2 wins, max drawdown 1.68%, fees $1.62.

Protocol: speaks agent-fun/bars-replay/v1 on stdin/stdout with no dependency
beyond the standard library. All decisions use integer basis-point math on the
raw prices, so two runs on the same bars produce byte-identical replies: no
clock, no environment, no unseeded randomness. A buy spends all cash; a sell
empties the whole position. max_slippage_bps is 300, a guard against gap
fills that never binds on the dev window.
"""

import json
import sys

SYMBOL = "CBBTC"
W_DIP = 45          # lookback bars for the dip (max close vs current close)
DIP_BPS = 180       # min dip from the 45-bar high, in bps
M_LOW = 10          # lookback bars for the recent low
OFF_LOW_BPS = 30    # min recovery off the 10-bar low, in bps
TP_BPS = 350        # take profit vs the entry fill
SL_BPS = 250        # stop loss vs the entry fill
HOLD_BARS = 120     # max bars held
COOLDOWN = 5        # bars after a sell before re-entering
MAX_SLIP_BPS = 300  # max slippage vs the latest close, in bps
MIN_NOTIONAL = 1000000  # 1 USDC, in quote units


def bps_down(ref, x):
    """Basis points x sits below ref (ref > 0)."""
    return (ref - x) * 10000 // ref


def bps_up(ref, x):
    """Basis points x sits above ref (ref > 0)."""
    return (x - ref) * 10000 // ref


class Bot:
    """One symbol, one position, full size, deep dips only."""

    def __init__(self, hello):
        self.hello = hello
        self.warmup = hello.get("warmup_bars", 0) or 0
        self.closes = []           # raw int closes of CBBTC, in bar order
        self.cash = int(hello["capital"]["quote_units"])
        self.pos_qty = 0           # raw int qty held
        self.entry_fill = 0        # raw int fill price of the open position
        self.entry_close = 0       # raw int close when the buy was decided
        self.entry_seq = -1        # seq of the batch that decided the buy
        self.last_sell_seq = -10 ** 9
        self.pending_buy = None    # client_id of a buy awaiting its fill
        self.next_id = 1

    def _cid(self):
        cid = "o%d" % self.next_id
        self.next_id += 1
        return cid

    def on_batch(self, batch):
        seq = batch["seq"]
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL:
                continue
            if fill.get("client_id") != self.pending_buy:
                continue
            if fill.get("status") == "filled" and fill.get("side") == "buy":
                self.entry_fill = int(fill["price"])
            self.pending_buy = None
        for bar in batch.get("bars", []):
            if bar.get("symbol") == SYMBOL:
                self.closes.append(int(bar["c"]))
        account = batch.get("account", {})
        self.cash = int(account.get("cash_quote_units", self.cash))
        self.pos_qty = 0
        for pos in account.get("positions", []):
            if pos.get("symbol") == SYMBOL:
                self.pos_qty = int(pos["qty"])
        if self.pos_qty == 0:
            self.entry_fill = 0
        orders = []
        if self.warmup and seq < self.warmup:
            return orders
        if not self.closes:
            return orders
        close = self.closes[-1]
        if self.pos_qty > 0:
            ref = self.entry_fill if self.entry_fill > 0 else self.entry_close
            if ref > 0:
                gain = bps_up(ref, close)
                held = seq - self.entry_seq
                if gain >= TP_BPS or gain <= -SL_BPS or held >= HOLD_BARS:
                    orders.append({"side": "sell", "client_id": self._cid(),
                                   "symbol": SYMBOL, "qty": str(self.pos_qty),
                                   "max_slippage_bps": MAX_SLIP_BPS})
                    self.last_sell_seq = seq
        elif self.pending_buy is None:
            if seq - self.last_sell_seq >= COOLDOWN and len(self.closes) >= W_DIP:
                hi = max(self.closes[-W_DIP:])
                if bps_down(hi, close) >= DIP_BPS:
                    lo = min(self.closes[-M_LOW:])
                    if bps_up(lo, close) >= OFF_LOW_BPS and self.cash >= MIN_NOTIONAL:
                        cid = self._cid()
                        orders.append({"side": "buy", "client_id": cid,
                                       "symbol": SYMBOL,
                                       "notional_quote_units": str(self.cash),
                                       "max_slippage_bps": MAX_SLIP_BPS})
                        self.pending_buy = cid
                        self.entry_seq = seq
                        self.entry_close = close
        return orders


def main():
    bot = None
    out = sys.stdout
    for line in sys.stdin.buffer:
        if not line.strip():
            continue
        msg = json.loads(line.decode("utf-8"))
        kind = msg.get("type")
        if kind == "hello":
            bot = Bot(msg)
        elif kind == "batch":
            try:
                orders = bot.on_batch(msg)
            except Exception as exc:  # never break the protocol on a bad bar
                print("on_batch error: %r" % (exc,), file=sys.stderr)
                orders = []
            reply = {"type": "reply", "seq": msg["seq"], "orders": orders}
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
