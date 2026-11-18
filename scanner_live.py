"""
Live-Waechter (ohne Handel, ohne Hebel)

Liest watchlist.json und meldet pro Wert:
  FRUEHWARNUNG   Kurs naehert sich dem Ausloeser
  SIGNAL         Ausloeser erreicht, Einstieg noch in der Zone und im Zeitfenster
  ZU_SPAET       Ausloeser war da, aber Kurs liegt ueber der Einstiegsgrenze
  FENSTER_VORBEI Zeitfenster (window_minutes) ist abgelaufen
  STOPP          Kurs unter dem Stopp, Setup ungueltig

Start:
  python scanner_live.py          Dauerbetrieb
  python scanner_live.py once     einmal pruefen und beenden
  python scanner_live.py test     Test-Push senden

Benoetigt scanner.py im selben Ordner (fuer das Tagesbild), laeuft sonst auch ohne.
Datenquelle: Yahoo Finance, je nach Boerse bis ca. 15 Min. verzoegert.
Keine Anlageberatung.
"""
import json
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

try:
    import scanner
except ImportError:
    scanner = None

CONFIG = "watchlist.json"
BERLIN = ZoneInfo("Europe/Berlin")
DEFAULTS = {
    "poll_seconds": 60,
    "ntfy_topic": "",
    "fee_pct": 0.1,       # Gebuehr pro Kauf/Verkauf in % (bitte aus der Bitpanda-App pruefen)
    "spread_pct": 0.5,    # geschaetzter Spread in %
    "min_rr": 1.5,        # Mindest-Chance/Risiko
    "window_minutes": 10,
}
LABEL = {
    "WARTET": "wartet",
    "FRUEHWARNUNG": "FRÜHWARNUNG",
    "SIGNAL": "SIGNAL",
    "ZU_SPAET": "ZU SPÄT",
    "FENSTER_VORBEI": "FENSTER VORBEI",
    "STOPP": "UNTER STOPP",
}
NOTIFY_STATES = ("FRUEHWARNUNG", "SIGNAL", "ZU_SPAET", "FENSTER_VORBEI", "STOPP")


# ---------- Konfiguration ----------
def read_config(path=CONFIG):
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print(f"Datei {path} nicht gefunden. Sie muss im selben Ordner liegen.")
        return None
    except json.JSONDecodeError as e:
        print(f"Tippfehler in {path}: Zeile {e.lineno}, Spalte {e.colno} ({e.msg}).")
        print("Meist fehlt ein Komma, ein Anführungszeichen oder eine Klammer.")
        return None
    cfg["settings"] = {**DEFAULTS, **cfg.get("settings", {})}
    stocks = cfg.get("stocks", [])
    for i, s in enumerate(stocks, 1):
        missing = [k for k in ("ticker", "trigger", "stop", "target") if k not in s]
        if missing:
            print(f"Eintrag {i} in {path}: Feld fehlt: {', '.join(missing)}")
            return None
        if not (s["stop"] < s["trigger"] < s["target"]):
            print(f"Eintrag {i} ({s['ticker']}): es muss gelten stop < trigger < target.")
            return None
    cfg["stocks"] = stocks
    return cfg


# ---------- Rechnen ----------
def levels(stock, s):
    """Kosten in % als Bruch und hoechster sinnvoller Einstiegskurs."""
    c = (2 * s["fee_pct"] + s["spread_pct"]) / 100
    max_entry = (stock["target"] + s["min_rr"] * stock["stop"]) / ((1 + c) * (1 + s["min_rr"]))
    return c, max_entry


def rr_at(price, stock, c):
    risk = price - stock["stop"] + price * c
    reward = stock["target"] - price - price * c
    return reward / risk if risk > 0 else float("nan")


def evaluate(stock, price, recent_high, max_entry):
    trig, stop = stock["trigger"], stock["stop"]
    warn = stock.get("warn_pct", 1.0)
    if price <= stop:
        return "STOPP"
    if recent_high >= trig:
        return "SIGNAL" if price <= max_entry else "ZU_SPAET"
    if price >= trig * (1 - warn / 100):
        return "FRUEHWARNUNG"
    return "WARTET"


# ---------- Zeiten ----------
def parse_hm(text, default):
    try:
        h, m = str(text).split(":")
        return int(h), int(m)
    except Exception:
        return default


