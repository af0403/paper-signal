#!/usr/bin/env python3
"""
PAPER farm-timing signal for Papertrade (papertrade.xyz).

Read-only. Pulls Papertrade's public protocol stats, works out what a
deliberate loss would earn back as staking rewards, and prints one of:

  FARM   - modelled payback is inside your target window
  WATCH  - not worth it now, but a mint-rate reset is close
  WAIT   - not worth it

It never connects a wallet, signs anything or places a trade.

Contract maths (verified against mainnet by the open-source papertrade-sdk):
  mint rate = 100 PAPER per $1 while trackedLp < $2M (cliff), otherwise
              100 * (S / (S + H))^2 with S = $120M, H = tailProgress
  loss basis = loss * 0.98 on a losing close, the full margin on a liquidation
  stakers get the LP overflow above the $5M cap (in practice ~90% of traders'
  net losses) plus 1% of realised PnL while the payout queue is empty

Usage:
  python3 paper_farm_signal.py                     # $1,000 loss, 30-day target
  python3 paper_farm_signal.py --loss 500 --target-days 45
  python3 paper_farm_signal.py --decay 0.05        # force 5%/day reward decline
  python3 paper_farm_signal.py --json              # machine-readable output
  python3 paper_farm_signal.py --offline DIR       # use saved summary.json,
                                                   # history_1h.json, house.json
Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

API = "https://exchange.papertrade.xyz"
WAD = 1e18
S_DECAY = 120e6          # tail decay scale, $
FLAT_RATE = 100.0        # PAPER per $1 below the cliff
CLIFF = 2e6              # trackedLp below this mints at the flat rate
CAP = 5e6                # LP balance above this overflows to stakers
HORIZON_DAYS = 365


# --------------------------------------------------------------------- data
def _get(path: str) -> dict:
    req = urllib.request.Request(API + path, headers={"User-Agent": "paper-farm-signal/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def load(offline: str | None) -> tuple[dict, dict, dict | None]:
    if offline:
        rd = lambda n: json.load(open(os.path.join(offline, n)))
        house = rd("house.json") if os.path.exists(os.path.join(offline, "house.json")) else None
        return rd("summary.json"), rd("history_1h.json"), house
    summary = _get("/query/protocol/summary")
    history = _get("/query/protocol/history?interval=1h")
    try:
        house = _get("/state/house/current")
    except Exception:
        house = None
    return summary, history, house


# -------------------------------------------------------------------- maths
def mint_rate(tracked_lp: float, tail_progress: float) -> float:
    if tracked_lp < CLIFF:
        return FLAT_RATE
    return FLAT_RATE * (S_DECAY / (S_DECAY + max(0.0, tail_progress))) ** 2


def window(cols: dict, hours: int, end: int | None = None) -> dict:
    """Deltas over the last `hours` hourly points (cumulative columns)."""
    n = len(cols["stakingRewards"])
    b = (n - 1) if end is None else end
    a = max(0, b - hours)
    span_h = b - a
    d = lambda k: cols[k][b] - cols[k][a]
    return {
        "hours": span_h,
        "rewards": d("stakingRewards"),
        "net_loss": -d("traderPnl"),
        "minted": d("paperSupply"),
        "staked": cols["paperStaked"][b],
    }


def tail_supply(h: float) -> float:
    """PAPER minted along the decaying tail once H = tailProgress (integral of
    the rate curve). Bounded by 100 * S = 12B, so dilution can't run away."""
    return FLAT_RATE * S_DECAY * h / (S_DECAY + h)


def simulate(loss: float, rate: float, staked0: float, rewards_per_day: float,
             basis_per_day: float, tail0: float, decay: float,
             start_delay_days: float = 0.0) -> dict:
    """Day-by-day: your PAPER earns its share of the reward stream while
    everyone's new losses keep diluting it. New loss basis advances H along the
    contract's curve, so later mints are smaller. Rewards and loss basis both
    shrink by `decay`/day."""
    mine = loss * rate
    staked = staked0 + mine
    tail = tail0
    earned, payback = 0.0, None
    curve = []
    for day in range(1, HORIZON_DAYS + 1):
        f = (1 - decay) ** (day - 1)
        new_tail = tail + basis_per_day * f
        staked += tail_supply(new_tail) - tail_supply(tail)
        tail = new_tail
        if day > start_delay_days:
            earned += rewards_per_day * f * mine / staked
        curve.append(earned)
        if payback is None and earned >= loss:
            payback = day
    return {"paper": mine, "payback_days": payback, "earned_30d": curve[29],
            "earned_90d": curve[89], "earned_365d": curve[-1]}


