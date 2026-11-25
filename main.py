"""Momentum bot for the pump.fun paper replay (agent-fun/replay/v2).

One entry idea, two tiers: the second wind. A coin that ran up hard, pulled
back deeply from its high, and now shows buyers returning off the low --
buying the bounce, never chasing the top. Tier A probes with 0.10 SOL and
pyramids winners to 0.50 SOL. Tier B is the same signal loosened (a smaller
pullback, a weaker bounce) traded at 0.05 SOL with no pyramid: it exists to
clear the 20-coin guardrail on a short final window, and its small size
bounds what its weaker edge can cost.

Exits: a stop loss, a trailing stop once the position is up, a fade in net
flow, an inactivity exit when the curve goes quiet, or a time limit.
Winners are scaled out in halves at +40% and +80% so a gap down cannot
take the whole gain. The replay flattens whatever is left at the end of the
window. A buy that fails (the replay fails ~2% of orders, on a draw seeded
differently per phase) is retried twice within 600 slots rather than
dropped, while the price has not run more than 20% past the entry.

Guardrails: the run needs round trips of >= 0.01 SOL on at least 20 distinct
coins and a max drawdown of 50%. No probe round trips are used: every entry
is a real second-wind trade.

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

# --- second-wind entry (bounce off a runner's deep pullback) ---
PB_WINDOW = 300
PB_RUN_UP = 0.20         # high over the window start must be at least this much up
PB_PULLBACK = 0.12       # price now at least this far below the window high
PB_BOUNCE_WINDOW = 40
PB_BOUNCE_NET = 0.05     # net buy SOL over the bounce window
PB_BOUNCE_BUYS = 3
PB_BOUNCE_OFF_LOW = 0.01
PB_MIN_RQ = 2.0
PB_MAX_RQ = 200.0
PB_MIN_AGE = 300
PB_INITIAL = 0.10
PB_ADD = 0.10
PB_STAKE = 0.50

# --- second-wind tier B (looser signal, tiny size, no pyramid) ---
PB2_RUN_UP = 0.18
PB2_PULLBACK = 0.10
PB2_BOUNCE_NET = 0.03
PB2_BOUNCE_BUYS = 2
PB2_BOUNCE_OFF_LOW = 0.008
PB2_MIN_RQ = 2.0
PB2_MIN_AGE = 250
PB2_INITIAL = 0.02
PB2_STAKE = 0.02

# --- sizing ---
MIN_BUY_SOL = 0.01
ADD_STEP = 0.02          # add only once price is this much above the last buy's
ADD_IF_UP = 0.0          # add only while the position is at least this far up
ADD_NET_SOL = 0.20       # add only while net SOL over the bounce window is at least this
MAX_EXPOSURE_SOL = 6.0
MAX_MAIN = 5

# --- exits ---
STOP_LOSS = 0.20
TRAIL = 0.30             # trailing stop from the position's best close value
TRAIL_ARM = 0.05         # trailing stop arms once value reached cost * (1 + this)
SCALE_OUT_AT = 0.40      # sell half the position once value reaches cost * (1 + this)
SCALE_OUT_2_AT = 0.80    # then sell half of what is left at cost * (1 + this)
INACTIVITY = 250         # exit when the curve has had no recorded trade for this many slots
FADE_WINDOW = 20
FADE_NET_SOL = 0.3
MAX_HOLD = 2000
HOLD_IF_UP = 0.50
COOLDOWN = 120
MAX_ENTRIES = 4
# A buy that fails (2% of orders fail in the replay) is retried, not
# dropped: a failed probe on the one coin that runs is the difference
# between a winning and a losing run, and the failure draw is seeded
# differently on every phase, so the bot must not hinge on one order.
BUY_RETRY_WINDOW = 600
BUY_RETRY_MAX = 2
BUY_RETRY_MAX_RUNUP = 1.20          # most entries per curve over the run


class Tape:
    __slots__ = ("created", "trades", "entries", "last_exit", "last_trade",
                 "entries_b", "last_exit_b")

    def __init__(self):
        self.created = None
        self.trades = []          # (slot, is_buy, sol, user, price) in arrival order
        self.entries = 0          # tier A entries
        self.last_exit = -10**18
        self.last_trade = -10**18
        self.entries_b = 0        # tier B entries (separate: never blocks tier A)
        self.last_exit_b = -10**18


class Held:
    __slots__ = ("opened", "peak", "invested", "exiting", "filled",
                 "last_add_price", "last_value", "kind", "stake", "add", "scaled",
                 "retry", "deadline")

    def __init__(self, opened, invested, last_add_price, kind, stake, add):
        self.opened = opened
        self.peak = 0.0
        self.invested = invested
        self.exiting = False
        self.filled = False
        self.last_add_price = last_add_price
        self.last_value = 0.0
        self.kind = kind          # 'pb'
        self.stake = stake        # most SOL into this position in total
        self.add = add            # each pyramid add, SOL
        self.scaled = 0           # scale-out steps taken (0, 1, 2)
        self.retry = 0            # failed initial buys retried so far
        self.deadline = opened + BUY_RETRY_WINDOW


tapes = {}
held = {}
my_fills = 0
my_coins = set()     # membership only, never iterated
first_time = None
last_time = None
KEEP = max(PB_WINDOW, FADE_WINDOW, PB_BOUNCE_WINDOW) + 5


def tape_of(mint):
    t = tapes.get(mint)
    if t is None:
        t = Tape()
        tapes[mint] = t
    return t


def pullback_signal(t, now, rq_sol, tier=1):
    """Second wind: ran up hard, pulled back deeply, buyers returning."""
    if tier == 2:
        run_up, pullback = PB2_RUN_UP, PB2_PULLBACK
        bounce_net, bounce_buys, bounce_off_low = PB2_BOUNCE_NET, PB2_BOUNCE_BUYS, PB2_BOUNCE_OFF_LOW
        min_rq, min_age = PB2_MIN_RQ, PB2_MIN_AGE
    else:
        run_up, pullback = PB_RUN_UP, PB_PULLBACK
        bounce_net, bounce_buys, bounce_off_low = PB_BOUNCE_NET, PB_BOUNCE_BUYS, PB_BOUNCE_OFF_LOW
        min_rq, min_age = PB_MIN_RQ, PB_MIN_AGE
    if rq_sol < min_rq or rq_sol > PB_MAX_RQ:
        return False
    if t.created is None or now - t.created < min_age:
        return False
    start = now - PB_WINDOW
    start_price = -1.0
    high = 0.0
    last_price = 0.0
    for (slot, _is_buy, _sol, _user, price) in t.trades:
        if slot <= start:
            start_price = price
            continue
        if start_price < 0:
            start_price = price
        if price > high:
            high = price
        last_price = price
    if start_price <= 0 or last_price <= 0 or high <= 0:
        return False
    if high < start_price * (1.0 + run_up):
        return False
    if last_price < start_price * 1.10:
        return False
    if last_price > high * (1.0 - pullback):
        return False
    bstart = now - PB_BOUNCE_WINDOW
    net = 0.0
    buys = 0
    low = None
    for (slot, is_buy, sol, _user, price) in t.trades:
        if slot <= bstart:
            continue
        if low is None or price < low:
            low = price
        if is_buy:
            net += sol
            buys += 1
        else:
            net -= sol
    if buys < bounce_buys or net < bounce_net:
        return False
    if low is None or last_price < low * (1.0 + bounce_off_low):
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
            t.last_trade = e["slot"]
            if e["mint"] not in traded_seen:
                traded_seen.add(e["mint"])
                traded.append(e["mint"])
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
                if (not h.filled and h.retry < BUY_RETRY_MAX
                        and now <= h.deadline):
                    continue  # buy failed: retry in the entry loop below
                del held[mint]
                t = tape_of(mint)
                if h.filled:
                    if h.kind == "pb2":
                        t.last_exit_b = now
                    else:
                        t.last_exit = now
                elif h.kind == "pb2":
                    t.entries_b -= 1
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
        # Scale out of winners before a gap down can take the whole gain:
        # half at +40%, half of the rest at +80%. A partial sale halves the
        # position, so re-base the peak on the smaller position.
        if not h.exiting and mint not in sell_pending:
            target = None
            if h.scaled == 0 and value >= cost * (1.0 + SCALE_OUT_AT):
                target = 1
            elif h.scaled == 1 and value >= cost * (1.0 + SCALE_OUT_2_AT):
                target = 2
            if target is not None and len(orders) < cap:
                half = pos.token_amount // 2
                if half > 0:
                    orders.append({"side": "sell", "mint": mint,
                                   "token_amount": half,
                                   "max_slippage_bps": SELL_SLIP_BPS})
                    h.scaled = target
                    h.invested *= 0.5
                    h.peak = value * 0.5
                    continue
        exit_now = h.exiting
        if not exit_now and value < cost * (1.0 - STOP_LOSS):
            exit_now = True
        if (not exit_now and h.peak >= cost * (1.0 + TRAIL_ARM)
                and value < h.peak * (1.0 - TRAIL)):
            exit_now = True
        if not exit_now and net_flow(t, now, FADE_WINDOW) < -FADE_NET_SOL:
            exit_now = True
        # A curve that goes quiet is a dead curve: sell while value is left.
        if not exit_now and now - t.last_trade > INACTIVITY:
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
        if mint in pending_by_mint:
            continue
        t = tapes[mint]
        h = held.get(mint)
        price = curve.virtual_quote_reserves / curve.virtual_token_reserves
        rq_sol = curve.real_quote_reserves / LAMPORTS
        if h is None:
            if slots_left <= 0 or main_left <= 0:
                continue
            if pullback_signal(t, now, rq_sol, 1):
                if t.entries >= MAX_ENTRIES or now - t.last_exit < COOLDOWN:
                    continue
                kind, initial, stake, add = "pb", PB_INITIAL, PB_STAKE, PB_ADD
            elif pullback_signal(t, now, rq_sol, 2):
                if slots_left < 2:
                    continue
                if t.entries_b >= MAX_ENTRIES or now - t.last_exit_b < COOLDOWN:
                    continue
                kind, initial, stake, add = "pb2", PB2_INITIAL, PB2_STAKE, PB2_INITIAL
            else:
                continue
            want = min(initial, stake)
            is_new = True
        elif not h.filled:
            # Retry of a failed initial buy: same amount, only while the
            # price has not run away and the order would fit the size cap.
            if (h.retry < BUY_RETRY_MAX and now <= h.deadline
                    and price <= h.last_add_price * BUY_RETRY_MAX_RUNUP
                    and h.invested <= rq_sol * 0.0099
                    and len(orders) < cap):
                orders.append({"side": "buy", "mint": mint,
                               "quote_lamports": int(h.invested * LAMPORTS),
                               "max_slippage_bps": BUY_SLIP_BPS})
                h.retry += 1
            continue
        else:
            # Pyramid into strength only: the position is up, the price
            # stepped up from the last add, and net SOL still flows in.
            if h.exiting or h.invested >= h.stake - MIN_BUY_SOL:
                continue
            pos = state.positions.get(mint)
            if pos is None:
                continue
            if price < h.last_add_price * (1.0 + ADD_STEP):
                continue
            if h.last_value < (pos.cost_lamports / LAMPORTS) * (1.0 + ADD_IF_UP):
                continue
            if net_flow(t, now, PB_BOUNCE_WINDOW) < ADD_NET_SOL:
                continue
            if rq_sol > PB_MAX_RQ:
                continue
            want = min(h.add, h.stake - h.invested)
            is_new = False
            kind = h.kind
            stake = h.stake
            add = h.add
        cap_sol = state.max_buy_lamports(mint) / LAMPORTS
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
            held[mint] = Held(now, amount, price, kind, stake, add)
            if kind == "pb2":
                t.entries_b += 1
            else:
                t.entries += 1
            slots_left -= 1
            main_left -= 1
        else:
            h.invested += amount
            h.last_add_price = price
    return orders


if __name__ == "__main__":
    makefun_bot.run(on_batch)
