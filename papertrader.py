#!/usr/bin/env python3
"""
papertrader.py - Paper-Trading mit Hebel. KEIN ECHTGELD, es wird nur gerechnet.

Der Paper-Trader nimmt die Signale deiner bestehenden Bausteine (Radar und Muster,
gelesen aus radar_log.csv und muster_log.csv) und führt damit ein Übungskonto:

  * 100 EUR Startkapital, 5-facher Hebel, Long und Short
  * Positionsgröße so, dass der Verlust am Stopp (inkl. Kosten und Rutschen) höchstens
    RISIKO_PRO_TRADE_PCT des Kapitals beträgt
  * Gewinnmitnahme in 4 Teilen zu je 25 %: Ziel 1, Ziel 2, Ziel 3 und ein Rest, der
    mit einem nachgezogenen Stopp (Trailing) große Bewegungen mitnimmt
  * Kosten, Rutschen (Slippage) und Finanzierungskosten werden vollständig abgezogen
  * Risikobremsen: höchstens 3 Positionen, Tagesverlust-Stopp, Drawdown-Stopp
  * Auswertung: Netto-Ergebnis, Trefferquote, Profit Factor, Drawdown und die Frage,
    ob die Strategie reif für mehr als Papier wäre

  python papertrader.py          Konto aktualisieren (macht GitHub Actions)
  python papertrader.py bericht  Bilanz anzeigen und aufs Handy schicken

Wichtig: Eine Position mit 5x Hebel verliert bei 1 % Kursrückgang 5 % deines Kapitals.
Keine Anlageberatung. Echtgeld bleibt aus, bis die Freigabe-Prüfung im Bericht bestanden ist.
"""
import json
import os
import sys
import datetime as dt

import numpy as np
import pandas as pd

import auswertung as A

# ---------------- Einstellungen ----------------
START_KAPITAL = 100.0
HEBEL = 5
KOSTEN_PCT = A.COST_PCT            # Hin- und Rückweg zusammen, in % vom Positionswert (Gebühr + Spread)
SLIPPAGE_PCT = 0.05                # Rutschen pro Ausführung in % (Kurs läuft dir davon)
STOP_SLIPPAGE_FAKTOR = 2.0         # Stopps rutschen stärker (Faktor auf SLIPPAGE_PCT)
FINANZIERUNG_PCT_TAG = 0.03        # Haltekosten für gehebelte Positionen, % vom Positionswert pro Tag
RISIKO_PRO_TRADE_PCT = 3.0         # höchster Verlust pro Trade in % vom Kapital (inkl. Kosten)
MAX_POSITIONEN = 3
TAGESVERLUST_STOP_PCT = 6.0        # ab diesem Tagesverlust (in % vom Kapital) keine neuen Trades mehr
MAX_DRAWDOWN_STOP_PCT = 25.0       # ab diesem Rückgang vom Höchststand: Pause, nur Verwaltung
MIN_STOP_PREIS_PCT = 1.0           # Stopp mindestens so weit weg (Kursbewegung in %), sonst zu nah am Rauschen
MAX_STOP_PREIS_PCT = 8.0           # weiter weg ist bei 5x zu nah an der Zwangsschließung
ZIEL_BASIS = "kapital"             # "kapital": Ziele in % deines eingesetzten Kapitals (Kurs = Ziel / Hebel)
                                   # "kurs":    Ziele sind die Kursbewegung selbst
ZIELE = [5.0, 7.5, 10.0]           # Ziel 1, 2, 3 in %
ANTEILE = [0.25, 0.25, 0.25, 0.25] # Teile; der letzte läuft mit Trailing weiter
TRAIL_PREIS_PCT = 1.0              # Trailing-Abstand vom besten Kurs in % Kursbewegung
STOP_NACHZIEHEN = True             # nach Ziel 1: Stopp auf Einstieg, nach Ziel 2: Stopp auf Ziel 1
LIQ_VERLUST_ANTEIL = 0.9           # Zwangsschließung, wenn 90 % der Marge weg sind
MAX_HALTE_TAGE = 10
RUECKWIRKEND_TAGE = 2              # beim ersten Start Signale der letzten Tage nachrechnen
MIN_PAPER_TRADES = 300             # so viele Papier-Trades vor jeder Echtgeld-Überlegung
STATE = "paper_state.json"
BAR_MIN = 15


