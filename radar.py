#!/usr/bin/env python3
"""
radar.py - waehlt bei jedem Lauf die Aktie, die nach dem Punktesystem des
Programms (Trend, Ausbruch, Volumen, Risiko) gerade am aussichtsreichsten
aussieht, schickt eine Push-Meldung mit Voranalyse und protokolliert sie.

Befehle:
  python radar.py dry      Scan ansehen (keine Push, kein Protokoll)
  python radar.py test     Test-Push senden
  python radar.py run      Scan + Push + Protokoll (das macht GitHub Actions)
  python radar.py report   Auswertung der protokollierten Signale

Eigene Aktien: Datei universe.txt anlegen, ein Kuerzel pro Zeile.
Kein Finanzrat. Die Punkte sind eine Regel, keine Vorhersage.
"""
import csv
import json
import os
import sys
import urllib.request
import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ---------------- Einstellungen ----------------
MIN_SCORE = 60            # ab diesem Punktestand (0-100) wird gemeldet
MAX_PUSH_PER_DAY = 4      # hoechstens so viele Meldungen pro Tag
COST_PCT = 0.7            # Platzhalter: Gebuehr + Spread in %, echte Werte eintragen
STOP_ATR = 1.5            # Stopp = Kurs minus 1,5 x ATR
TARGET_ATR = 2.0          # Ziel  = Kurs plus 2 x ATR
HORIZON_DAYS = 5          # Auswertung: Ziel/Stopp innerhalb von 5 Handelstagen

LOG = "radar_log.csv"
STATE = "radar_state.json"
UNIVERSE_FILE = "universe.txt"

DEFAULT_UNIVERSE = """
ADS.DE AIR.DE ALV.DE BAS.DE BAYN.DE BEI.DE BMW.DE BNR.DE CBK.DE CON.DE 1COV.DE
DTG.DE DBK.DE DB1.DE DHL.DE DTE.DE EOAN.DE FRE.DE FME.DE HNR1.DE HEI.DE HEN3.DE
IFX.DE MBG.DE MRK.DE MTX.DE MUV2.DE P911.DE PAH3.DE QIA.DE RHM.DE RWE.DE SAP.DE
SRT3.DE SIE.DE ENR.DE SHL.DE SY1.DE VOW3.DE VNA.DE ZAL.DE
AIXA.DE EVT.DE NDX1.DE TUI1.DE LHA.DE HAG.DE AFX.DE WAF.DE SMHN.DE TKA.DE DUE.DE
GXI.DE JEN.DE NEM.DE SDF.DE LEG.DE SZU.DE EVK.DE FPE3.DE GBF.DE HOT.DE KGX.DE
G1A.DE BC8.DE DWNI.DE TLX.DE UTDI.DE RRTL.DE SNG.DE O2D.DE SMWN.DE
NVDA AMD TSLA AAPL MSFT AMZN GOOGL META NFLX AVGO COIN MSTR PLTR SMCI UBER SHOP
CRWD PANW SNOW ARM MU INTC QCOM ADBE CRM ORCL DIS BA JPM GS V MA PYPL SQ ROKU
RIVN LCID NIO F GM XOM CVX LLY NVO PFE MRNA
"""

TZ_DE = ZoneInfo("Europe/Berlin")
TZ_US = ZoneInfo("America/New_York")


# ---------------- Hilfsfunktionen ----------------
def lade_universum():
    if os.path.exists(UNIVERSE_FILE):
        with open(UNIVERSE_FILE, encoding="utf-8") as f:
            ticks = [z.strip().upper() for z in f if z.strip() and not z.startswith("#")]
        if ticks:
            return sorted(set(ticks))
    return sorted(set(DEFAULT_UNIVERSE.split()))


def ist_deutsch(ticker):
    return ticker.endswith(".DE")


def markt_offen(tz, von, bis, jetzt_utc):
    lokal = jetzt_utc.astimezone(tz)
    if lokal.weekday() >= 5:
        return False
    t = lokal.time()
    return von <= t <= bis


def offene_maerkte(jetzt_utc):
    de = markt_offen(TZ_DE, dt.time(9, 0), dt.time(17, 30), jetzt_utc)
    us = markt_offen(TZ_US, dt.time(9, 30), dt.time(16, 0), jetzt_utc)
    return de, us


