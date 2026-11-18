"""
Katalysator-Score (ohne Handel, ohne Hebel)

  python catalyst.py                -> Score fuer die Watchlist aus scanner.py + Protokoll speichern
  python catalyst.py SNG.DE NVDA    -> nur diese Werte
  python catalyst.py check          -> wertet das Protokoll aus: was wurde aus den Signalen?

Punkte (je nach Datenlage, fehlende Daten zaehlen nicht mit):
  Short-Anteil am Float, Days to Cover, kleiner Float, Volumen, 20-Tage-Ausbruch,
  Quartalszahlen in den naechsten 14 Tagen, Analysten-Hochstufungen (30 Tage).
  Abzug: RSI ueber 80 (ueberhitzt).

Benoetigt scanner.py im selben Ordner. Daten: Yahoo Finance, bei deutschen
Nebenwerten oft luecken- oder verzoegert. Keine Anlageberatung.
"""
import csv
import os
import sys
from datetime import date, datetime

import pandas as pd
import yfinance as yf

import scanner

LOG = "catalyst_log.csv"
HIGH = 4          # ab dieser Punktzahl gilt ein Wert als "hoch"
HORIZON = 5       # Handelstage fuer die Auswertung
FIELDS = ["date", "ticker", "score", "max", "price", "reasons"]


# ---------- Daten holen ----------
def get_info(tk):
    try:
        return tk.info or {}
    except Exception:
        return {}


def next_earnings(tk):
    """Tage bis zu den naechsten Quartalszahlen oder None, wenn unbekannt."""
    try:
        cal = tk.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not dates:
            return None
        today = date.today()
        future = [d for d in dates if d >= today]
        return (min(future) - today).days if future else None
    except Exception:
        return None


def recent_upgrades(tk):
    """(Hochstufungen, Abstufungen) der letzten 30 Tage oder None."""
    try:
        ud = tk.upgrades_downgrades
        if ud is None or ud.empty:
            return None
        idx = ud.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        recent = ud[idx >= pd.Timestamp.now() - pd.Timedelta(days=30)]
        action = recent["Action"].astype(str).str.lower()
        return int((action == "up").sum()), int((action == "down").sum())
    except Exception:
        return None


# ---------- Punkte ----------
def score(last, info, earn_days, upd):
    parts = []  # (Punkte, Maximum, Text)

    sp = info.get("shortPercentOfFloat")
    if sp is not None:
        pct = sp * 100
        parts.append((2 if pct >= 20 else 1 if pct >= 10 else 0, 2,
                      f"Short-Anteil {pct:.1f} % vom Float"))

    dtc = info.get("shortRatio")
    if dtc is None and info.get("sharesShort") and info.get("averageVolume"):
        dtc = info["sharesShort"] / info["averageVolume"]
    if dtc is not None:
        parts.append((1 if dtc >= 5 else 0, 1, f"Days to Cover {dtc:.1f}"))

    fl = info.get("floatShares")
    if fl:
        parts.append((1 if fl < 50e6 else 0, 1, f"Float {fl / 1e6:.0f} Mio. Aktien"))

    if pd.notna(last["vol_x"]):
        parts.append((1 if last["vol_x"] >= scanner.VOL_MULT else 0, 1,
                      f"Volumen {last['vol_x']:.1f}x"))

    parts.append((1 if last["long"] else 0, 1,
                  "20-Tage-Ausbruch" if last["long"] else "kein Ausbruch"))

    if earn_days is not None:
        parts.append((1 if earn_days <= 14 else 0, 1, f"Quartalszahlen in {earn_days} Tagen"))

    if upd is not None:
        parts.append((1 if upd[0] > 0 else 0, 1,
                      f"Analysten 30 Tage: {upd[0]} hoch, {upd[1]} runter"))

    total = sum(p for p, _, _ in parts)
    maximum = sum(m for _, m, _ in parts)
    if pd.notna(last["rsi"]) and last["rsi"] > 80:
        total -= 1
        parts.append((-1, 0, f"überhitzt (RSI {last['rsi']:.0f})"))
    return total, maximum, parts