# ---------------- Zustand ----------------
def lade_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:  # noqa: BLE001
        s = {}
    s.setdefault("kapital", START_KAPITAL)
    s.setdefault("spitze", s["kapital"])
    s.setdefault("start", None)
    s.setdefault("offen", [])
    s.setdefault("trades", [])
    s.setdefault("gesehen", [])
    s.setdefault("abgelehnt", [])
    s.setdefault("pausiert", False)
    return s


def speichere_state(s):
    s["gesehen"] = s["gesehen"][-1500:]
    s["abgelehnt"] = s["abgelehnt"][-200:]
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=0)


def ts(x):
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


# ---------------- Signale lesen ----------------
def lese_signale():
    out = []
    for r in A.lese_csv("radar_log.csv"):
        if r.get("push_ok") != "1":
            continue
        out.append(dict(id=f"R|{r['ticker']}|{r['zeit_utc']}", system="Radar", ticker=r["ticker"], richtung=1,
                        kurs=float(r["kurs"]), stop=float(r["stop"]), zeit=ts(r["zeit_utc"])))
    for r in A.lese_csv("muster_log.csv"):
        if str(r.get("gemeldet")) != "1":
            continue
        out.append(dict(id=f"M|{r['ticker']}|{r['muster']}|{r['kerze_utc']}", system="Muster", ticker=r["ticker"],
                        richtung=int(r["richtung"]), kurs=float(r["kurs"]), stop=float(r["stopp"]),
                        zeit=ts(r["kerze_utc"]) + pd.Timedelta(minutes=BAR_MIN)))
    return sorted(out, key=lambda x: x["zeit"])


# ---------------- Rechnen ----------------
def ziel_preise(entry, d):
    teil = HEBEL if ZIEL_BASIS == "kapital" else 1.0
    return [entry * (1 + d * z / teil / 100) for z in ZIELE]


def berechne_wert(kapital, entry, stop, offen_wert):
    """Positionswert in EUR so, dass der Verlust am Stopp höchstens RISIKO_PRO_TRADE_PCT beträgt."""
    dist = abs(entry - stop) / entry * 100
    verlust_pct = dist + KOSTEN_PCT + SLIPPAGE_PCT * (1 + STOP_SLIPPAGE_FAKTOR)  # % vom Positionswert
    wert = kapital * RISIKO_PRO_TRADE_PCT / 100 / (verlust_pct / 100)
    return min(wert, kapital * HEBEL - offen_wert), verlust_pct


def fill(p, anteil, preis, stopp=False):
    """Ein Teil der Position wird geschlossen. Rutschen und anteilige Kosten abziehen."""
    d = p["richtung"]
    slip = SLIPPAGE_PCT * (STOP_SLIPPAGE_FAKTOR if stopp else 1.0) / 100
    ausgang = preis * (1 - d * slip)
    brutto = d * (ausgang - p["entry"]) * p["menge"] * anteil
    kosten = p["wert"] * anteil * KOSTEN_PCT / 200
    p["netto"] += brutto - kosten
    p["kosten"] += kosten
    p["rest"] -= anteil
    return ausgang


