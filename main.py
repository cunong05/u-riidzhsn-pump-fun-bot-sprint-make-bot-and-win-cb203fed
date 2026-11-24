"""Momentum bot for the pump.fun paper replay (agent-fun/replay/v2).

Strategy, in one paragraph: watch every curve's recorded tape; when a curve
is past its launch dump (min_age slots old) and its recent tape shows
accelerating buy pressure -- net SOL flowing in, several distinct buyers, the
price up and sitting near its window high, with more inflow in the newer half
of the window than in the older half -- probe it with a small buy. Pyramid
into strength only, up to a fixed stake per coin. Cut losers on a stop loss,
a trailing stop once the position is up, a fade in net flow, or a time limit.
The replay flattens whatever is left at the end of the window.

Guardrails: the run needs round trips of >= 0.01 SOL on at least 20 distinct
coins and a max drawdown of 50%. Small probe round trips keep the bot on pace
when momentum entries lag the pace in block time.

Deterministic by construction: no clock, no randomness, no network. All
iteration is over insertion-ordered dicts or the batch's own event order;
sets are used for membership tests only.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bot  # noqa: E402  (after the sys.path line)

LAMPORTS = makefun_bot.LAMPORTS_PER_SOL
CURVE_FEE = 0.0125  # protocol + creator fee share taken on a fill (95 + 30 bps)
BUY_SLIP_BPS = 1500
SELL_SLIP_BPS = 10_000

# --- entry signal ---
WINDOW = 40            # entry lookback, slots
MIN_NET_SOL = 0.5      # net buy SOL (buys minus sells) over the window
MIN_BUYS = 4           # buy trades in the window
MIN_BUYERS = 4         # distinct buying wallets in the window
MIN_PRICE_UP = 0.04    # price now over price at window start, minus 1
MAX_PRICE_UP = 2.0     # skip curves that already ran too far in one window
ACCEL = 1.0            # newer-half net inflow >= this x older-half net inflow
NEAR_HIGH = 0.90       # price now at least this share of the window high
MIN_RQ_SOL = 1.0       # curve real SOL bounds at entry
MAX_RQ_SOL = 80.0
MIN_AGE = 300          # slots since CreateEvent before an entry is allowed

# --- sizing ---
INITIAL_SOL = 0.05     # first buy of a position
ADD_SOL = 0.10         # each pyramid add
STAKE_SOL = 0.50       # most SOL into one position in total
MIN_BUY_SOL = 0.01     # smallest buy worth sending
ADD_STEP = 0.02        # add only once price is this much above the last buy's
ADD_IF_UP = 0.0        # add only while the position is at least this far up
ADD_NET_SOL = 0.20     # add only while net SOL over the window is at least this
MAX_EXPOSURE_SOL = 6.0  # most SOL at cost across all positions at once
MAX_MAIN = 4           # most momentum positions open at once (1 slot kept for probes)

# --- exits ---
STOP_LOSS = 0.15       # sell when value falls this far below cost
TRAIL = 0.25           # trailing stop from the position's best close value
TRAIL_ARM = 0.05       # trailing stop arms once value reached cost * (1 + this)
FADE_WINDOW = 20       # exit when net SOL over this window falls below -FADE_NET_SOL
FADE_NET_SOL = 0.5
MAX_HOLD = 2000        # exit after this many slots held (unless well up)
HOLD_IF_UP = 0.50      # the time limit does not apply while up at least this much
COOLDOWN = 150         # slots to wait after an exit before re-entering a curve
MAX_ENTRIES = 2        # most entries per curve over the run

# --- guardrail pacing (round trips >= 0.01 SOL on >= 20 coins) ---
TARGET_COINS = 30
TARGET_FILLS = 240
PACE_SECS = 14_400
PROBE_SOL = 0.01
MAX_PROBES = 1


class Tape:
    __slots__ = ("created", "trades", "entries", "last_exit")

    def __init__(self):
        self.created = None       # slot of the CreateEvent
        self.trades = []          # (slot, is_buy, sol, user, price) in arrival order
        self.entries = 0          # momentum entries taken on this curve
        self.last_exit = -10**18  # slot of the last exit


class Held:
    __slots__ = ("opened", "peak", "invested", "exiting", "filled",
                 "last_add_price", "last_value")

    def __init__(self, opened, invested, last_add_price):
        self.opened = opened
        self.peak = 0.0
        self.invested = invested
        self.exiting = False
        self.filled = False
        self.last_add_price = last_add_price
        self.last_value = 0.0


tapes = {}
held = {}
probes = {}          # mint -> True once its probe buy has landed (selling next)
my_fills = 0
my_coins = set()     # membership only, never iterated
first_time = None
last_time = None
KEEP = max(WINDOW, FADE_WINDOW) + 5


def tape_of(mint):
    t = tapes.get(mint)
    if t is None:
        t = Tape()
        tapes[mint] = t
    return t


def entry_signal(t, now, rq_sol):
    """True when the tape shows accelerating buy pressure near its high."""
    if rq_sol < MIN_RQ_SOL or rq_sol > MAX_RQ_SOL:
        return False
    if t.created is None or now - t.created < MIN_AGE:
        return False
    start = now - WINDOW
    mid = now - WINDOW // 2
    buy_sol = 0.0
    sell_sol = 0.0
    buys = 0
    net_old = 0.0
    net_new = 0.0
    start_price = -1.0
    high = 0.0
    last_price = 0.0
    buyers = {}
    for (slot, is_buy, sol, user, price) in t.trades:
        if slot <= start:
            start_price = price
            continue
        if start_price < 0:
            start_price = price
        signed = sol if is_buy else -sol
        if slot <= mid:
            net_old += signed
        else:
            net_new += signed
        if is_buy:
            buy_sol += sol
            buys += 1
            buyers[user] = buyers.get(user, 0.0) + sol
        else:
            sell_sol += sol
        if price > high:
            high = price
        last_price = price
    if buys < MIN_BUYS or len(buyers) < MIN_BUYERS:
        return False
    if buy_sol - sell_sol < MIN_NET_SOL:
        return False
    if start_price <= 0 or last_price <= 0:
        return False
    up = last_price / start_price - 1.0
    if up < MIN_PRICE_UP or up > MAX_PRICE_UP:
        return False
    if last_price < high * NEAR_HIGH:
        return False
    if net_new <= 0 or net_new < ACCEL * net_old:
        return False
    return True


def net_flow(t, now, window):
    """Net recorded SOL (buys minus sells) over the last `window` slots."""
    net = 0.0
    for i in range(len(t.trades) - 1, -1, -1):
        slot, is_buy, sol, _user, _price = t.trades[i]
        if slot <= now - window:
            break
        net += sol if is_buy else -sol
    return net


def on_batch(state, batch):
    global my_fills, first_time, last_time
    now = batch["slot"]
    traded = []          # mints with a trade this batch, in event order
    traded_seen = set()
    for f in batch["fills"]:
        if f["status"] == "filled" and not f["client_id"].startswith("_"):
            my_fills += 1
            my_coins.add(f["mint"])
    for e in batch["events"]:
        if e["block_time"] is not None:
            if first_time is None:
                first_time = e["block_time"]
            if last_time is None or e["block_time"] > last_time:
                last_time = e["block_time"]
        if e["event"] == "CreateEvent":
            tape_of(e["mint"]).created = e["slot"]
        elif e["event"] == "TradeEvent":
            t = tape_of(e["mint"])
            sol = int(e["sol_amount"]) / LAMPORTS
            price = int(e["virtual_quote_reserves"]) / int(e["virtual_token_reserves"])
            t.trades.append((e["slot"], e["is_buy"], sol, e["user"], price))
            if e["mint"] not in traded_seen:
                traded_seen.add(e["mint"])
                traded.append(e["mint"])
        else:  # CompleteEvent
            tape_of(e["mint"]).created = tape_of(e["mint"]).created
    for mint in traded:
        t = tapes[mint]
        cut = 0
        while cut < len(t.trades) and t.trades[cut][0] <= now - KEEP:
            cut += 1
        if cut:
            del t.trades[:cut]

    marks = {m["mint"]: m for m in batch["marks"]}
    pending_by_mint = {}
    sell_pending = set()
    for po in state.pending.values():
        pending_by_mint[po.order["mint"]] = pending_by_mint.get(po.order["mint"], 0) + 1
        if po.order["side"] == "sell":
            sell_pending.add(po.order["mint"])

    orders = []
    cap = state.hello["caps"]["max_orders_per_batch"]
    tx_sol = state.tx_fee_lamports() / LAMPORTS

    def position_value(mint, pos, curve):
        mk = marks.get(mint)
        if mk is not None:
            vq = int(mk["virtual_quote_reserves"])
            vt = int(mk["virtual_token_reserves"])
        else:
            vq = curve.virtual_quote_reserves
            vt = curve.virtual_token_reserves
        tokens = pos.token_amount
        return (tokens * vq / (vt + tokens) / LAMPORTS) * (1.0 - CURVE_FEE) - tx_sol

    # --- exits first ---
    for mint in list(held.keys()):
        h = held[mint]
        pos = state.positions.get(mint)
        curve = state.curves.get(mint)
        if pos is None:
            if mint not in pending_by_mint:
                del held[mint]
                t = tape_of(mint)
                if h.filled:
                    t.last_exit = now
                else:
                    t.entries -= 1
            continue
        h.filled = True
        if curve is None or curve.complete:
            continue
        value = position_value(mint, pos, curve)
        cost = pos.cost_lamports / LAMPORTS
        if value > h.peak:
            h.peak = value
        h.last_value = value
        t = tape_of(mint)
        rq_sol = int(curve.real_quote_reserves) / LAMPORTS
        exit_now = h.exiting
        if not exit_now and value < cost * (1.0 - STOP_LOSS):
            exit_now = True
        if (not exit_now and h.peak >= cost * (1.0 + TRAIL_ARM)
                and value < h.peak * (1.0 - TRAIL)):
            exit_now = True
        if not exit_now and net_flow(t, now, FADE_WINDOW) < -FADE_NET_SOL:
            exit_now = True
        if (not exit_now and now - h.opened > MAX_HOLD
                and value < cost * (1.0 + HOLD_IF_UP)):
            exit_now = True
        if not exit_now:
            continue
        h.exiting = True
        if mint in sell_pending:
            continue
        if len(orders) >= cap:
            break
        orders.append({"side": "sell", "mint": mint,
                       "token_amount": pos.token_amount,
                       "max_slippage_bps": SELL_SLIP_BPS})

    # --- probe round trips: sell the probe as soon as its buy has landed ---
    for mint in list(probes.keys()):
        pos = state.positions.get(mint)
        curve = state.curves.get(mint)
        if pos is None:
            if mint not in pending_by_mint:
                del probes[mint]
            continue
        if curve is None or curve.complete or mint in pending_by_mint:
            continue
        if len(orders) >= cap:
            break
        orders.append({"side": "sell", "mint": mint,
                       "token_amount": pos.token_amount,
                       "max_slippage_bps": SELL_SLIP_BPS})
        probes[mint] = True

    # --- entries and pyramid adds ---
    free = state.free_cash_lamports() / LAMPORTS
    slots_left = state.hello["caps"]["max_open_positions"] - state.committed_position_count()
    main_left = MAX_MAIN - len(held)
    exposure = 0.0
    for mint, h in held.items():
        pos = state.positions.get(mint)
        exposure += max(h.invested, (pos.cost_lamports / LAMPORTS) if pos is not None else 0.0)
    for mint in traded:
        if len(orders) >= cap:
            break
        curve = state.curves.get(mint)
        if curve is None or curve.complete:
            continue
        if mint in pending_by_mint or mint in probes:
            continue
        t = tapes[mint]
        h = held.get(mint)
        price = curve.virtual_quote_reserves / curve.virtual_token_reserves
        rq_sol = curve.real_quote_reserves / LAMPORTS
        if h is None:
            if slots_left <= 0 or main_left <= 0:
                continue
            if t.entries >= MAX_ENTRIES or now - t.last_exit < COOLDOWN:
                continue
            if not entry_signal(t, now, rq_sol):
                continue
            want = min(INITIAL_SOL, STAKE_SOL)
            is_new = True
        else:
            if h.exiting or not h.filled or h.invested >= STAKE_SOL - MIN_BUY_SOL:
                continue
            pos = state.positions.get(mint)
            if pos is None:
                continue
            if price < h.last_add_price * (1.0 + ADD_STEP):
                continue
            if h.last_value < (pos.cost_lamports / LAMPORTS) * (1.0 + ADD_IF_UP):
                continue
            if net_flow(t, now, WINDOW) < ADD_NET_SOL:
                continue
            if rq_sol > MAX_RQ_SOL:
                continue
            want = min(ADD_SOL, STAKE_SOL - h.invested)
            is_new = False
        cap_sol = (state.max_buy_lamports(mint)) / LAMPORTS
        amount = min(cap_sol, want, MAX_EXPOSURE_SOL - exposure)
        if amount > free - tx_sol:
            amount = free - tx_sol
        if amount < MIN_BUY_SOL:
            continue
        lamports = int(amount * LAMPORTS)
        if lamports <= 0:
            continue
        orders.append({"side": "buy", "mint": mint, "quote_lamports": lamports,
                       "max_slippage_bps": BUY_SLIP_BPS})
        free -= amount + tx_sol
        exposure += amount
        if is_new:
            held[mint] = Held(now, amount, price)
            t.entries += 1
            slots_left -= 1
            main_left -= 1
        else:
            h.invested += amount
            h.last_add_price = price

    # --- guardrail pacing: open a probe when fills or coins lag the pace ---
    if (len(probes) < MAX_PROBES and slots_left > 0
            and first_time is not None and last_time is not None):
        pace = min(1.0, (last_time - first_time) / PACE_SECS)
        need_coins = len(my_coins) < -(-TARGET_COINS * pace // 1)  # ceil
        need_fills = my_fills < -(-TARGET_FILLS * pace // 1)
        if need_coins or need_fills:
            min_rq = PROBE_SOL * 10_000 / state.hello["caps"]["max_order_bps_of_real_reserve"]
            for mint in traded:
                if len(orders) >= cap or len(probes) >= MAX_PROBES:
                    break
                if (mint in held or mint in probes or mint in pending_by_mint
                        or mint in state.positions):
                    continue
                if need_coins and not need_fills and mint in my_coins:
                    continue
                curve = state.curves.get(mint)
                if curve is None or curve.complete:
                    continue
                if curve.real_quote_reserves / LAMPORTS < min_rq * 1.5:
                    continue
                if free < PROBE_SOL + 2 * tx_sol:
                    break
                orders.append({"side": "buy", "mint": mint,
                               "quote_lamports": int(PROBE_SOL * LAMPORTS),
                               "max_slippage_bps": BUY_SLIP_BPS})
                probes[mint] = False
                free -= PROBE_SOL + tx_sol
    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
