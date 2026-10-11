"""
Crypto Pump Alert Bot  (alerts only - it NEVER trades)

Signals (from your plan):
  Price   : +2% in last 5 min
  Volume  : ~3x normal
  X       : posts spike in last 10 min      (optional, needs X API token)
  News    : listing/announcement chatter    (optional, needs X API token)
  Final   : alert only when several signals agree
Filter    : skip coins that already pumped a lot (24h change too high)

SETUP
  pip install requests python-dotenv
  Create a .env file next to this script:
      TELEGRAM_BOT_TOKEN=123456:ABC...
      TELEGRAM_CHAT_ID=123456789
      COINGECKO_API_KEY=CG-xxxx          # free Demo key
      X_BEARER_TOKEN=                    # optional, leave empty to skip X/news
  Run:  python pump_alert_bot.py
"""
import csv
import os
import time
from collections import defaultdict, deque

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------- Settings (tune these after backtesting) ----------
POLL_SECONDS = 60            # CoinGecko Demo allows ~30 calls/min; 60s is safe
TOP_N = 100                  # scan top 100 coins
PRICE_WINDOW_MIN = 5
PRICE_JUMP_PCT = 2.0
VOLUME_MULT = 3.0
X_WINDOW_MIN = 10
X_SPIKE_MULT = 3.0
X_MIN_POSTS = 15             # ignore tiny numbers
MAX_24H_CHANGE_PCT = 30.0    # already pumped too much -> skip
MIN_SIGNALS = 2              # price+volume is mandatory; X/news can add
COOLDOWN_MIN = 60            # don't re-alert same coin for 1 hour
LOG_FILE = "alerts_log.csv"  # for paper-testing: did alert come BEFORE the pump?

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")
CG_KEY = os.getenv("COINGECKO_API_KEY", "")
X_TOKEN = os.getenv("X_BEARER_TOKEN", "")

history = defaultdict(lambda: deque(maxlen=120))  # coin_id -> (ts, price, vol24h)
last_alert = {}


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
        params={
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": TOP_N,
            "page": 1,
            "price_change_percentage": "24h",
        },
        headers={"x-cg-demo-api-key": CG_KEY} if CG_KEY else {},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def price_signal(coin_id, now):
    """Return (pct change over window, volume ratio) or None if not enough data."""
    h = history[coin_id]
    target = now - PRICE_WINDOW_MIN * 60
    old = next((x for x in h if x[0] >= target - 30), None)
    if not old or now - old[0] < PRICE_WINDOW_MIN * 60 * 0.6:
        return None
    ts0, p0, v0 = old
    ts1, p1, v1 = h[-1]
    pct = (p1 - p0) / p0 * 100 if p0 else 0
    # Approx: 24h volume added during the window vs average per-window volume
    window_vol = max(v1 - v0, 0)
    normal = v1 / (24 * 60 / PRICE_WINDOW_MIN)
    vol_ratio = window_vol / normal if normal else 0
    return pct, vol_ratio


def x_counts(query, minutes):
    """Posts per minute-bucket over last hour -> (recent count, avg per same-size window)."""
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
    avg = sum(older) / max(len(older), 1) * minutes
    return recent, avg


def x_signals(symbol, name):
    """Returns (x_spike: bool, news: bool, details: str). Skipped if no X token."""
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


def log_alert(row):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "coin", "price", "5m_pct", "vol_ratio", "24h_pct", "signals"])
        w.writerow(row)


def scan():
    now = time.time()
    coins = fetch_markets()
    for c in coins:
        cid, price, vol = c["id"], c.get("current_price"), c.get("total_volume")
        if price is None or vol is None:
            continue
        history[cid].append((now, price, vol))

        sig = price_signal(cid, now)
        if not sig:
            continue
        pct, vol_ratio = sig
        if pct < PRICE_JUMP_PCT or vol_ratio < VOLUME_MULT:
            continue  # price AND volume are required

        ch24 = c.get("price_change_percentage_24h") or 0
        if ch24 > MAX_24H_CHANGE_PCT:
            continue  # already pumped too much
        if now - last_alert.get(cid, 0) < COOLDOWN_MIN * 60:
            continue

        spike, news, detail = x_signals(c["symbol"], c["name"])
        signals = ["price", "volume"] + (["x_spike"] if spike else []) + (["news"] if news else [])
        if len(signals) < MIN_SIGNALS:
            continue
        # If X is configured, require at least one social/news signal to cut fake pumps
        if X_TOKEN and not (spike or news):
            continue

        last_alert[cid] = now
        msg = (
            f"🚨 PUMP ALERT: {c['name']} ({c['symbol'].upper()})\n"
            f"Price: ${price:,.6g}  ({pct:+.2f}% in {PRICE_WINDOW_MIN}m)\n"
            f"Volume: ~{vol_ratio:.1f}x normal\n"
            f"24h change: {ch24:+.1f}%\n"
            f"Signals: {', '.join(signals)}\n"
            f"{detail}\n"
            f"https://www.coingecko.com/en/coins/{cid}\n\n"
            "⚠️ Alert only, not financial advice. Verify the news yourself."
        )
        send_telegram(msg)
        log_alert([time.strftime("%Y-%m-%d %H:%M:%S"), cid, price,
                   round(pct, 2), round(vol_ratio, 2), round(ch24, 1), "+".join(signals)])


def main():
    send_telegram("✅ Pump alert bot started.")
    while True:
        try:
            scan()
        except requests.RequestException as e:
            print("Network/API error:", e)
        except Exception as e:  # keep running no matter what
            print("Unexpected error:", e)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