def oeffne(sig, state, jetzt):
    """Prüft das Risiko und eröffnet eine Paper-Position. Gibt (Position|None, Text) zurück."""
    d, kapital = sig["richtung"], state["kapital"]
    entry = sig["kurs"] * (1 + d * SLIPPAGE_PCT / 100)
    stop = sig["stop"]
    dist = abs(sig["kurs"] - stop) / sig["kurs"] * 100
    if (d > 0 and stop >= sig["kurs"]) or (d < 0 and stop <= sig["kurs"]):
        return None, "Stopp liegt auf der falschen Seite"
    if dist < MIN_STOP_PREIS_PCT:
        stop = sig["kurs"] * (1 - d * MIN_STOP_PREIS_PCT / 100)
        dist = MIN_STOP_PREIS_PCT
    if dist > MAX_STOP_PREIS_PCT:
        return None, f"Stopp {dist:.1f} % weit weg: zu riskant bei {HEBEL}x"
    if state["pausiert"]:
        return None, "Pause wegen Drawdown-Grenze"
    if len(state["offen"]) >= MAX_POSITIONEN:
        return None, f"schon {MAX_POSITIONEN} Positionen offen"
    if any(p["ticker"] == sig["ticker"] for p in state["offen"]):
        return None, "Position in diesem Wert schon offen"
    heute = jetzt.strftime("%Y-%m-%d")
    tagesnetto = sum(t["netto"] for t in state["trades"] if t["ende"][:10] == heute)
    if tagesnetto <= -TAGESVERLUST_STOP_PCT / 100 * kapital:
        return None, "Tagesverlust-Grenze erreicht"
    offen_wert = sum(p["wert"] * p["rest"] for p in state["offen"])
    wert, verlust_pct = berechne_wert(kapital, sig["kurs"], stop, offen_wert)
    if wert < 10:
        return None, "Position kleiner als 10 EUR, nicht sinnvoll"
    p = dict(id=sig["id"], system=sig["system"], ticker=sig["ticker"], richtung=d, entry=entry, stop=stop,
             start_stop=stop, wert=wert, menge=wert / entry, rest=1.0, ziele=0, ziel_preise=ziel_preise(entry, d),
             trail=False, extrem=entry, netto=-wert * KOSTEN_PCT / 200, kosten=wert * KOSTEN_PCT / 200, fin=0.0,
             start=sig["zeit"].isoformat(), naechste_bar=sig["zeit"].isoformat(), fin_bis=sig["zeit"].isoformat(),
             kapital_start=kapital, letzter_kurs=entry)
    risiko = wert * verlust_pct / 100
    z = ziel_preise(entry, d)
    text = (f"{'LONG' if d > 0 else 'SHORT'} {sig['ticker']} ({sig['system']}) eröffnet. "
            f"Position {A.zahl(wert, 0)} EUR ({A.zahl(wert / kapital, 1)}-fach, max. {HEBEL}x). "
            f"Stopp {A.zahl(stop, 4)}. Ziele {A.zahl(z[0], 4)} / {A.zahl(z[1], 4)} / {A.zahl(z[2], 4)}. "
            f"Max. Verlust ca. {A.zahl(risiko)} EUR ({A.zahl(risiko / kapital * 100, 1)} % vom Kapital), "
            f"Kosten ca. {A.zahl(wert * KOSTEN_PCT / 100)} EUR.")
    return p, text


def schliesse(p, state, ende, grund):
    netto = p["netto"]
    t = dict(id=p["id"], system=p["system"], ticker=p["ticker"], richtung=p["richtung"], start=p["start"],
             ende=ende.isoformat(), entry=p["entry"], wert=p["wert"], ziele=p["ziele"], grund=grund,
             netto=round(netto, 4), kosten=round(p["kosten"], 4), fin=round(p["fin"], 4),
             netto_pct=round(netto / p["kapital_start"] * 100, 3))
    state["trades"].append(t)
    state["kapital"] += netto
    state["spitze"] = max(state["spitze"], state["kapital"])
    dd = (state["spitze"] - state["kapital"]) / state["spitze"] * 100
    if dd >= MAX_DRAWDOWN_STOP_PCT:
        state["pausiert"] = True
    return t