def sitzungsanteil(ticker, jetzt_utc, letzter_bar_tag):
    """Wie viel vom Handelstag ist vorbei (0.15 bis 1.0), fuer die Volumen-Hochrechnung."""
    if ist_deutsch(ticker):
        tz, von, bis = TZ_DE, dt.time(9, 0), dt.time(17, 30)
    else:
        tz, von, bis = TZ_US, dt.time(9, 30), dt.time(16, 0)
    lokal = jetzt_utc.astimezone(tz)
    if letzter_bar_tag != lokal.date() or lokal.time() >= bis:
        return 1.0
    if lokal.time() <= von:
        return 0.15
    gesamt = (dt.datetime.combine(lokal.date(), bis) - dt.datetime.combine(lokal.date(), von)).seconds
    vorbei = (dt.datetime.combine(lokal.date(), lokal.time()) - dt.datetime.combine(lokal.date(), von)).seconds
    return float(min(1.0, max(0.15, vorbei / gesamt)))


def rsi(close, n=14):
    d = close.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    v = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = g / v.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr_pct(df, n=14):
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return (tr.ewm(alpha=1 / n, adjust=False).mean() / df["Close"]) * 100


def zerlege(daten, tickers):
    """yfinance liefert bei mehreren Werten MultiIndex-Spalten -> Dict Ticker -> DataFrame."""
    out = {}
    if daten is None or len(daten) == 0:
        return out
    if isinstance(daten.columns, pd.MultiIndex):
        ebene0 = list(daten.columns.get_level_values(0).unique())
        for t in tickers:
            if t in ebene0:
                df = daten[t].dropna(how="all")
                if not df.empty:
                    out[t] = df
    elif len(tickers) == 1:
        out[tickers[0]] = daten.dropna(how="all")
    return out


