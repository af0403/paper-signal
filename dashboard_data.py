"""
Builds docs/data.json for the GitHub Pages dashboard.

Keeps a rolling record of every check (30 days) plus the protocol's own
hourly series, so the charts are full from the first run.
"""
from __future__ import annotations

import json
import os
import time

DATA_FILE = os.environ.get("DASHBOARD_DATA", "docs/data.json")
KEEP_SECS = 30 * 24 * 3600
PROTOCOL_HOURS = 7 * 24


def _load() -> dict:
    try:
        with open(DATA_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"checks": []}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(DATA_FILE) or ".", exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, separators=(",", ":"))


def _protocol_series(history: dict) -> dict:
    """Hourly deltas from the API's cumulative columns (last 7 days)."""
    c = history["columns"]
    n = len(c["stakingRewards"])
    start = history.get("startMs", 0)
    step = history.get("intervalMs", 3600000)
    lo = max(1, n - PROTOCOL_HOURS)
    out = {"t": [], "rewards": [], "net_loss": [], "minted": [], "supply": [], "volume": []}
    for i in range(lo, n):
        out["t"].append(start + i * step)
        out["rewards"].append(round(c["stakingRewards"][i] - c["stakingRewards"][i - 1], 2))
        out["net_loss"].append(round(-(c["traderPnl"][i] - c["traderPnl"][i - 1]), 2))
        out["minted"].append(round(c["paperSupply"][i] - c["paperSupply"][i - 1]))
        out["supply"].append(round(c["paperSupply"][i]))
        if "volume" in c:
            out["volume"].append(round(c["volume"][i] - c["volume"][i - 1]))
    return out


def record_success(a: dict, history: dict) -> None:
    d = _load()
    now = int(time.time() * 1000)
    p, s = a["protocol"], a["scenarios"]
    drain = a.get("drain_to_reset") or {}
    pb = lambda r: r["payback_days"] if r["payback_days"] else None
    d["checks"].append({
        "t": now, "decision": a["decision"], "mint_rate": p["mint_rate"],
        "rewards_24h": p["rewards_last_24h_usd"], "rewards_6h": p["rewards_last_6h_usd"],
        "payback_base": pb(s["last_24h_with_decay"]), "payback_flat": pb(s["last_24h_flat (optimistic)"]),
        "lp": p["lp_usd"], "queue": p["queue_usd"], "move_pct": drain.get("move_pct_both_assets"),
    })
    d["checks"] = [x for x in d["checks"] if now - x["t"] <= KEEP_SECS]
    d["latest"] = a
    d["updated_ms"] = now
    d["status"] = {"ok": True, "error": None, "since_ms": now}
    try:
        d["protocol"] = _protocol_series(history)
    except Exception as e:                       # charts degrade, page still works
        d["protocol_error"] = str(e)
    _save(d)


def record_telegram(status: str) -> None:
    """Public diagnostic: never include the token or chat id here."""
    d = _load()
    d["telegram"] = {"status": status[:200], "at_ms": int(time.time() * 1000)}
    _save(d)


def record_failure(err: str) -> None:
    d = _load()
    now = int(time.time() * 1000)
    prev = d.get("status") or {}
    d["status"] = {"ok": False, "error": err[:300],
                   "since_ms": prev.get("since_ms") if prev.get("ok") is False else now}
    d["attempted_ms"] = now
    _save(d)