def verarbeite_bar(p, ts_bar, o, h, l, c):
    """Eine 15-Minuten-Kerze. Gibt Liste von (art, text) Ereignissen und ggf. Schlussgrund zurück."""
    d, ereignisse = p["richtung"], []
    ungef = (ts_bar - ts(p["fin_bis"])).total_seconds() / 86400
    fin = p["wert"] * p["rest"] * FINANZIERUNG_PCT_TAG / 100 * max(ungef, 0)
    p["netto"] -= fin
    p["fin"] += fin
    p["fin_bis"] = ts_bar.isoformat()
    ungünstig, günstig = (l, h) if d > 0 else (h, l)
    # 1) Stopp zuerst (vorsichtig: bei unklarer Reihenfolge in der Kerze gilt das Schlechtere)
    if (d > 0 and ungünstig <= p["stop"]) or (d < 0 and ungünstig >= p["stop"]):
        preis = min(p["stop"], o) if d > 0 else max(p["stop"], o)  # Lücke über den Stopp hinweg
        rest = p["rest"]
        fill(p, rest, preis, stopp=True)
        return ereignisse, ("Trailing-Stopp" if p["trail"] else "Stopp")
    # 2) Ziele der Reihe nach
    while p["ziele"] < len(ZIELE):
        z = p["ziel_preise"][p["ziele"]]
        if not ((d > 0 and günstig >= z) or (d < 0 and günstig <= z)):
            break
        fill(p, ANTEILE[p["ziele"]], z)
        p["ziele"] += 1
        ereignisse.append(("ziel", f"{p['ticker']}: Ziel {p['ziele']} von {len(ZIELE)} erreicht, "
                                   f"{int(ANTEILE[p['ziele'] - 1] * 100)} % verkauft."))
        if STOP_NACHZIEHEN and p["ziele"] == 1:
            p["stop"] = p["entry"]
        elif STOP_NACHZIEHEN and p["ziele"] == 2:
            p["stop"] = p["ziel_preise"][0]
        elif p["ziele"] == len(ZIELE):
            p["trail"] = True
            p["extrem"] = günstig
    # 3) Trailing für den Rest
    if p["trail"]:
        p["extrem"] = max(p["extrem"], günstig) if d > 0 else min(p["extrem"], günstig)
        neu = p["extrem"] * (1 - d * TRAIL_PREIS_PCT / 100)
        p["stop"] = max(p["stop"], neu) if d > 0 else min(p["stop"], neu)
    p["letzter_kurs"] = c
    # 4) Höchstdauer
    if (ts_bar - ts(p["start"])).days >= MAX_HALTE_TAGE:
        fill(p, p["rest"], c)
        return ereignisse, "Höchstdauer"
    return ereignisse, None


def lade_bars(ticker):
    import yfinance as yf
    d = yf.download(ticker, period="30d", interval="15m", auto_adjust=True, progress=False)
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = d.dropna(subset=["High", "Low", "Close"])
    d.index = d.index.tz_localize("UTC") if d.index.tz is None else d.index.tz_convert("UTC")
    return d


def aktualisiere(state, jetzt, lade_fn=None):
    lade_fn = lade_fn or lade_bars
    ereignisse, cache = [], {}
    for p in list(state["offen"]):
        if p["ticker"] not in cache:
            try:
                cache[p["ticker"]] = lade_fn(p["ticker"])
            except Exception as e:  # noqa: BLE001
                print("Kursdaten", p["ticker"], e)
                cache[p["ticker"]] = None
        df = cache[p["ticker"]]
        if df is None:
            continue
        ab = ts(p["naechste_bar"])
        for idx, row in df[df.index >= ab].iterrows():
            if idx + pd.Timedelta(minutes=BAR_MIN) > jetzt:
                break  # Kerze noch nicht fertig
            ev, grund = verarbeite_bar(p, idx, float(row["Open"]), float(row["High"]), float(row["Low"]), float(row["Close"]))
            ereignisse += [x[1] for x in ev]
            p["naechste_bar"] = (idx + pd.Timedelta(minutes=BAR_MIN)).isoformat()
            if grund:
                t = schliesse(p, state, idx + pd.Timedelta(minutes=BAR_MIN), grund)
                state["offen"].remove(p)
                sym = "✅" if t["netto"] > 0 else "❌"
                ereignisse.append(f"{sym} {p['ticker']} geschlossen ({grund}): {A.pz(t['netto'])} EUR "
                                  f"({A.pz(t['netto_pct'])} % vom Kapital), {t['ziele']} von {len(ZIELE)} Zielen erreicht.")
                break
    return ereignisse


