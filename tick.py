"""
tick.py - ein Durchlauf, gedacht fuer GitHub Actions (alle ca. 15 Minuten).

  python tick.py           -> Scanner-Signale und watchlist.json-Ausloeser pruefen,
                              neue Ereignisse in signals_log.csv schreiben, Push senden
  python tick.py report    -> signals_log.csv auswerten (was wurde aus den Signalen?)

Benoetigt scanner.py und scanner_live.py im selben Ordner.
Der Stand zwischen den Durchlaeufen steht in state.json.
Hinweis: Ein Tages-Signal kann bis Handelsschluss wieder verschwinden, protokolliert
wird das erste Mal, dass es auftrat. Keine Anlageberatung.
"""
import csv
import json
import os
import sys
import time
from datetime import datetime

import scanner
import scanner_live as live

SIGNAL_LOG = "signals_log.csv"
STATE_FILE = "state.json"
FIELDS = ["time", "ticker", "type", "bar", "price", "vol_x", "rsi", "detail"]


# ---------- Stand ----------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    for key in ("seen", "last", "since"):
        st.setdefault(key, {})
    return st


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)


def append_events(events):
    if not events:
        return
    new_file = not os.path.exists(SIGNAL_LOG)
    with open(SIGNAL_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(events)


def fetch_retry(ticker, tries=3):
    for _ in range(tries):
        try:
            q = live.fetch(ticker)
            if q:
                return q
        except Exception as e:
            print(f"{ticker}: Fehler beim Laden ({e})")
        time.sleep(4)
    return None


# ---------- Teil A: Scanner-Signale ----------
def scan_signals(tickers, st, now, settings):
    events = []
    try:
        data = scanner.load(tickers)
    except Exception as e:
        print(f"Scan fehlgeschlagen: {e}")
        return events
    for t, df in data.items():
        d = scanner.add_signals(df)
        last = d.iloc[-1]
        bar = d.index[-1].strftime("%Y-%m-%d")
        for kind, flag in (("LONG", bool(last["long"])), ("SCHWAECHE", bool(last["weak"]))):
            key = f"{t}|{kind}"
            if not flag or st["seen"].get(key) == bar:
                continue
            st["seen"][key] = bar
            events.append({
                "time": now.strftime("%Y-%m-%d %H:%M"), "ticker": t, "type": kind, "bar": bar,
                "price": round(float(last["Close"]), 4),
                "vol_x": round(float(last["vol_x"]), 1), "rsi": round(float(last["rsi"]), 0),
                "detail": "Ausbruch + Volumen + über SMA50" if kind == "LONG" else "Bruch des 20-Tage-Tiefs + Volumen",
            })
            if kind == "LONG":
                live.notify(
                    settings, f"Scanner-Signal {t}",
                    f"{t}: Ausbruch über das 20-Tage-Hoch mit {last['vol_x']:.1f}x Volumen, "
                    f"Kurs {last['Close']:.2f}, RSI {last['rsi']:.0f}.\n"
                    "Kandidat, keine Garantie. Tagessignal kann bis Handelsschluss wieder verschwinden.",
                    "high",
                )
    return events


# ---------- Teil B: Ausloeser aus watchlist.json ----------
def watch_triggers(cfg, st, now):
    events = []
    s = cfg["settings"]
    for stock in cfg["stocks"]:
        if not stock.get("armed", True):
            continue
        t = stock["ticker"]
        c, max_entry = live.levels(stock, s)
        is_open, mins_left = live.market_status(stock, now)
        if not is_open:
            continue
        q = fetch_retry(t)
        if q is None:
            print(f"{t}: keine Minutendaten")
            continue
        price = q["price"]
        state = live.evaluate(stock, price, q["recent_high"], max_entry)
        window = stock.get("window_minutes", s["window_minutes"])
        left = None
        if state == "SIGNAL":
            since = st["since"].setdefault(t, now.isoformat())
            used = (now - datetime.fromisoformat(since)).total_seconds() / 60
            if used > window:
                state = "FENSTER_VORBEI"
            else:
                left = window - used
        else:
            st["since"].pop(t, None)
        print(f"{now:%H:%M} {stock.get('name', t)}: {price:.4f} | {live.LABEL[state]}")
        if state != st["last"].get(t):
            st["last"][t] = state
            if state in live.NOTIFY_STATES:
                title, body, prio = live.build(stock, state, price, c, max_entry, left,
                                               mins_left, q["age"], "")
                live.notify(s, title, body, prio)
                events.append({
                    "time": now.strftime("%Y-%m-%d %H:%M"), "ticker": t, "type": state, "bar": "",
                    "price": round(price, 4), "vol_x": "", "rsi": "",
                    "detail": f"Auslöser {stock['trigger']}, Einstieg bis {max_entry:.4f}",
                })
    return events


def main():
    args = sys.argv[1:]
    if args and args[0].lower() == "report":
        report()
        return
    cfg = live.read_config()
    if cfg is None:
        cfg = {"settings": dict(live.DEFAULTS), "stocks": []}
    cfg["settings"]["ntfy_topic"] = os.environ.get("NTFY_TOPIC") or cfg["settings"].get("ntfy_topic", "")
    now = datetime.now(live.BERLIN)
    st = load_state()
    tickers = list(scanner.WATCHLIST)
    for stock in cfg["stocks"]:
        if stock.get("armed", True) and stock["ticker"] not in tickers:
            tickers.append(stock["ticker"])
    events = scan_signals(tickers, st, now, cfg["settings"])
    events += watch_triggers(cfg, st, now)
    append_events(events)
    save_state(st)
    print(f"{now:%Y-%m-%d %H:%M} fertig: {len(events)} neue Ereignisse")


# ---------- Auswertung ----------
def report():
    if not os.path.exists(SIGNAL_LOG):
        print(f"Noch kein Protokoll ({SIGNAL_LOG}). Zuerst 'git pull', falls die Läufe in GitHub passieren.")
        return
    with open(SIGNAL_LOG, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    types = {}
    for r in rows:
        types[r["type"]] = types.get(r["type"], 0) + 1
    print("Ereignisse im Protokoll:", ", ".join(f"{k}: {v}" for k, v in sorted(types.items())) or "keine")
    longs = [r for r in rows if r["type"] == "LONG"]
    if not longs:
        print("Noch keine LONG-Signale zum Auswerten.")
        return
    data = scanner.load(sorted({r["ticker"] for r in longs}))
    done, pending = [], 0
    for r in longs:
        df = data.get(r["ticker"])
        if df is None:
            continue
        dates = [x.date() for x in df.index]
        logged = datetime.strptime(r["bar"], "%Y-%m-%d").date()
        if logged not in dates:
            continue
        res = scanner.outcome(df.reset_index(drop=True), dates.index(logged))
        if res is None:
            pending += 1
        else:
            done.append(res)
    base = []
    for df in data.values():
        d = df.reset_index(drop=True)
        for i in range(60, len(d) - 1):
            res = scanner.outcome(d, i)
            if res is not None:
                base.append(res)

    def line(name, rets):
        if not rets:
            print(f"{name}: keine Fälle")
            return
        n = len(rets)
        print(f"{name}: {n} Fälle | im Plus {100 * sum(r > 0 for r in rets) / n:.0f} % | "
              f"Ziel +{scanner.TP * 100:.0f} % erreicht {100 * sum(r >= scanner.TP for r in rets) / n:.0f} % | "
              f"Schnitt {100 * sum(rets) / n:.2f} %")

    print(f"\nRegel: Einstieg am Folgetag, +{scanner.TP * 100:.0f} % Ziel, -{scanner.SL * 100:.0f} % Stopp, "
          f"max. {scanner.MAX_DAYS} Tage, ohne Gebühren. Noch offen: {pending}\n")
    line("Protokollierte LONG-Signale", done)
    line("Alle Tage derselben Werte  ", base)
    print("\nNur wenn die Signale über viele Wochen und Fälle klar besser sind als 'alle Tage', "
          "hat das Signal einen Wert. Unter ca. 30 Fällen ist es Zufall.")


if __name__ == "__main__":
    main()