def drain_move(house: dict | None, tracked_lp: float) -> dict | None:
    """How far BTC/ETH must move in the crowd's favour to push the LP under the
    cliff (which resets the mint rate to 100). Ignores the win haircut, so the
    real move needed is larger: treat this as a lower bound."""
    if not house or "head" not in house:
        return None
    _, lp, _, q, bl, bs, el, es = house["head"][:8]
    net_btc, net_eth = bl - bs, el - es            # + = traders net long
    gap = max(0.0, tracked_lp - CLIFF)
    out = {"net_long_btc_usd": net_btc, "net_long_eth_usd": net_eth, "gap_to_cliff_usd": gap}
    expo = abs(net_btc) + abs(net_eth)
    if expo > 0:
        out["move_pct_both_assets"] = 100 * gap / expo
    if abs(net_btc) > 0:
        out["btc_move_pct"] = 100 * gap / abs(net_btc) * (1 if net_btc > 0 else -1)
    if abs(net_eth) > 0:
        out["eth_move_pct"] = 100 * gap / abs(net_eth) * (1 if net_eth > 0 else -1)
    return out


# ------------------------------------------------------------------ signal
def analyse(summary: dict, history: dict, house: dict | None, loss: float,
            target_days: int, decay_override: float | None, watch_move_pct: float) -> dict:
    p, bal = summary["paper"], summary["balances"]
    tracked_lp = float(p["trackedLp"]) / WAD
    tail = float(p["tailProgress"]) / WAD
    queue = float(bal["queue"]) / WAD
    lp = float(bal["lp"]) / WAD
    rate = mint_rate(tracked_lp, tail)
    reset_active = tracked_lp < CLIFF

    cols = history["columns"]
    w24, w6 = window(cols, 24), window(cols, 6)
    prev24 = window(cols, 24, end=len(cols["stakingRewards"]) - 1 - 24)

    per_day = lambda w, k: w[k] * 24 / w["hours"] if w["hours"] else 0.0
    r24, r6, rprev = per_day(w24, "rewards"), per_day(w6, "rewards"), per_day(prev24, "rewards")
    mint24 = per_day(w24, "minted")
    staked = w24["staked"]

    if decay_override is not None:
        decay = decay_override
    elif rprev > 0:
        decay = min(0.9, max(0.0, 1 - r24 / rprev))   # never extrapolate growth
    else:
        decay = 0.0

    # Rewards to stakers pause while the LP is under the cap or a queue exists;
    # estimate how long until they resume, from the recent net-loss pace.
    net_loss_day = max(per_day(w24, "net_loss"), 1.0)
    refill_days = max(0.0, (CAP - lp) + queue) / net_loss_day if (lp < CAP or queue > 0) else 0.0

    # Gross loss basis the whole market fed in over the last day (wins don't
    # cancel it: mints come from gross losses, rewards from net losses).
    basis24 = mint24 / rate if rate else 0.0
    scenarios = {
        "recent_6h_pace": simulate(loss, rate, staked, r6, basis24, tail, decay, refill_days),
        "last_24h_with_decay": simulate(loss, rate, staked, r24, basis24, tail, decay, refill_days),
        "last_24h_flat (optimistic)": simulate(loss, rate, staked, r24, basis24, tail, 0.0, refill_days),
    }
    base = scenarios["last_24h_with_decay"]

    market_cost = (w24["net_loss"] / w24["minted"]) if w24["minted"] > 0 else None
    drain = drain_move(house, tracked_lp)

    if base["payback_days"] and base["payback_days"] <= target_days:
        decision = "FARM"
        why = f"base-case payback {base['payback_days']}d is inside your {target_days}d target"
    elif drain and drain.get("move_pct_both_assets") is not None and drain["move_pct_both_assets"] <= watch_move_pct:
        decision = "WATCH"
        why = (f"not worth it now, but a ~{drain['move_pct_both_assets']:.2f}% move in the crowd's "
               f"favour would drain the LP under $2M and reset the mint rate to 100")
    else:
        decision = "WAIT"
        pb = base["payback_days"]
        why = f"base-case payback {'>' + str(HORIZON_DAYS) if pb is None else pb}d vs {target_days}d target"

    return {
        "decision": decision, "why": why,
        "inputs": {"loss_usd": loss, "target_days": target_days, "decay_per_day": round(decay, 4)},
        "protocol": {
            "as_of_ms": summary["source"]["at"], "mint_rate": round(rate, 3),
            "your_cost_per_paper": round(1 / rate, 5), "reset_active": reset_active,
            "tracked_lp_usd": round(tracked_lp), "lp_usd": round(lp), "queue_usd": round(queue),
            "tail_progress_usd": round(tail), "paper_staked": round(staked),
            "rewards_last_24h_usd": round(w24["rewards"]), "rewards_prev_24h_usd": round(prev24["rewards"]),
            "rewards_last_6h_usd": round(w6["rewards"]),
            "yield_per_paper_per_day": r24 / staked if staked else 0.0,
            "market_net_cost_per_paper": round(market_cost, 5) if market_cost else None,
            "rewards_resume_in_days": round(refill_days, 2),
        },
        "drain_to_reset": drain,
        "scenarios": scenarios,
    }


