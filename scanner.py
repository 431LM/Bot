"""
Aktien-Signalscanner (ohne Handel, ohne Hebel)

  python scanner.py                  -> aktueller Scan der Watchlist
  python scanner.py SNG.DE NVDA      -> Scan nur dieser Werte
  python scanner.py backtest         -> prueft, wie die Signale in der Vergangenheit lagen
  python scanner.py backtest SNG.DE  -> Backtest nur fuer diese Werte

Datenquelle: Yahoo Finance (ca. 15 Min. verzoegert). Keine Anlageberatung.
Ein Signal ist ein Kandidat, keine Garantie.
"""
import sys

import pandas as pd
import yfinance as yf

# Eigene Werte hier ergaenzen. Deutsche Boersenplaetze enden auf .DE
WATCHLIST = [
    "SNG.DE",   # Singulus
    "NVDA", "AMD", "TSLA", "PLTR", "AAPL", "MSFT", "META", "AMZN",
    "SAP.DE", "SIE.DE", "RHM.DE", "ALV.DE", "AIR.DE", "IFX.DE",
    "COIN", "MSTR", "SMCI", "ARM", "UBER",
]

TP = 0.02        # Gewinnziel +2 %
SL = 0.02        # Stopp -2 %
MAX_DAYS = 5     # nach 5 Handelstagen wird zum Schlusskurs ausgestiegen
LOOKBACK = 20    # Ausbruch ueber das 20-Tage-Hoch
VOL_MULT = 2.0   # Volumen mind. doppelt so hoch wie der 20-Tage-Schnitt


def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - (100 / (1 + gain / loss))


def load(tickers):
    data = yf.download(
        tickers, period="2y", interval="1d", auto_adjust=True,
        group_by="ticker", progress=False, threads=True,
    )
    if not isinstance(data.columns, pd.MultiIndex):
        data = pd.concat({tickers[0]: data}, axis=1)
    out = {}
    for t in tickers:
        try:
            df = data[t].dropna()
        except KeyError:
            print(f"Keine Daten fuer {t}")
            continue
        if len(df) > 80:
            out[t] = df
        else:
            print(f"Zu wenig Daten fuer {t}")
    return out


def add_signals(df):
    df = df.copy()
    df["rsi"] = rsi(df["Close"])
    df["sma50"] = df["Close"].rolling(50).mean()
    df["hh"] = df["High"].rolling(LOOKBACK).max().shift(1)
    df["ll"] = df["Low"].rolling(LOOKBACK).min().shift(1)
    df["vol_avg"] = df["Volume"].rolling(20).mean().shift(1)
    df["vol_x"] = df["Volume"] / df["vol_avg"]
    df["long"] = (
        (df["Close"] > df["hh"]) & (df["vol_x"] >= VOL_MULT) & (df["Close"] > df["sma50"])
    )
    df["weak"] = (df["Close"] < df["ll"]) & (df["vol_x"] >= VOL_MULT)
    return df


def outcome(df, i):
    """Einstieg zum naechsten Eroeffnungskurs, Ziel +TP, Stopp -SL, max. MAX_DAYS.
    Wenn Ziel und Stopp am selben Tag moeglich sind, zaehlt der Stopp (vorsichtig)."""
    if i + MAX_DAYS > len(df) - 1:
        return None
    entry = df["Open"].iloc[i + 1]
    if not entry or entry != entry:
        return None
    for j in range(i + 1, i + MAX_DAYS + 1):
        if df["Low"].iloc[j] <= entry * (1 - SL):
            return -SL
        if df["High"].iloc[j] >= entry * (1 + TP):
            return TP
    return df["Close"].iloc[i + MAX_DAYS] / entry - 1


def scan(tickers):
    rows = []
    for t, df in load(tickers).items():
        d = add_signals(df)
        last = d.iloc[-1]
        prev = d["Close"].iloc[-2]
        flag = ""
        if last["long"]:
            flag = "LONG-Kandidat"
        elif last["weak"]:
            flag = "Schwaeche"
        if flag and last["rsi"] > 80:
            flag += " (ueberhitzt)"
        rows.append({
            "Wert": t, "Signal": flag or "-",
            "Kurs": round(last["Close"], 2),
            "Tag %": round((last["Close"] / prev - 1) * 100, 1),
            "Vol x": round(last["vol_x"], 1),
            "RSI": round(last["rsi"], 0),
            "Datum": d.index[-1].strftime("%d.%m."),
        })
    if not rows:
        print("Keine Daten geladen.")
        return
    table = pd.DataFrame(rows).sort_values("Vol x", ascending=False)
    hits = table[table["Signal"] != "-"]
    print("\n=== Signale ===")
    print(hits.to_string(index=False) if len(hits) else "Heute keine Signale.")
    print("\n=== Alle Werte (nach Volumen) ===")
    print(table.to_string(index=False))
    print("\nHinweis: Kandidaten, keine Garantie. Kein Hebel ohne eigenen Test.")


def backtest(tickers):
    sig, base = [], []
    for t, df in load(tickers).items():
        d = add_signals(df).reset_index(drop=True)
        for i in range(60, len(d) - 1):
            r = outcome(d, i)
            if r is None:
                continue
            base.append(r)
            if d["long"].iloc[i]:
                sig.append(r)

    def stats(name, rets):
        if not rets:
            print(f"{name}: keine Faelle")
            return
        s = pd.Series(rets)
        print(f"{name}: {len(s)} Trades | Win% {100 * (s > 0).mean():.1f} "
              f"| Schnitt {100 * s.mean():.2f} % pro Trade")

    print(f"\nRegel: Einstieg naechster Tag, +{TP * 100:.0f} % Ziel, -{SL * 100:.0f} % Stopp, "
          f"max. {MAX_DAYS} Tage. Ohne Gebuehren und Spread.\n")
    stats("Signal-Tage  ", sig)
    stats("Alle Tage    ", base)
    print("\nWenn die Signal-Tage nicht deutlich besser sind als alle Tage, "
          "hat das Signal keinen Vorteil.")
    print("Unter ca. 30 Trades sind die Zahlen kaum aussagekraeftig.")


if __name__ == "__main__":
    args = sys.argv[1:]
    mode = "scan"
    if args and args[0].lower() == "backtest":
        mode, args = "backtest", args[1:]
    tickers = args or WATCHLIST
    scan(tickers) if mode == "scan" else backtest(tickers)