def market_status(stock, now):
    """(offen?, Minuten bis Handelsschluss) in Berliner Zeit."""
    t = stock["ticker"]
    d_open, d_close = ((9, 0), (17, 30)) if t.endswith(".DE") else ((15, 30), (22, 0))
    oh, om = parse_hm(stock.get("open", ""), d_open)
    ch, cm = parse_hm(stock.get("close", ""), d_close)
    opens = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    closes = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    if now.weekday() >= 5 or not (opens <= now < closes):
        return False, 0
    return True, int((closes - now).total_seconds() // 60)


# ---------- Daten ----------
def fetch(ticker):
    h = yf.Ticker(ticker).history(period="1d", interval="1m")
    if h.empty:
        return None
    h = h.dropna(subset=["Close"])
    if h.empty:
        return None
    last_ts = h.index[-1]
    age = (pd.Timestamp.now(tz=last_ts.tz) - last_ts).total_seconds() / 60
    return {
        "price": float(h["Close"].iloc[-1]),
        "recent_high": float(h["High"].tail(5).max()),
        "age": age,
    }


def day_context(tickers):
    """Tagesbild aus scanner.py: RSI, Volumen, 20-Tage-Ausbruch."""
    ctx = {}
    if scanner is None or not tickers:
        return ctx
    try:
        data = scanner.load(tickers)
    except Exception as e:
        print(f"Tagesbild nicht verfügbar: {e}")
        return ctx
    for t, df in data.items():
        last = scanner.add_signals(df).iloc[-1]
        ctx[t] = (f"RSI {last['rsi']:.0f}, Volumen {last['vol_x']:.1f}x, "
                  f"20-Tage-Ausbruch: {'ja' if last['long'] else 'nein'}")
    return ctx


# ---------- Meldungen ----------
def notify(settings, title, body, priority="default"):
    print(f"\n>>> {title}\n{body}\n")
    topic = str(settings.get("ntfy_topic", "")).strip()
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={
                "Title": title.encode("ascii", "ignore").decode(),
                "Priority": priority,
                "Tags": "chart_with_upwards_trend",
            },
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"(Push fehlgeschlagen: {e})")


def build(stock, state, price, c, max_entry, left, mins_left, age, ctx_line):
    name = stock.get("name", stock["ticker"])
    trig, stop, target = stock["trigger"], stock["stop"], stock["target"]
    lines = []
    prio = "high"
    if state == "FRUEHWARNUNG":
        gap = (trig / price - 1) * 100
        title = f"Fruehwarnung {name}"
        lines.append(f"{name}: {price:.4f}, noch {gap:.1f} % bis zum Auslöser {trig}.")
        lines.append("Bereit machen, noch nicht kaufen.")
    elif state == "SIGNAL":
        title = f"SIGNAL {name}"
        prio = "urgent"
        lines.append(f"AUSLÖSER erreicht: {price:.4f} (Auslöser {trig}).")
        lines.append(f"Einstiegszone bis {max_entry:.4f}. Darüber lohnt sich das Risiko nicht mehr.")
        lines.append(f"Break-even erst ab {price * (1 + c):.4f} (Kosten {c * 100:.2f} %).")
        lines.append(f"Stopp {stop}, Ziel {target}, Chance/Risiko {rr_at(price, stock, c):.1f}.")
        if left is not None:
            lines.append(f"Noch {left:.0f} Min. in deinem Zeitfenster.")
    elif state == "ZU_SPAET":
        title = f"Zu spaet {name}"
        lines.append(f"Auslöser war erreicht, aber {price:.4f} liegt über der Einstiegsgrenze {max_entry:.4f}.")
        lines.append("Nicht hinterherlaufen.")
    elif state == "FENSTER_VORBEI":
        title = f"Fenster vorbei {name}"
        lines.append(f"Dein Zeitfenster ist abgelaufen. Kurs {price:.4f}. Signal gilt nicht mehr.")
    else:
        title = f"Unter Stopp {name}"
        prio = "default"
        lines.append(f"{price:.4f} liegt unter dem Stopp {stop}. Das Setup ist ungültig.")
    if mins_left is not None and mins_left <= 60:
        lines.append(f"Handelsschluss in {mins_left} Min.")
    if age > 5:
        lines.append(f"ACHTUNG: Daten sind {age:.0f} Min. alt, der echte Kurs kann anders liegen.")
    if ctx_line:
        lines.append(f"Tagesbild: {ctx_line}")
    if stock.get("note"):
        lines.append(f"Notiz: {stock['note']}")
    return title, "\n".join(lines), prio