def verarbeite_signale(state, jetzt):
    if state["start"] is None:
        state["start"] = (jetzt - pd.Timedelta(days=RUECKWIRKEND_TAGE)).isoformat()
    ereignisse = []
    for sig in lese_signale():
        if sig["id"] in state["gesehen"] or sig["zeit"] < ts(state["start"]) or sig["zeit"] > jetzt:
            continue
        state["gesehen"].append(sig["id"])
        p, text = oeffne(sig, state, jetzt)
        if p is None:
            state["abgelehnt"].append(dict(id=sig["id"], ticker=sig["ticker"], grund=text, zeit=sig["zeit"].isoformat()))
            print(f"{sig['ticker']}: nicht eröffnet, {text}")
        else:
            state["offen"].append(p)
            ereignisse.append(text)
    return ereignisse


# ---------------- Auswertung ----------------
def kennzahlen(trades, start=START_KAPITAL):
    n = len(trades)
    if not n:
        return None
    nets = np.array([t["netto"] for t in trades], float)
    gew, verl = nets[nets > 0], nets[nets <= 0]
    kurve = start + np.cumsum(nets)
    spitze = np.maximum.accumulate(np.r_[start, kurve])[1:]
    dd = float(((spitze - kurve) / spitze).max() * 100)
    pf = float(gew.sum() / abs(verl.sum())) if verl.sum() < 0 else float("inf")
    se = float(nets.std(ddof=1) / np.sqrt(n)) if n > 1 else float("inf")
    return dict(n=n, netto=float(nets.sum()), quote=len(gew) / n * 100, pf=pf, dd=dd,
                erwartung=float(nets.mean()), untere=float(nets.mean() - 1.96 * se),
                schnitt_gewinn=float(gew.mean()) if len(gew) else 0.0,
                schnitt_verlust=float(verl.mean()) if len(verl) else 0.0,
                kosten=float(sum(t["kosten"] + t["fin"] for t in trades)))


def freigabe(k):
    """Prüfliste für die Frage 'reif für mehr als Papier?'. Gibt (bestanden, Zeilen) zurück."""
    if not k:
        return False, ["⚪ Noch keine abgeschlossenen Trades."]
    pruef = [
        (k["n"] >= MIN_PAPER_TRADES, f"Mindestens {MIN_PAPER_TRADES} Trades (bisher {k['n']})"),
        (k["netto"] > 0, f"Netto im Plus (bisher {A.pz(k['netto'])} EUR)"),
        (k["pf"] >= 1.3, f"Profit Factor mindestens 1,3 (bisher {A.zahl(k['pf']) if k['pf'] != float('inf') else 'unendlich'})"),
        (k["untere"] > 0, "Durchschnitt pro Trade auch im ungünstigen Fall über 0 (sonst kann es Glück sein)"),
        (k["dd"] <= MAX_DRAWDOWN_STOP_PCT, f"Größter Rückgang höchstens {MAX_DRAWDOWN_STOP_PCT:.0f} % (bisher {A.zahl(k['dd'], 1)} %)"),
    ]
    return all(o for o, _ in pruef), [("✅ " if o else "❌ ") + t for o, t in pruef]


