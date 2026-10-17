#!/usr/bin/env python3
"""
Telegram notifier for the PAPER farm-timing signal (runs every 30 minutes).

Runs paper_farm_signal.analyse(), compares the result with the last run and
messages you on Telegram only when something worth knowing happens:

  * first run            -> "connected" message with the current signal
  * signal changes       -> WAIT / WATCH / FARM alert (both directions)
  * still FARM           -> reminder every 6 hours
  * LP crosses $5M cap   -> drop below (overflow to stakers stops) / back to cap
  * LP crosses $2M       -> mint-rate reset to 100 PAPER/$1 / back on the curve
  * 09:00 UK each day    -> one-line "still alive" heartbeat
  * 3 failed fetches     -> "can't reach Papertrade" warning, then "back online"

Read-only: no wallet, no trading.

Environment:
  TELEGRAM_BOT_TOKEN   required (from @BotFather; keep it in a GitHub secret)
  TELEGRAM_CHAT_ID     optional; auto-detected from the last message you sent
                       the bot, then remembered in the state file
  STATE_FILE           default: state.json
  LOSS_USD             default: 1000
  TARGET_DAYS          default: 30
  OFFLINE_DIR          testing only: read saved JSON instead of the live API
  TELEGRAM_API         testing only: override https://api.telegram.org
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import dashboard_data
import paper_farm_signal as sig

TG_API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
UK = ZoneInfo("Europe/London")
HEARTBEAT_HOUR = 9
FARM_REMINDER_SECS = 6 * 3600
FAIL_ALERT_AFTER = 3
EMOJI = {"FARM": "🟢", "WATCH": "🟡", "WAIT": "🔴"}

# LP zones, with a buffer so tiny wobbles around the $5M cap don't spam you:
# "full" once LP >= $4.95M, "below" once LP < $4.75M, "reset" under $2M.
LP_FULL_AT = 4.95e6
LP_BELOW_AT = 4.75e6
LP_CLIFF = 2e6


def lp_zone(lp: float, prev: str | None) -> str:
    if lp < LP_CLIFF:
        return "reset"
    if lp >= LP_FULL_AT:
        return "full"
    if lp < LP_BELOW_AT:
        return "below"
    return prev if prev in ("full", "below") else "full"


def lp_message(prev: str, zone: str, a: dict) -> str | None:
    p = a["protocol"]
    lp = f"${p['lp_usd']/1e6:.2f}M"
    if zone == "reset":
        curve = sig.mint_rate(LP_FULL_AT, p["tail_progress_usd"])   # rate if LP were above $2M
        return (f"🚨 LP fell below $2M ({lp}). Mint rate reset to 100 PAPER/$1, "
                f"about {100 / max(curve, 1e-9):.0f}x cheaper than the curve ({curve:.1f}). "
                f"Staker rewards are paused until the LP refills to $5M.")
    if prev == "reset":
        return (f"↗️ LP back above $2M ({lp}). Mint rate is back on the curve at "
                f"{p['mint_rate']:.1f} PAPER/$1.")
    if zone == "below":
        return (f"🔻 LP dropped below the $5M cap ({lp}). Traders are winning, so "
                f"overflow to stakers has stopped. The reset line is $2M.")
    if zone == "full":
        return f"🔺 LP back at the $5M cap ({lp}). Overflow to stakers has resumed."
    return None


# ---------------------------------------------------------------- telegram
def tg(token: str, method: str, params: dict | None = None) -> dict:
    url = f"{TG_API}/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode() if params else None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            desc = json.load(e).get("description", "")
        except Exception:
            desc = ""
        raise RuntimeError(f"Telegram {method} HTTP {e.code}: {desc}") from None


def find_chat_id(token: str) -> int | None:
    """The chat of the most recent private message sent to the bot."""
    res = tg(token, "getUpdates", {"limit": "100"})
    for upd in reversed(res.get("result", [])):
        chat = (upd.get("message") or upd.get("edited_message") or {}).get("chat")
        if chat and chat.get("type") == "private":
            return chat["id"]
    return None


def send(token: str, chat_id: int, text: str) -> None:
    tg(token, "sendMessage", {"chat_id": str(chat_id), "text": text,
                              "disable_web_page_preview": "true"})


# ------------------------------------------------------------------- state
def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------- messages
def payback_str(r: dict) -> str:
    return f"{r['payback_days']}d" if r["payback_days"] else "never (1yr)"


def summary(a: dict) -> str:
    p, s, d = a["protocol"], a["scenarios"], a["drain_to_reset"]
    base = s["base: weekly trend"]
    flat = s["last 24h held flat (optimistic)"]
    loss = a["inputs"]["loss_usd"]
    lines = [
        f"{EMOJI[a['decision']]} {a['decision']}: {a['why']}",
        "",
        f"Mint rate: {p['mint_rate']:.1f} PAPER/$1"
        + (" (RESET ACTIVE)" if p["reset_active"] else ""),
        f"Your cost: ${p['your_cost_per_paper']:.4f}/PAPER if liquidated, "
        f"${p['your_cost_per_paper_close']:.4f} on a losing close",
        f"Staker rewards: ${p['rewards_last_24h_usd']/1e6:.2f}M last 24h "
        f"(prev ${p['rewards_prev_24h_usd']/1e6:.2f}M)",
        f"${loss:,.0f} loss payback: {payback_str(base)} base, {payback_str(flat)} if flat",
    ]
    if p["rewards_resume_in_days"]:
        lines.append(f"Rewards paused, resume in ~{p['rewards_resume_in_days']}d")
    if d and d.get("move_pct_both_assets") is not None:
        lines.append(f"Reset distance: ~{d['move_pct_both_assets']:.2f}% move in the crowd's favour")
    return "\n".join(lines)


# -------------------------------------------------------------------- main
def _err(e: Exception, token: str) -> str:
    """Error text safe for the public dashboard: the token never appears."""
    s = f"{type(e).__name__}: {e}"
    return s.replace(token, "<token>") if token else s


def resolve_chat(token: str, state: dict) -> int | None:
    if not token:
        dashboard_data.record_telegram("no_token: the GitHub job can't see a bot token secret")
        return None
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or state.get("chat_id")
    if chat_id:
        state["chat_id"] = int(chat_id)
        return int(chat_id)
    try:
        me = tg(token, "getMe")
        bot = (me.get("result") or {}).get("username", "?")
        upd = tg(token, "getUpdates", {"limit": "100"})
        n = len(upd.get("result", []))
        chat_id = find_chat_id(token)
    except Exception as e:
        dashboard_data.record_telegram("token_error: " + _err(e, token))
        print("Telegram call failed: " + _err(e, token), file=sys.stderr)
        return None
    if chat_id:
        state["chat_id"] = int(chat_id)
        return int(chat_id)
    dashboard_data.record_telegram(f"no_chat: token works for @{bot} but it has {n} unread "
                                   "messages; send the bot a message in Telegram")
    print("No Telegram chat found yet. Open your bot in Telegram and send it any "
          "message; the next run will pick it up.", file=sys.stderr)
    return None


def run() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN not set: updating the dashboard only.", file=sys.stderr)
    state = load_state()
    now = time.time()
    uk_now = datetime.fromtimestamp(now, UK)

    # 1. fetch + analyse; the dashboard is updated whatever happens next
    try:
        summary_json, history, house = sig.load(os.environ.get("OFFLINE_DIR"))
        a = sig.analyse(summary_json, history, house,
                        float(os.environ.get("LOSS_USD", 1000)),
                        int(os.environ.get("TARGET_DAYS", 30)), None, 1.0)
        dashboard_data.record_success(a, history)
    except Exception as e:
        state["fail_count"] = state.get("fail_count", 0) + 1
        print(f"fetch/analyse failed ({state['fail_count']}): {e}", file=sys.stderr)
        dashboard_data.record_failure(str(e))
        chat_id = resolve_chat(token, state)
        if chat_id and state["fail_count"] == FAIL_ALERT_AFTER:
            send(token, chat_id, f"⚠️ Can't reach Papertrade's stats for {FAIL_ALERT_AFTER} "
                                 f"checks in a row.\nLast error: {str(e)[:200]}\n"
                                 "I'll keep trying and tell you when it's back.")
        save_state(state)
        return 0

    chat_id = resolve_chat(token, state)
    if not chat_id:
        save_state(state)
        print(f"{uk_now:%Y-%m-%d %H:%M} {a['decision']} | dashboard updated, no Telegram")
        return 0

    # 2. decide what (if anything) to say
    decision, last = a["decision"], state.get("last_decision")
    msgs = []
    if state.get("fail_count", 0) >= FAIL_ALERT_AFTER:
        msgs.append("✅ Papertrade stats reachable again.")
    state["fail_count"] = 0

    if last is None:
        msgs.append("👋 PAPER signal bot connected. I check every 30 minutes and only "
                    "message you when the signal changes, plus a 9am heartbeat.\n\n" + summary(a))
        state["last_alert_ts"] = now
    elif decision != last:
        msgs.append(f"Signal changed: {last} → {decision}\n\n" + summary(a))
        state["last_alert_ts"] = now
    elif decision == "FARM" and now - state.get("last_alert_ts", 0) >= FARM_REMINDER_SECS:
        msgs.append("Still FARM\n\n" + summary(a))
        state["last_alert_ts"] = now

    prev_zone = state.get("lp_zone")
    zone = lp_zone(a["protocol"]["lp_usd"], prev_zone)
    if prev_zone is not None and zone != prev_zone:
        m = lp_message(prev_zone, zone, a)
        if m:
            msgs.append(m)
    state["lp_zone"] = zone

    today = uk_now.date().isoformat()
    if uk_now.hour >= HEARTBEAT_HOUR and state.get("last_heartbeat") != today:
        if not msgs:
            p = a["protocol"]
            base = a["scenarios"]["base: weekly trend"]
            msgs.append(f"☀️ Daily check: {EMOJI[decision]} {decision}. Mint {p['mint_rate']:.1f}/$1, "
                        f"rewards ${p['rewards_last_24h_usd']/1e6:.2f}M/24h, "
                        f"payback {payback_str(base)}.")
        state["last_heartbeat"] = today

    try:
        for m in msgs:
            send(token, chat_id, m)
    except Exception as e:
        # Don't record the new state, so the same alert is retried next run.
        dashboard_data.record_telegram("send_error: " + _err(e, token))
        print("Telegram send failed: " + _err(e, token), file=sys.stderr)
        state.pop("chat_id", None) if "chat not found" in str(e).lower() else None
        save_state(state)
        return 0
    dashboard_data.record_telegram(f"ok: sent {len(msgs)} message(s)")

    state["last_decision"] = decision
    state["last_run"] = uk_now.isoformat(timespec="minutes")
    save_state(state)
    print(f"{uk_now:%Y-%m-%d %H:%M} {decision} | sent {len(msgs)} message(s)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