# ---------- Ablauf ----------
def setup_check(cfg):
    s = cfg["settings"]
    active = [x for x in cfg["stocks"] if x.get("armed", True)]
    if not active:
        print("Keine aktiven Werte. Setze in watchlist.json bei einem Wert \"armed\": true.")
        return
    print("=== Setup-Check ===")
    for stock in active:
        c, max_entry = levels(stock, s)
        p = stock["trigger"]
        print(f"{stock.get('name', stock['ticker'])} ({stock['ticker']})")
        print(f"  Auslöser {p} | Stopp {stock['stop']} | Ziel {stock['target']}")
        print(f"  Kosten {c * 100:.2f} % | Break-even bei Auslöser: {p * (1 + c):.4f}")
        print(f"  Höchster sinnvoller Einstieg: {max_entry:.4f} | Chance/Risiko am Auslöser: {rr_at(p, stock, c):.1f}")
        if max_entry <= p:
            print("  WARNUNG: Schon am Auslöser ist das Chance/Risiko-Verhältnis zu schlecht. "
                  "Ziel höher, Stopp enger oder Kosten prüfen.")
    print()


def cycle(cfg, st, ctx):
    s = cfg["settings"]
    now = datetime.now(BERLIN)
    for stock in cfg["stocks"]:
        if not stock.get("armed", True):
            continue
        t = stock["ticker"]
        name = stock.get("name", t)
        c, max_entry = levels(stock, s)
        is_open, mins_left = market_status(stock, now)
        if not is_open:
            print(f"{now:%H:%M} {name}: Börse geschlossen")
            continue
        try:
            q = fetch(t)
        except Exception as e:
            print(f"{now:%H:%M} {name}: Fehler beim Laden ({e})")
            continue
        if q is None:
            print(f"{now:%H:%M} {name}: keine Minutendaten (Ticker prüfen)")
            continue
        price = q["price"]
        state = evaluate(stock, price, q["recent_high"], max_entry)
        window = stock.get("window_minutes", s["window_minutes"])
        left = None
        if state == "SIGNAL":
            since = st["since"].setdefault(t, now)
            used = (now - since).total_seconds() / 60
            if used > window:
                state = "FENSTER_VORBEI"
            else:
                left = window - used
        else:
            st["since"].pop(t, None)
        age_txt = f" | Daten {q['age']:.0f} Min. alt" if q["age"] > 3 else ""
        left_txt = f" | noch {left:.0f} Min." if left is not None else ""
        print(f"{now:%H:%M} {name}: {price:.4f} | {LABEL[state]} | "
              f"{(price / stock['trigger'] - 1) * 100:+.1f} % zum Auslöser{left_txt}{age_txt}")
        if state != st["last"].get(t):
            st["last"][t] = state
            if state in NOTIFY_STATES:
                title, body, prio = build(stock, state, price, c, max_entry, left,
                                          mins_left, q["age"], ctx.get(t, ""))
                notify(s, title, body, prio)


def main():
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "run"
    cfg = read_config()
    if cfg is None:
        return
    if mode == "test":
        notify(cfg["settings"], "Test vom Waechter",
               "Wenn du das siehst, funktionieren die Meldungen.", "default")
        return
    setup_check(cfg)
    st = {"last": {}, "since": {}}
    ctx, ctx_time = {}, 0.0
    print("Läuft. Beenden mit Ctrl+C. Änderungen in watchlist.json gelten nach dem Speichern.\n")
    try:
        while True:
            new = read_config()
            if new:
                cfg = new
            if time.time() - ctx_time > 1800:
                tickers = [x["ticker"] for x in cfg["stocks"] if x.get("armed", True)]
                ctx, ctx_time = day_context(tickers), time.time()
            cycle(cfg, st, ctx)
            if mode == "once":
                break
            time.sleep(max(15, int(cfg["settings"]["poll_seconds"])))
    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    main()