def report(a: dict) -> str:
    p, s = a["protocol"], a["scenarios"]
    lines = [
        f"\n  SIGNAL: {a['decision']}  -  {a['why']}\n",
        f"  Mint rate        {p['mint_rate']:.2f} PAPER per $1 lost"
        + ("   (RESET ACTIVE: LP under $2M)" if p["reset_active"] else ""),
        f"  Your cost        ${p['your_cost_per_paper']:.4f} per PAPER (straight loss)",
    ]
    if p["market_net_cost_per_paper"]:
        x = p["your_cost_per_paper"] / p["market_net_cost_per_paper"]
        lines.append(f"  Market's cost    ${p['market_net_cost_per_paper']:.4f} per PAPER net of wins "
                     f"(you'd pay {x:.1f}x what the crowd pays)")
    lines += [
        f"  Staker rewards   ${p['rewards_last_24h_usd']:,} last 24h vs ${p['rewards_prev_24h_usd']:,} prior 24h "
        f"(${p['rewards_last_6h_usd']:,} in last 6h)",
        f"  Modelled decay   {a['inputs']['decay_per_day']*100:.1f}% per day",
        f"  LP / queue       ${p['lp_usd']:,} / ${p['queue_usd']:,}"
        + (f"   rewards resume in ~{p['rewards_resume_in_days']}d" if p["rewards_resume_in_days"] else ""),
    ]
    d = a["drain_to_reset"]
    if d and d.get("move_pct_both_assets") is not None:
        lines.append(f"  Reset distance   traders net long BTC ${d['net_long_btc_usd']/1e6:,.1f}M, "
                     f"ETH ${d['net_long_eth_usd']/1e6:,.1f}M -> ~{d['move_pct_both_assets']:.2f}% "
                     f"favourable move drains LP to $2M (lower bound)")
    lines.append(f"\n  ${a['inputs']['loss_usd']:,.0f} loss -> {s['last_24h_with_decay']['paper']:,.0f} PAPER")
    lines.append(f"  {'scenario':<30}{'payback':>10}{'30d':>10}{'90d':>10}{'365d':>10}")
    for name, r in s.items():
        pb = f"{r['payback_days']}d" if r["payback_days"] else "never"
        lines.append(f"  {name:<30}{pb:>10}{r['earned_30d']:>10,.0f}{r['earned_90d']:>10,.0f}{r['earned_365d']:>10,.0f}")
    lines.append("\n  Model only. Ignores smart-contract, oracle and governance risk, and the chance"
                 "\n  your 'losing' trade wins instead (no PAPER, haircut + 2% fee on the gain).\n")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loss", type=float, default=1000.0, help="loss you'd take, $ (default 1000)")
    ap.add_argument("--target-days", type=int, default=30, help="max acceptable payback (default 30)")
    ap.add_argument("--decay", type=float, default=None, help="force daily reward decay, e.g. 0.05")
    ap.add_argument("--watch-move", type=float, default=1.0,
                    help="flag WATCH when a reset is within this %% move (default 1.0)")
    ap.add_argument("--offline", metavar="DIR", help="read saved JSON instead of the live API")
    ap.add_argument("--json", action="store_true", help="print JSON instead of the report")
    args = ap.parse_args()
    try:
        summary, history, house = load(args.offline)
    except Exception as e:
        print(f"Could not load Papertrade data: {e}", file=sys.stderr)
        return 2
    a = analyse(summary, history, house, args.loss, args.target_days, args.decay, args.watch_move)
    print(json.dumps(a, indent=2) if args.json else report(a))
    return {"FARM": 0, "WATCH": 10, "WAIT": 20}[a["decision"]]


if __name__ == "__main__":
    sys.exit(main())