def baue_bericht(state):
    k = kennzahlen(state["trades"])
    z = ["PAPER-KONTO (Übung, kein echtes Geld)",
         f"Kapital: {A.zahl(state['kapital'])} EUR (Start {A.zahl(START_KAPITAL, 0)} EUR), "
         f"{HEBEL}x Hebel, {len(state['offen'])} Position(en) offen.", ""]
    if k:
        z += [f"{k['n']} abgeschlossene Trades: Netto {A.pz(k['netto'])} EUR nach allen Kosten.",
              f"Trefferquote {k['quote']:.0f} %, Profit Factor {A.zahl(k['pf']) if k['pf'] != float('inf') else 'unendlich'} "
              f"(über 1 heißt: Gewinne größer als Verluste).",
              f"Ø Gewinn {A.pz(k['schnitt_gewinn'])} EUR, Ø Verlust {A.pz(k['schnitt_verlust'])} EUR, "
              f"größter Rückgang {A.zahl(k['dd'], 1)} %.",
              f"Kosten und Haltegebühren insgesamt: {A.zahl(k['kosten'])} EUR.", ""]
    ok, zeilen = freigabe(k)
    z += ["FREIGABE-PRÜFUNG FÜR MEHR ALS PAPIER"] + zeilen
    z.append("Ergebnis: " + ("Alle Punkte erfüllt. Auch dann keine Garantie, nur mit Geld, dessen Verlust du verkraftest."
                             if ok else "NICHT bestanden. Echtgeld bleibt aus."))
    if state["pausiert"]:
        z.append(f"PAUSE: Der Rückgang vom Höchststand hat {MAX_DRAWDOWN_STOP_PCT:.0f} % erreicht. Es werden keine neuen Trades eröffnet.")
    if state["abgelehnt"]:
        z.append(f"Abgelehnte Signale (letzte 200): {len(state['abgelehnt'])}, häufigster Grund: "
                 + max({a['grund'] for a in state['abgelehnt']}, key=lambda g: sum(1 for a in state['abgelehnt'] if a['grund'] == g)))
    z += ["", "Keine Anlageberatung."]
    return "\n".join(z)


def zusammenfassung():
    """Kurzfassung für die Bot-Zentrale."""
    s = lade_state()
    k = kennzahlen(s["trades"])
    ok, zeilen = freigabe(k)
    return dict(kapital=round(s["kapital"], 2), start=START_KAPITAL, hebel=HEBEL, offen=len(s["offen"]), pausiert=s["pausiert"],
                n=k["n"] if k else 0, netto=round(k["netto"], 2) if k else 0, quote=round(k["quote"]) if k else 0,
                pf=round(k["pf"], 2) if k and k["pf"] != float("inf") else None, dd=round(k["dd"], 1) if k else 0,
                freigabe=ok, pruefung=zeilen, ziel_trades=MIN_PAPER_TRADES,
                kurve=[round(START_KAPITAL + float(v), 2) for v in np.cumsum([t["netto"] for t in s["trades"]])][-120:],
                trades=[{k_: t[k_] for k_ in ("ticker", "system", "richtung", "grund", "netto", "ende")} for t in s["trades"][::-1][:15]])


def main():
    jetzt = pd.Timestamp.now(tz="UTC")
    state = lade_state()
    if len(sys.argv) > 1 and sys.argv[1].lower() == "bericht":
        text = baue_bericht(state)
        print(text)
        A.sende_push("Paper-Konto Bericht", text)
        return
    ereignisse = aktualisiere(state, jetzt)
    ereignisse += verarbeite_signale(state, jetzt)
    ereignisse += aktualisiere(state, jetzt)  # frisch eröffnete Positionen sofort nachrechnen
    speichere_state(state)
    print(f"Kapital {state['kapital']:.2f} EUR, {len(state['offen'])} offen, {len(state['trades'])} abgeschlossen.")
    if ereignisse:
        text = "\n".join(f"- {e}" for e in ereignisse[:10]) + "\n\nPaper-Trading: kein echtes Geld. Keine Anlageberatung."
        print(text)
        A.sende_push("Paper: Ereignisse", text)


if __name__ == "__main__":
    main()