# ---------------- Bewertung ----------------
def bewerte(ticker, df, jetzt_utc):
    """Gibt dict mit Punkten und Kennzahlen zurueck oder None."""
    df = df.dropna(subset=["Close", "High", "Low"]).copy()
    if len(df) < 55:
        return None
    df["Volume"] = df["Volume"].fillna(0)
    close = df["Close"]
    kurs = float(close.iloc[-1])
    if kurs <= 0:
        return None

    sma20 = float(close.rolling(20).mean().iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    hoch20 = float(df["High"].iloc[-21:-1].max())
    r = float(rsi(close).iloc[-1])
    a = float(atr_pct(df).iloc[-1])
    vol_schnitt = float(df["Volume"].iloc[-21:-1].mean())
    if vol_schnitt <= 0 or np.isnan(r) or np.isnan(a):
        return None

    tag = df.index[-1].date() if hasattr(df.index[-1], "date") else None
    anteil = sitzungsanteil(ticker, jetzt_utc, tag)
    relvol = float(df["Volume"].iloc[-1]) / anteil / vol_schnitt

    punkte = 0
    gruende = []

    # Trend (max 25)
    p = 0
    if kurs > sma50:
        p += 10
    if sma20 > sma50:
        p += 5
    if kurs > sma20:
        p += 10
    punkte += p
    gruende.append((p, 25, "Trend " + ("aufwaerts" if p >= 20 else "gemischt" if p >= 10 else "schwach")))

    # Ausbruch (max 25)
    abstand = (kurs / hoch20 - 1) * 100
    if kurs >= hoch20:
        p = 25
    elif abstand > -1:
        p = 15
    elif abstand > -3:
        p = 7
    else:
        p = 0
    punkte += p
    gruende.append((p, 25, f"{abstand:+.1f} % zum 20-Tage-Hoch"))

    # Volumen (max 25)
    if relvol >= 2:
        p = 25
    elif relvol >= 1.5:
        p = 18
    elif relvol >= 1.2:
        p = 10
    elif relvol >= 1.0:
        p = 4
    else:
        p = 0
    punkte += p
    gruende.append((p, 25, f"Volumen {relvol:.1f}x normal (hochgerechnet)"))

    # Risiko / Luft nach oben (max 25)
    if 50 <= r <= 70:
        p = 25
    elif 70 < r <= 80:
        p = 12
    elif r > 80:
        p = 0
    elif r >= 40:
        p = 8
    else:
        p = 3
    punkte += p
    gruende.append((p, 25, f"RSI {r:.0f}" + (" (ueberhitzt)" if r > 80 else "")))

    # Abzuege
    if a < 1.0:
        punkte -= 15
        gruende.append((-15, 0, f"Schwankung zu klein ({a:.1f} % ATR), Kosten fressen den Gewinn"))
    stop = kurs * (1 - STOP_ATR * a / 100)
    ziel = kurs * (1 + TARGET_ATR * a / 100)
    ziel_pct = (ziel / kurs - 1) * 100
    if ziel_pct < 2 * COST_PCT:
        punkte -= 15
        gruende.append((-15, 0, f"Ziel (+{ziel_pct:.1f} %) kaum ueber den Kosten"))

    return {
        "ticker": ticker, "kurs": kurs, "score": max(0, min(100, punkte)),
        "stop": stop, "ziel": ziel, "rsi": r, "atr": a, "relvol": relvol,
        "abstand": abstand, "gruende": gruende,
    }


def tagesdaten(tickers):
    import yfinance as yf
    d = yf.download(tickers, period="4mo", interval="1d", group_by="ticker",
                    auto_adjust=True, progress=False, threads=True)
    return zerlege(d, tickers)


def intraday_info(ticker, jetzt_utc):
    """Typische 15-Minuten-Schwankung und Alter der letzten Kerze."""
    try:
        import yfinance as yf
        d = yf.download(ticker, period="5d", interval="15m", auto_adjust=True, progress=False)
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        d = d.dropna(subset=["Close"])
        if len(d) < 20:
            return None, None
        spanne = ((d["High"] - d["Low"]) / d["Close"] * 100).tail(60).median()
        letzte = d.index[-1]
        if letzte.tzinfo is None:
            letzte = letzte.tz_localize("UTC")
        alter = (jetzt_utc - letzte.to_pydatetime().astimezone(dt.timezone.utc)).total_seconds() / 60 - 15
        # Kerzen tragen die Startzeit; Ende der Kerze = Start + 15 Min
        return float(spanne), max(0.0, float(alter))
    except Exception as e:  # noqa: BLE001
        print("Intraday-Daten nicht verfuegbar:", e)
        return None, None


# ---------------- Nachricht ----------------
def baue_nachricht(b, spanne, alter, jetzt_utc):
    de = ist_deutsch(b["ticker"])
    waehrung = "EUR" if de else "USD"
    lokal = jetzt_utc.astimezone(TZ_DE).strftime("%H:%M")
    zeilen = []
    zeilen.append(f"Kurs {b['kurs']:.2f} {waehrung} | Punkte {b['score']}/100")
    zeilen.append("")
    zeilen.append("Beobachte die naechsten 15 Min.")
    if spanne is not None:
        zeilen.append(f"Typische 15-Min-Schwankung: +/-{spanne:.2f} %")
    zeilen.append("")
    zeilen.append("Voranalyse (Szenarien, keine Prognose):")
    zeilen.append(f"- Haelt der Kurs, ist das Ziel {b['ziel']:.2f} (+{(b['ziel'] / b['kurs'] - 1) * 100:.1f} %) im Bereich.")
    zeilen.append(f"- Faellt er unter {b['stop']:.2f} ({(b['stop'] / b['kurs'] - 1) * 100:.1f} %), ist die Idee ungueltig.")
    zeilen.append(f"- Break-even mit Kosten ca. {b['kurs'] * (1 + COST_PCT / 100):.2f}.")
    zeilen.append("")
    zeilen.append("Warum:")
    for p, m, text in b["gruende"]:
        if m > 0:
            zeilen.append(f"- {text} ({p}/{m})")
        else:
            zeilen.append(f"- {text} ({p})")
    zeilen.append("")
    if alter is not None and alter > 5:
        zeilen.append(f"ACHTUNG: Daten ca. {alter:.0f} Min alt, Kurs in der Bitpanda-App pruefen.")
    else:
        zeilen.append("Kurs vor dem Handeln in der Bitpanda-App pruefen (Daten koennen verzoegert sein).")
    zeilen.append(f"Meldung {lokal} Uhr (DE). Kein Finanzrat.")
    return "\n".join(zeilen)


def sende_push(titel, text, prio="default"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC nicht gesetzt - keine Push gesendet.")
        return False
    titel_ascii = titel.encode("ascii", "ignore").decode("ascii")
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=text.encode("utf-8"), method="POST",
        headers={"Title": titel_ascii, "Priority": prio, "Tags": "chart_with_upwards_trend"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = 200 <= r.status < 300
            print("Push gesendet." if ok else f"Push-Fehler {r.status}")
            return ok
    except Exception as e:  # noqa: BLE001
        print("Push-Fehler:", e)
        return False


# ---------------- Zustand und Protokoll ----------------
def lade_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def speichere_state(s):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)


LOG_FELDER = ["zeit_utc", "zeit_de", "ticker", "kurs", "score", "stop", "ziel",
              "relvol", "rsi", "atr_pct", "daten_alter_min", "push_ok"]


def protokolliere(b, alter, push_ok, jetzt_utc):
    neu = not os.path.exists(LOG)
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if neu:
            w.writerow(LOG_FELDER)
        w.writerow([
            jetzt_utc.strftime("%Y-%m-%d %H:%M:%S"),
            jetzt_utc.astimezone(TZ_DE).strftime("%Y-%m-%d %H:%M:%S"),
            b["ticker"], f"{b['kurs']:.4f}", b["score"], f"{b['stop']:.4f}", f"{b['ziel']:.4f}",
            f"{b['relvol']:.2f}", f"{b['rsi']:.1f}", f"{b['atr']:.2f}",
            "" if alter is None else f"{alter:.0f}", int(push_ok),
        ])


# ---------------- Hauptablauf ----------------
def scan(jetzt_utc, nur_offene=True, tagesdaten_fn=None):
    tickers = lade_universum()
    if nur_offene:
        de, us = offene_maerkte(jetzt_utc)
        tickers = [t for t in tickers if (ist_deutsch(t) and de) or (not ist_deutsch(t) and us)]
        if not tickers:
            print("Kein Markt offen - nichts zu tun.")
            return []
    print(f"Pruefe {len(tickers)} Werte ...")
    daten = (tagesdaten_fn or tagesdaten)(tickers)
    ergebnisse = []
    for t, df in daten.items():
        try:
            b = bewerte(t, df, jetzt_utc)
        except Exception as e:  # noqa: BLE001
            print("Fehler bei", t, e)
            continue
        if b:
            ergebnisse.append(b)
    ergebnisse.sort(key=lambda x: x["score"], reverse=True)
    print(f"{len(ergebnisse)} Werte mit ausreichend Daten ({len(tickers) - len(ergebnisse)} uebersprungen).")
    return ergebnisse


def zeige_top(ergebnisse, n=10):
    print(f"\n{'Wert':<9}{'Punkte':>7}{'Kurs':>10}{'RelVol':>8}{'RSI':>6}{'ATR%':>6}")
    for b in ergebnisse[:n]:
        print(f"{b['ticker']:<9}{b['score']:>7}{b['kurs']:>10.2f}{b['relvol']:>8.1f}{b['rsi']:>6.0f}{b['atr']:>6.1f}")


def cmd_dry():
    jetzt = dt.datetime.now(dt.timezone.utc)
    erg = scan(jetzt, nur_offene=False)
    zeige_top(erg)
    if erg:
        b = erg[0]
        spanne, alter = intraday_info(b["ticker"], jetzt)
        print("\n--- So wuerde die Meldung aussehen ---\n")
        print(f"{b['ticker']}: bester Wert\n")
        print(baue_nachricht(b, spanne, alter, jetzt))


# ---------------- Daytrading-Modus (15-Minuten-Fenster) ----------------
DAY_WINDOW_MIN = 15   # Beobachtungsfenster: danach ist die Chance vorbei
DAY_TARGET = 2.0      # Ziel  = Kurs + 2 x typische 15-Min-Spanne
DAY_STOP = 1.0        # Stopp = Kurs - 1 x typische 15-Min-Spanne
DAY_LIMIT = 0.5       # Einstiegsgrenze = Kurs + halber Weg zum Ziel


def day_plan(b, spanne, jetzt_utc):
    """Ziel/Stopp/Einstiegsgrenze aus der 15-Min-Schwankung (nicht aus der Tagesschwankung)."""
    sp = spanne if spanne else max(b["atr"] / 5.0, 0.1)
    sp = max(float(sp), 0.05)
    k = b["kurs"]
    return {
        "spanne": sp,
        "ziel": k * (1 + DAY_TARGET * sp / 100),
        "stop": k * (1 - DAY_STOP * sp / 100),
        "limit": k * (1 + DAY_LIMIT * DAY_TARGET * sp / 100),
        "ziel_pct": DAY_TARGET * sp,
        "stop_pct": -DAY_STOP * sp,
        "ende": jetzt_utc + dt.timedelta(minutes=DAY_WINDOW_MIN),
    }


def baue_daytrade_nachricht(b, plan, alter, jetzt_utc):
    de = ist_deutsch(b["ticker"])
    w = "EUR" if de else "USD"
    von = jetzt_utc.astimezone(TZ_DE).strftime("%H:%M")
    bis = plan["ende"].astimezone(TZ_DE).strftime("%H:%M")
    netto = plan["ziel_pct"] - COST_PCT
    z = []
    z.append(f"JETZT {DAY_WINDOW_MIN} MIN BEOBACHTEN: {von} bis {bis} Uhr (DE)")
    z.append(f"Kurs {b['kurs']:.2f} {w} | Punkte {b['score']}/100")
    z.append("")
    z.append(f"Einstieg nur bis Kurs {plan['limit']:.2f}. Liegt er schon darueber oder ist es nach {bis} Uhr: Chance verpasst, nicht hinterherrennen.")
    z.append("")
    z.append(f"Ziel {plan['ziel']:.2f} (+{plan['ziel_pct']:.2f} %)")
    z.append(f"Stopp {plan['stop']:.2f} ({plan['stop_pct']:.2f} %)")
    z.append(f"Nach Kosten (ca. {COST_PCT} %) bleibt beim Ziel {netto:+.2f} % uebrig.")
    z.append(f"Typische 15-Min-Spanne: {plan['spanne']:.2f} %")
    z.append("")
    z.append("Warum:")
    for p_, m, text in b["gruende"]:
        z.append(f"- {text} ({p_}/{m})" if m > 0 else f"- {text} ({p_})")
    z.append("")
    if alter is not None and alter > 5:
        z.append(f"ACHTUNG: Daten ca. {alter:.0f} Min alt, Kurs in der Bitpanda-App pruefen.")
    else:
        z.append("Kurs vor dem Handeln in der Bitpanda-App pruefen.")
    z.append("Nach Fensterende kommt eine Auswertung. Kein Finanzrat.")
    return "\n".join(z)


def minuten_daten(ticker):
    import yfinance as yf
    d = yf.download(ticker, period="2d", interval="1m", auto_adjust=True, progress=False)
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = d.dropna(subset=["Close"])
    if d.index.tz is None:
        d.index = d.index.tz_localize("UTC")
    else:
        d.index = d.index.tz_convert("UTC")
    return d


def bewerte_fenster(sig, d):
    """Ziel/Stopp/nichts innerhalb des Fensters. Gibt (ergebnis, hoch%, tief%, ende%) oder None."""
    start = pd.Timestamp(sig["zeit_utc"])
    ende = pd.Timestamp(sig["ende_utc"])
    w = d[(d.index >= start.floor("min")) & (d.index < ende)]
    if w.empty:
        return None
    k = sig["kurs"]
    erg = "NICHTS"
    for _, r in w.iterrows():
        if r["Low"] <= sig["stop"]:
            erg = "STOPP"
            break
        if r["High"] >= sig["ziel"]:
            erg = "ZIEL"
            break
    return (erg, (w["High"].max() / k - 1) * 100, (w["Low"].min() / k - 1) * 100,
            (w["Close"].iloc[-1] / k - 1) * 100)


def nachfass(jetzt_utc, state, push_fn, minuten_fn):
    """Meldet das Ergebnis jedes Signals, dessen 15-Min-Fenster vorbei ist."""
    offen, rest = state.get("offen", []), []
    ergebnisse = state.get("ergebnisse", [])
    for sig in offen:
        ende = pd.Timestamp(sig["ende_utc"])
        alter_min = (pd.Timestamp(jetzt_utc) - ende).total_seconds() / 60
        if alter_min < 0:
            rest.append(sig)
            continue
        try:
            d = minuten_fn(sig["ticker"])
        except Exception as e:  # noqa: BLE001
            print("Minutendaten fehlen:", sig["ticker"], e)
            d = None
        vollstaendig = d is not None and not d.empty and d.index[-1] >= ende - pd.Timedelta(minutes=3)
        if not vollstaendig and alter_min < 45:
            rest.append(sig)  # Yahoo hinkt hinterher: naechster Lauf probiert es noch einmal
            continue
        res = bewerte_fenster(sig, d) if d is not None and not d.empty else None
        if res is None:
            print("Keine Daten fuer Auswertung:", sig["ticker"])
            continue
        erg, hoch, tief, end_pct = res
        pct = {"ZIEL": sig["ziel_pct"], "STOPP": sig["stop_pct"], "NICHTS": end_pct}[erg]
        netto = pct - COST_PCT
        ergebnisse.append({
            "zeit_de": sig["zeit_de"], "ticker": sig["ticker"], "score": sig["score"],
            "relvol": sig["relvol"], "ziel_pct": round(sig["ziel_pct"], 3),
            "stop_pct": round(sig["stop_pct"], 3), "ergebnis": erg, "pct": round(pct, 3),
            "netto": round(netto, 3), "hoch": round(hoch, 3), "tief": round(tief, 3),
            "unvollstaendig": (not vollstaendig),
        })
        wort = {"ZIEL": "Ziel wurde erreicht", "STOPP": "Stopp wurde ausgeloest",
                "NICHTS": "weder Ziel noch Stopp"}[erg]
        text = (f"{sig['ticker']}: {wort}.\n"
                f"Fenster {sig['zeit_de'][11:16]} Uhr + {DAY_WINDOW_MIN} Min, Kurs bei Meldung {sig['kurs']:.2f}.\n"
                f"Hoch {hoch:+.2f} %, Tief {tief:+.2f} %, Ende {end_pct:+.2f} %.\n"
                f"Ergebnis nach Kosten (ca. {COST_PCT} %): {netto:+.2f} %.\n"
                f"Fenster ist zu. Chance verpasst, nicht mehr hinterherrennen."
                + ("\n(Daten unvollstaendig)" if not vollstaendig else ""))
        print("\n" + text + "\n")
        push_fn(f"Fenster vorbei: {sig['ticker']} {erg}", text, "default")
    state["offen"] = rest
    state["ergebnisse"] = ergebnisse[-300:]
    return state


def baue_tagesbericht_day(state, heute_de):
    erg = state.get("ergebnisse", [])
    offen = len(state.get("offen", []))
    heute = [e for e in erg if str(e["zeit_de"]).startswith(heute_de)]
    z = [f"Tagesbericht {heute_de}", ""]
    if heute:
        z.append(f"HEUTE {len(heute)} ausgewertete Meldung(en):")
        for e in heute:
            z.append(f"- {e['ticker']} {int(e['score'])} Pkt, {str(e['zeit_de'])[11:16]} Uhr: {e['ergebnis']}, nach Kosten {e['netto']:+.2f} %")
    else:
        z.append("HEUTE keine ausgewertete Meldung.")
    if offen:
        z.append(f"({offen} Meldung(en) noch im Fenster oder in Auswertung)")
    n = len(erg)
    if n:
        ziele = sum(1 for e in erg if e["ergebnis"] == "ZIEL")
        stopps = sum(1 for e in erg if e["ergebnis"] == "STOPP")
        nichts = n - ziele - stopps
        schnitt = sum(e["netto"] for e in erg) / n
        z += ["", f"GESAMT {n} Meldungen: Ziel {ziele}, Stopp {stopps}, nichts {nichts}.",
              f"Trefferquote (Ziel zuerst): {ziele / n * 100:.0f} %.",
              f"Schnitt pro Meldung nach Kosten: {schnitt:+.2f} %.",
              f"Break-even vor Kosten: {DAY_STOP / (DAY_STOP + DAY_TARGET) * 100:.0f} % Trefferquote."]
    else:
        z += ["", "GESAMT noch keine ausgewerteten Meldungen."]
    z += ["", "VERBESSERUNG:"]
    if n < 10:
        z.append(f"- Erst {n} Faelle: zu wenig, um etwas abzuleiten (ab ca. 30 aussagekraeftig).")
    else:
        def grp(name, hoch, tief):
            if len(hoch) >= 4 and len(tief) >= 4:
                q = lambda g: sum(1 for e in g if e["ergebnis"] == "ZIEL") / len(g) * 100
                m = lambda g: sum(e["netto"] for e in g) / len(g)
                z.append(f"- {name}: hoch {q(hoch):.0f} % / {m(hoch):+.2f} % ({len(hoch)}) vs. niedrig {q(tief):.0f} % / {m(tief):+.2f} % ({len(tief)})")
        grp("Punkte ab 70", [e for e in erg if e["score"] >= 70], [e for e in erg if e["score"] < 70])
        grp("Volumen ab 1,0x", [e for e in erg if e["relvol"] >= 1.0], [e for e in erg if e["relvol"] < 1.0])
        if z[-1] == "VERBESSERUNG:":
            z.append("- Noch keine Untergruppe mit genug Faellen.")
    z += ["", "Nur Beobachtung, kein Finanzrat."]
    return "\n".join(z)


def cmd_daily_day():
    heute_de = dt.datetime.now(TZ_DE).strftime("%Y-%m-%d")
    text = baue_tagesbericht_day(lade_state(), heute_de)
    print(text)
    sende_push("Radar Tagesbericht", text)


def cmd_report_day():
    print(baue_tagesbericht_day(lade_state(), dt.datetime.now(TZ_DE).strftime("%Y-%m-%d")))


def cmd_run(jetzt=None, tagesdaten_fn=None, intraday_fn=None, push_fn=None, minuten_fn=None):
    jetzt = jetzt or dt.datetime.now(dt.timezone.utc)
    push_fn = push_fn or sende_push
    intraday_fn = intraday_fn or intraday_info
    state = lade_state()
    try:
        state = nachfass(jetzt, state, push_fn, minuten_fn or minuten_daten)
    except Exception as e:  # noqa: BLE001
        print("Nachfass-Fehler:", e)
    erg = scan(jetzt, nur_offene=True, tagesdaten_fn=tagesdaten_fn)
    heute = jetzt.astimezone(TZ_DE).strftime("%Y-%m-%d")
    tag = state.get(heute, {"pushed": [], "count": 0})
    state = {heute: tag, "offen": state.get("offen", []), "ergebnisse": state.get("ergebnisse", [])}
    if not erg:
        speichere_state(state)
        return
    zeige_top(erg, 5)
    if tag["count"] >= MAX_PUSH_PER_DAY:
        print("Tageslimit fuer Meldungen erreicht.")
        speichere_state(state)
        return
    kandidat = next((b for b in erg if b["score"] >= MIN_SCORE and b["ticker"] not in tag["pushed"]), None)
    if not kandidat:
        print(f"Kein neuer Wert mit mindestens {MIN_SCORE} Punkten.")
        speichere_state(state)
        return
    spanne, alter = intraday_fn(kandidat["ticker"], jetzt)
    plan = day_plan(kandidat, spanne, jetzt)
    text = baue_daytrade_nachricht(kandidat, plan, alter, jetzt)
    titel = f"Radar: {kandidat['ticker']} {kandidat['score']}/100 - 15 Min!"
    print("\n" + titel + "\n" + text + "\n")
    ok = push_fn(titel, text, "high" if kandidat["score"] >= 75 else "default")
    log_b = dict(kandidat, stop=plan["stop"], ziel=plan["ziel"])
    protokolliere(log_b, alter, ok, jetzt)
    tag["pushed"].append(kandidat["ticker"])
    tag["count"] += 1
    if ok:
        state["offen"].append({
            "ticker": kandidat["ticker"], "kurs": kandidat["kurs"], "score": kandidat["score"],
            "relvol": kandidat["relvol"], "ziel": plan["ziel"], "stop": plan["stop"],
            "ziel_pct": plan["ziel_pct"], "stop_pct": plan["stop_pct"],
            "zeit_utc": jetzt.strftime("%Y-%m-%d %H:%M:%S+00:00"),
            "zeit_de": jetzt.astimezone(TZ_DE).strftime("%Y-%m-%d %H:%M:%S"),
            "ende_utc": plan["ende"].strftime("%Y-%m-%d %H:%M:%S+00:00"),
        })
    speichere_state(state)



def cmd_test():
    ok = sende_push("Radar Test", "Push funktioniert. Das ist eine Testmeldung vom Radar.")
    sys.exit(0 if ok else 1)


def cmd_report():
    if not os.path.exists(LOG):
        print("Noch kein Protokoll (radar_log.csv).")
        return
    import yfinance as yf
    df = pd.read_csv(LOG)
    if df.empty:
        print("Protokoll ist leer.")
        return
    print(f"{len(df)} Signale im Protokoll.\n")
    ergebnisse = []
    for _, z in df.iterrows():
        start = pd.Timestamp(z["zeit_utc"], tz="UTC")
        try:
            d = yf.download(z["ticker"], start=(start - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                            interval="15m", auto_adjust=True, progress=False)
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            d = d.dropna(subset=["Close"])
            if d.index.tz is None:
                d.index = d.index.tz_localize("UTC")
            d = d[d.index > start]
        except Exception as e:  # noqa: BLE001
            print("Daten fehlen fuer", z["ticker"], e)
            continue
        if d.empty:
            ergebnisse.append((z, "offen", np.nan))
            continue
        ende = start + pd.Timedelta(days=HORIZON_DAYS + 2)
        d = d[d.index <= ende]
        status, res = "offen", (d["Close"].iloc[-1] / z["kurs"] - 1) * 100
        for _, k in d.iterrows():
            if k["Low"] <= z["stop"]:
                status, res = "Stopp", (z["stop"] / z["kurs"] - 1) * 100
                break
            if k["High"] >= z["ziel"]:
                status, res = "Ziel", (z["ziel"] / z["kurs"] - 1) * 100
                break
        ergebnisse.append((z, status, res))
    print(f"{'Zeit (DE)':<17}{'Wert':<9}{'Punkte':>7}  {'Ergebnis':<8}{'%':>7}")
    for z, s, r in ergebnisse:
        print(f"{z['zeit_de'][:16]:<17}{z['ticker']:<9}{int(z['score']):>7}  {s:<8}{r:>7.2f}")
    ziele = sum(1 for _, s, _ in ergebnisse if s == "Ziel")
    stopps = sum(1 for _, s, _ in ergebnisse if s == "Stopp")
    offen = sum(1 for _, s, _ in ergebnisse if s == "offen")
    abgeschlossen = ziele + stopps
    print(f"\nZiel zuerst: {ziele} | Stopp zuerst: {stopps} | noch offen: {offen}")
    if abgeschlossen:
        print(f"Trefferquote der abgeschlossenen: {ziele / abgeschlossen * 100:.0f} % ({abgeschlossen} Faelle)")
    print(f"Break-even bei diesem Ziel/Stopp-Verhaeltnis: {STOP_ATR / (STOP_ATR + TARGET_ATR) * 100:.0f} % Trefferquote (vor Kosten)")
    print("Unter ca. 30 abgeschlossenen Faellen ist die Quote vor allem Zufall.")


# ---------------- Tagesbericht (Push) ----------------
def auswerten(df):
    """Wie cmd_report, aber gibt die Ergebnisse zurueck: Liste von (Zeile, Status, Prozent)."""
    import yfinance as yf
    ergebnisse = []
    for _, z in df.iterrows():
        start = pd.Timestamp(z["zeit_utc"], tz="UTC")
        try:
            d = yf.download(z["ticker"], start=(start - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                            interval="15m", auto_adjust=True, progress=False)
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            d = d.dropna(subset=["Close"])
            if d.index.tz is None:
                d.index = d.index.tz_localize("UTC")
            d = d[d.index > start]
        except Exception as e:  # noqa: BLE001
            print("Daten fehlen fuer", z["ticker"], e)
            continue
        if d.empty:
            ergebnisse.append((z, "offen", float("nan")))
            continue
        d = d[d.index <= start + pd.Timedelta(days=HORIZON_DAYS + 2)]
        status, res = "offen", (d["Close"].iloc[-1] / z["kurs"] - 1) * 100
        for _, k in d.iterrows():
            if k["Low"] <= z["stop"]:
                status, res = "Stopp", (z["stop"] / z["kurs"] - 1) * 100
                break
            if k["High"] >= z["ziel"]:
                status, res = "Ziel", (z["ziel"] / z["kurs"] - 1) * 100
                break
        ergebnisse.append((z, status, res))
    return ergebnisse


def quote(erg):
    ziele = sum(1 for _, s, _ in erg if s == "Ziel")
    stopps = sum(1 for _, s, _ in erg if s == "Stopp")
    n = ziele + stopps
    return ziele, stopps, n, (ziele / n * 100 if n else None)


def hinweise(erg):
    """Verbesserungshinweise - nur wenn genug abgeschlossene Faelle da sind."""
    _, _, n, _ = quote(erg)
    if n < 10:
        return [f"Erst {n} abgeschlossene Faelle: noch zu wenig, um etwas abzuleiten (ab ca. 30 aussagekraeftig)."]
    out = []
    def vergleich(name, hoch, tief):
        zh, sh, nh, qh = quote(hoch)
        zt, st, nt, qt = quote(tief)
        if nh >= 4 and nt >= 4:
            out.append(f"{name}: hoch {qh:.0f} % ({nh}) vs. niedrig {qt:.0f} % ({nt})")
    vergleich("Punkte ab 70", [e for e in erg if e[0]["score"] >= 70], [e for e in erg if e[0]["score"] < 70])
    vergleich("Volumen ab 1,0x", [e for e in erg if e[0]["relvol"] >= 1.0], [e for e in erg if e[0]["relvol"] < 1.0])
    if not out:
        out.append("Noch keine Untergruppe mit genug Faellen fuer einen Vergleich.")
    return out


def baue_tagesbericht(erg, heute_de):
    heute = [e for e in erg if str(e[0]["zeit_de"]).startswith(heute_de)]
    zeilen = [f"Tagesbericht {heute_de}", ""]
    if heute:
        zeilen.append(f"HEUTE {len(heute)} Meldung(en):")
        for z, s, r in heute:
            rr = "k.A." if pd.isna(r) else f"{r:+.1f} %"
            zeilen.append(f"- {z['ticker']} {int(z['score'])} Pkt, {str(z['zeit_de'])[11:16]} Uhr, Kurs {z['kurs']:.2f}: {s} ({rr})")
    else:
        zeilen.append("HEUTE keine Meldung (kein Wert hatte 60+ Punkte oder Boerse zu).")
    ziele, stopps, n, q = quote(erg)
    offen = sum(1 for _, s, _ in erg if s == "offen")
    zeilen += ["", f"GESAMT {len(erg)} Signale: Ziel {ziele}, Stopp {stopps}, offen {offen}."]
    if q is not None:
        zeilen.append(f"Trefferquote: {q:.0f} % ({n} abgeschlossene Faelle).")
    else:
        zeilen.append("Trefferquote: noch keine abgeschlossenen Faelle.")
    zeilen.append(f"Break-even: {STOP_ATR / (STOP_ATR + TARGET_ATR) * 100:.0f} % (vor Kosten).")
    zeilen += ["", "VERBESSERUNG:"] + [f"- {h}" for h in hinweise(erg)]
    zeilen += ["", "Nur Beobachtung, kein Finanzrat."]
    return "\n".join(zeilen)


def cmd_daily():
    if not os.path.exists(LOG):
        sende_push("Radar Tagesbericht", "Noch kein Protokoll. Heute gab es keine Meldung.")
        return
    df = pd.read_csv(LOG)
    if df.empty:
        sende_push("Radar Tagesbericht", "Protokoll ist leer. Noch keine Meldungen.")
        return
    heute_de = dt.datetime.now(TZ_DE).strftime("%Y-%m-%d")
    text = baue_tagesbericht(auswerten(df), heute_de)
    print(text)
    sende_push("Radar Tagesbericht", text)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "dry"
    if arg == "dry":
        cmd_dry()
    elif arg == "test":
        cmd_test()
    elif arg == "run":
        cmd_run()
    elif arg == "report":
        cmd_report_day()
    elif arg == "report_alt":
        cmd_report()
    elif arg == "daily":
        cmd_daily_day()
    else:
        print(__doc__)
