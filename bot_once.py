import json
import os
import time

import requests

TOP_N = 250
MIN_VOLUME_USD = 500_000
MIN_WINDOW_MIN = 4
MAX_WINDOW_MIN = 20
PRICE_JUMP_PCT = 2.0
VOLUME_MULT = 3.0
X_WINDOW_MIN = 10
X_SPIKE_MULT = 3.0
X_MIN_POSTS = 15
MAX_24H_CHANGE_PCT = 30.0
COOLDOWN_MIN = 60
KEEP_MIN = 30
STATE_FILE = "state.json"

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
CG_KEY = os.getenv("COINGECKO_API_KEY", "")
X_TOKEN = os.getenv("X_BEARER_TOKEN", "")


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
            s.setdefault("history", {})
            s.setdefault("last_alert", {})
            return s
    except (OSError, ValueError):
        return {"history": {}, "last_alert": {}}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)


def send_telegram(text):
    if not TG_TOKEN or not TG_CHAT:
        print("[no telegram configured]\n", text)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
    except requests.RequestException as e:
        print("Telegram error:", e)


def fetch_markets():
    r = requests.get(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={"vs_currency": "usd", "order": "market_cap_desc",
                "per_page": TOP_N, "page": 1, "price_change_percentage": "24h"},
        headers={"x-cg-demo-api-key": CG_KEY} if CG_KEY else {},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def price_signal(snaps, now):
    ts1, p1, v1 = snaps[-1]
    old = None
    for ts, p, v in snaps[:-1]:
        age = (now - ts) / 60
        if MIN_WINDOW_MIN <= age <= MAX_WINDOW_MIN:
            old = (ts, p, v)
            break
    if not old:
        return None
    ts0, p0, v0 = old
    pct = (p1 - p0) / p0 * 100 if p0 else 0
    elapsed = max(ts1 - ts0, 1)
    window_vol = max(v1 - v0, 0)
    normal = v1 / (86400 / elapsed)
    return pct, (window_vol / normal if normal else 0)


def x_counts(query, minutes):
    r = requests.get(
        "https://api.x.com/2/tweets/counts/recent",
        params={"query": query, "granularity": "minute"},
        headers={"Authorization": f"Bearer {X_TOKEN}"},
        timeout=20,
    )
    r.raise_for_status()
    buckets = [b["tweet_count"] for b in r.json().get("data", [])]
    if len(buckets) < minutes * 2:
        return 0, 0
    recent = sum(buckets[-minutes:])
    older = buckets[-60:-minutes] or [0]
    return recent, sum(older) / max(len(older), 1) * minutes


def x_signals(symbol, name):
    if not X_TOKEN:
        return False, False, "X/news check skipped (no token)"
    try:
        q = f'("${symbol.upper()}" OR "{name}") -is:retweet lang:en'
        recent, avg = x_counts(q, X_WINDOW_MIN)
        spike = recent >= X_MIN_POSTS and recent >= X_SPIKE_MULT * max(avg, 1)
        nq = (f'("${symbol.upper()}" OR "{name}") '
              '(listing OR listed OR partnership OR mainnet OR announces OR "official") '
              '-is:retweet lang:en')
        n_recent, n_avg = x_counts(nq, X_WINDOW_MIN)
        news = n_recent >= 5 and n_recent >= 2 * max(n_avg, 1)
        return spike, news, f"X posts: {recent} in {X_WINDOW_MIN}m (normal ~{avg:.0f}); news-like: {n_recent}"
    except requests.RequestException as e:
        return False, False, f"X check failed: {e}"


def main():
    now = time.time()
    state = load_state()
    hist, last_alert = state["history"], state["last_alert"]
    coins = fetch_markets()
    sent = 0

    for c in coins:
        cid, price, vol = c["id"], c.get("current_price"), c.get("total_volume")
        if price is None or vol is None or vol < MIN_VOLUME_USD:
            continue
        snaps = [s for s in hist.get(cid, []) if now - s[0] <= KEEP_MIN * 60]
        snaps.append([now, price, vol])
        hist[cid] = snaps

        sig = price_signal(snaps, now)
        if not sig:
            continue
        pct, vol_ratio = sig
        if pct < PRICE_JUMP_PCT or vol_ratio < VOLUME_MULT:
            continue
        ch24 = c.get("price_change_percentage_24h") or 0
        if ch24 > MAX_24H_CHANGE_PCT:
            continue
        if now - last_alert.get(cid, 0) < COOLDOWN_MIN * 60:
            continue

        spike, news, detail = x_signals(c["symbol"], c["name"])
        if X_TOKEN and not (spike or news):
            continue
        signals = ["price", "volume"] + (["x_spike"] if spike else []) + (["news"] if news else [])

        last_alert[cid] = now
        sent += 1
        send_telegram(
            f"🚨 PUMP ALERT: {c['name']} ({c['symbol'].upper()})\n"
            f"Price: ${price:,.6g}  ({pct:+.2f}% recently)\n"
            f"Volume: ~{vol_ratio:.1f}x normal\n"
            f"24h change: {ch24:+.1f}%\n"
            f"Signals: {', '.join(signals)}\n"
            f"{detail}\n"
            f"https://www.coingecko.com/en/coins/{cid}\n\n"
            "⚠️ Alert only, not financial advice. Verify the news yourself."
        )

    live = {c["id"] for c in coins}
    state["history"] = {k: v for k, v in hist.items() if k in live}
    save_state(state)
    print(f"Scanned {len(coins)} coins, sent {sent} alert(s).")


if __name__ == "__main__":
    main()