# ---------- Protokoll ----------
def append_log(rows):
    existing = set()
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8") as f:
            existing = {(r["date"], r["ticker"]) for r in csv.DictReader(f)}
    new = [r for r in rows if (r["date"], r["ticker"]) not in existing]
    if not new:
        return 0
    write_header = not os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            w.writeheader()
        w.writerows(new)
    return len(new)


def run(tickers):
    data = scanner.load(tickers)
    results, log_rows = [], []
    for t, df in data.items():
        d = scanner.add_signals(df)
        last = d.iloc[-1]
        tk = yf.Ticker(t)
        total, maximum, parts = score(last, get_info(tk), next_earnings(tk), recent_upgrades(tk))
        results.append({
            "Wert": t, "Score": f"{total}/{maximum}", "_ratio": total / maximum if maximum else 0,
            "_total": total, "Kurs": round(float(last["Close"]), 2),
            "Tag %": round((last["Close"] / d["Close"].iloc[-2] - 1) * 100, 1),
            "_parts": parts,
        })
        log_rows.append({
            "date": d.index[-1].strftime("%Y-%m-%d"), "ticker": t,
            "score": total, "max": maximum, "price": round(float(last["Close"]), 4),
            "reasons": "; ".join(txt for _, _, txt in parts),
        })
    if not results:
        print("Keine Daten.")
        return
    results.sort(key=lambda r: (r["_total"], r["_ratio"]), reverse=True)
    table = pd.DataFrame(results)[["Wert", "Score", "Kurs", "Tag %"]]
    print("\n=== Katalysator-Score (nach Punkten) ===")
    print(table.to_string(index=False))
    print(f"\n=== Details der besten {min(5, len(results))} ===")
    for r in results[:5]:
        print(f"\n{r['Wert']}  Score {r['Score']}")
        for p, m, txt in r["_parts"]:
            print(f"  {p}/{m}  {txt}" if m else f"  {p}    {txt}")
    n = append_log(log_rows)
    print(f"\n{n} neue Zeilen in {LOG} gespeichert. Auswerten später mit: python catalyst.py check")
    print(f"Hoch = ab {HIGH} Punkten. Ein hoher Score ist ein Hinweis, keine Garantie.")


# ---------- Auswertung ----------
def check():
    if not os.path.exists(LOG):
        print(f"Noch kein Protokoll ({LOG}). Erst 'python catalyst.py' an mehreren Tagen laufen lassen.")
        return
    with open(LOG, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    data = scanner.load(sorted({r["ticker"] for r in rows}))
    done, pending = [], 0
    for r in rows:
        df = data.get(r["ticker"])
        if df is None:
            continue
        logged = datetime.strptime(r["date"], "%Y-%m-%d").date()
        after = df[df.index.date > logged]
        if len(after) < HORIZON:
            pending += 1
            continue
        ret = float(after["Close"].iloc[HORIZON - 1]) / float(r["price"]) - 1
        done.append({"score": int(r["score"]), "ret": ret})
    print(f"\nAusgewertet: {len(done)} Einträge | noch offen (weniger als {HORIZON} Handelstage): {pending}")
    if not done:
        print("Noch zu früh. Warte ein paar Tage und starte 'check' erneut.")
        return
    df = pd.DataFrame(done)

    def line(name, g):
        if g.empty:
            print(f"{name}: keine Fälle")
            return
        print(f"{name}: {len(g)} Fälle | im Plus {100 * (g['ret'] > 0).mean():.0f} % "
              f"| ≥ +2 % {100 * (g['ret'] >= 0.02).mean():.0f} % | Schnitt {100 * g['ret'].mean():.2f} %")

    print(f"\nErgebnis nach {HORIZON} Handelstagen, ohne Gebühren:\n")
    line(f"Score ≥ {HIGH}  ", df[df["score"] >= HIGH])
    line(f"Score < {HIGH}  ", df[df["score"] < HIGH])
    line("Alle         ", df)
    print("\nNur wenn 'Score hoch' über viele Wochen und Fälle klar besser ist als "
          "'Alle', hat der Score einen Wert. Unter ca. 30 Fällen ist es Zufall.")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0].lower() == "check":
        check()
    else:
        run(args or scanner.WATCHLIST)
