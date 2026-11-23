#!/usr/bin/env python3
"""
auswertung.py - das Wochenfazit.

Fasst die Protokolle aller Systeme zusammen und beantwortet die eine Frage:
"Hätte man mit diesen Meldungen nach Kosten Geld verdient - oder war es Glück?"

  python auswertung.py        Auswerten, anzeigen und aufs Handy schicken
  python auswertung.py dry    Nur anzeigen (kein Handy)

Gelesen werden:
  radar_state.json     Radar: Ergebnisse der 15-Minuten-Meldungen (steht schon dort drin)
  muster_log.csv       Muster: jeder erkannte Ausbruch (gemeldet oder nicht)
  signals_log.csv      Wächter: Tages-Signale "LONG"
  catalyst_log.csv     Katalysator-Score: täglicher Punktestand

Ergebnis-Speicher: muster_ergebnisse.json (damit alte Fälle nicht verloren gehen,
denn Yahoo liefert 15-Minuten-Kurse nur für die letzten 60 Tage).

Die Ampel ist absichtlich streng:
  ⚪ zu früh    unter 30 Fälle: reiner Zufall, keine Aussage möglich
  🔴 Minus      im Schnitt nach Kosten verloren
  🟡 unsicher   im Plus, aber so knapp, dass es Glück sein kann
  🟢 Plus       ab 100 Fällen klar im Plus (trotzdem keine Garantie für die Zukunft)

Keine Anlageberatung.
"""
import csv
import datetime as dt
import json
import os
import sys
import urllib.request

import numpy as np
import pandas as pd

COST_PCT = float(os.environ.get("COST_PCT", "0.7").replace(",", "."))  # Kosten pro Kauf+Verkauf in %
MIN_N, GUT_N = 30, 100
HOCH = 4                      # Katalysator-Score ab diesem Wert gilt als "hoch"
HORIZON = 5                   # Katalysator: Handelstage bis zur Auswertung
MUSTER_CACHE = "muster_ergebnisse.json"


# ---------------- Hilfen ----------------
def zahl(x, nd=2):
    return f"{x:.{nd}f}".replace(".", ",")


def pz(x, nd=2):
    return f"{x:+.{nd}f}".replace(".", ",")


def lese_csv(pfad):
    if not os.path.exists(pfad):
        return []
    with open(pfad, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def lese_json(pfad):
    try:
        with open(pfad, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def urteil(renditen):
    """Ampel für eine Liste von Ergebnissen in Prozent NACH Kosten."""
    n = len(renditen)
    if n < MIN_N:
        return "⚪", f"Zu früh: Erst {n} Fälle. Unter {MIN_N} ist alles Zufall, noch keine Aussage möglich."
    r = np.asarray(renditen, float)
    mean = float(r.mean())
    se = float(r.std(ddof=1) / np.sqrt(n)) if n > 1 else float("inf")
    if mean <= 0:
        return "🔴", f"Im Minus: Im Schnitt {pz(mean)} % pro Fall nach Kosten. Bisher hat sich das nicht gelohnt."
    if mean - 1.96 * se > 0 and n >= GUT_N:
        return "🟢", (f"Im Plus: Im Schnitt {pz(mean)} % pro Fall nach Kosten, und das ist bei {n} Fällen "
                      "wahrscheinlich kein Zufall. Eine Garantie für die Zukunft ist es nicht.")
    return "🟡", (f"Knapp im Plus ({pz(mean)} % pro Fall nach Kosten), aber noch nicht sicher: "
                  "Bei so wenigen oder so streuenden Fällen kann das Glück sein.")


def block(titel, untertitel, renditen, extra=None):
    ampel, satz = urteil(renditen)
    z = [f"{ampel} {titel}", f"({untertitel})"]
    if extra:
        z.append(extra)
    z.append(satz)
    return "\n".join(z), ampel


# ---------------- Radar ----------------
def auswerten_radar():
    erg = lese_json("radar_state.json").get("ergebnisse", [])
    if not erg:
        return "⚪ RADAR\nNoch keine ausgewertete Meldung.", "⚪"
    netto = [float(e["netto"]) for e in erg]
    ziele = sum(1 for e in erg if e["ergebnis"] == "ZIEL")
    stopps = sum(1 for e in erg if e["ergebnis"] == "STOPP")
    extra = (f"{len(erg)} Meldungen: {ziele}x Gewinnziel, {stopps}x Verlustgrenze, "
             f"{len(erg) - ziele - stopps}x weder noch.")
    return block("RADAR", "Aktien-Auswahl mit 15-Minuten-Fenster", netto, extra)


# ---------------- Muster ----------------
def _zerlege(daten, tickers):
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


def lade_15m(tickers, chunk=40):
    import yfinance as yf
    out = {}
    for i in range(0, len(tickers), chunk):
        teil = tickers[i:i + chunk]
        try:
            d = yf.download(teil, period="60d", interval="15m", group_by="ticker",
                            auto_adjust=True, progress=False, threads=True)
            out.update(_zerlege(d, teil))
        except Exception as e:  # noqa: BLE001
            print("Download-Fehler:", e)
    return out


def bewerte_kerze(d, k, ziel, stopp, ziel_pct, stopp_pct, hi, lo, cl):
    """Nächste 15-Min-Kerze nach dem Ausbruch. Der Stopp wird zuerst geprüft (vorsichtig)."""
    if (d > 0 and lo <= stopp) or (d < 0 and hi >= stopp):
        return "STOPP", -stopp_pct - COST_PCT
    if (d > 0 and hi >= ziel) or (d < 0 and lo <= ziel):
        return "ZIEL", ziel_pct - COST_PCT
    return "NICHTS", d * (cl / k - 1) * 100 - COST_PCT


def auswerten_muster(laden_fn=None, jetzt=None):
    rows = lese_csv("muster_log.csv")
    if not rows:
        return "⚪ MUSTER\nNoch keine erkannten Ausbrüche im Protokoll.", "⚪"
    jetzt = jetzt or pd.Timestamp.now(tz="UTC")
    cache = lese_json(MUSTER_CACHE)
    offen = [r for r in rows if f"{r['ticker']}|{r['muster']}|{r['kerze_utc']}" not in cache]
    daten = {}
    if offen:
        try:
            daten = (laden_fn or lade_15m)(sorted({r["ticker"] for r in offen}))
        except Exception as e:  # noqa: BLE001
            print("Kursdaten nicht ladbar:", e)
    for r in offen:
        df = daten.get(r["ticker"])
        if df is None or df.empty:
            continue
        df = df.dropna(subset=["High", "Low", "Close"])
        idx = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
        df = df.set_axis(idx)
        nxt = pd.Timestamp(r["kerze_utc"], tz="UTC") + pd.Timedelta(minutes=15)
        if nxt + pd.Timedelta(minutes=15) > jetzt or nxt not in df.index:
            continue  # Kerze noch nicht fertig oder nicht (mehr) vorhanden
        k = df.loc[nxt]
        erg, net = bewerte_kerze(int(r["richtung"]), float(r["kurs"]), float(r["ziel"]), float(r["stopp"]),
                                 float(r["ziel_pct"]), float(r["stopp_pct"]),
                                 float(k["High"]), float(k["Low"]), float(k["Close"]))
        cache[f"{r['ticker']}|{r['muster']}|{r['kerze_utc']}"] = [erg, round(net, 4)]
    if cache:
        with open(MUSTER_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=0)
    alle, gemeldet = [], []
    for r in rows:
        e = cache.get(f"{r['ticker']}|{r['muster']}|{r['kerze_utc']}")
        if e is None:
            continue
        alle.append(e[1])
        if str(r.get("gemeldet")) == "1":
            gemeldet.append(e[1])
    pending = len(rows) - len(alle)
    if not alle:
        return f"⚪ MUSTER\n{len(rows)} Ausbrüche notiert, noch keiner auswertbar.", "⚪"
    extra = f"{len(alle)} erkannte Ausbrüche ausgewertet ({len(gemeldet)} davon wurden gemeldet, {pending} noch offen)."
    text, ampel = block("MUSTER", "Chartfiguren auf 15-Minuten-Kerzen, alle erkannten Ausbrüche", alle, extra)
    if gemeldet:
        a2, s2 = urteil(gemeldet)
        text += f"\nNur die gemeldeten: {a2} {s2}"
    return text, ampel


# ---------------- Wächter (Tages-Signale) ----------------
def auswerten_waechter(tages_fn=None):
    rows = [r for r in lese_csv("signals_log.csv") if r.get("type") == "LONG"]
    if not rows:
        return "⚪ WÄCHTER\nNoch keine Tages-Signale im Protokoll.", "⚪"
    import scanner
    data = (tages_fn or scanner.load)(sorted({r["ticker"] for r in rows}))
    sig, base = [], []
    for r in rows:
        df = data.get(r["ticker"])
        if df is None:
            continue
        daten = [x.date() for x in df.index]
        tag = dt.datetime.strptime(r["bar"], "%Y-%m-%d").date()
        if tag not in daten:
            continue
        res = scanner.outcome(df.reset_index(drop=True), daten.index(tag))
        if res is not None:
            sig.append(res * 100 - COST_PCT)
    for df in data.values():
        d = df.reset_index(drop=True)
        for i in range(60, len(d) - 1):
            res = scanner.outcome(d, i)
            if res is not None:
                base.append(res * 100 - COST_PCT)
    if not sig:
        return f"⚪ WÄCHTER\n{len(rows)} Signale notiert, noch keines auswertbar (5 Handelstage Wartezeit).", "⚪"
    extra = f"{len(sig)} Signale ausgewertet. Zum Vergleich ein beliebiger Tag derselben Aktien: {pz(float(np.mean(base)))} % ({len(base)} Fälle)." if base else None
    text, ampel = block("WÄCHTER", "Tages-Ausbruch mit hohem Umsatz, 5 Handelstage Haltedauer", sig, extra)
    if base and float(np.mean(sig)) <= float(np.mean(base)):
        text += "\nDas Signal ist nicht besser als ein beliebiger Tag: Es bringt bisher keinen Vorteil."
    return text, ampel


# ---------------- Katalysator-Score ----------------
def auswerten_katalysator(tages_fn=None):
    rows = lese_csv("catalyst_log.csv")
    if not rows:
        return "⚪ KATALYSATOR-SCORE\nNoch kein Protokoll.", "⚪"
    import scanner
    data = (tages_fn or scanner.load)(sorted({r["ticker"] for r in rows}))
    hoch, alle = [], []
    for r in rows:
        df = data.get(r["ticker"])
        if df is None:
            continue
        tag = dt.datetime.strptime(r["date"], "%Y-%m-%d").date()
        danach = df[df.index.date > tag]
        if len(danach) < HORIZON:
            continue
        ret = (float(danach["Close"].iloc[HORIZON - 1]) / float(r["price"]) - 1) * 100 - COST_PCT
        alle.append(ret)
        if int(r["score"]) >= HOCH:
            hoch.append(ret)
    if not alle:
        return f"⚪ KATALYSATOR-SCORE\n{len(rows)} Einträge, noch keiner auswertbar (5 Handelstage Wartezeit).", "⚪"
    extra = (f"{len(alle)} Einträge ausgewertet. Hoher Score (ab {HOCH}): {len(hoch)} Fälle, "
             f"Schnitt {pz(float(np.mean(hoch)))} %. Alle: {pz(float(np.mean(alle)))} %." if hoch else
             f"{len(alle)} Einträge ausgewertet, noch kein hoher Score darunter.")
    text, ampel = block("KATALYSATOR-SCORE", "hoher Punktestand, 5 Handelstage Haltedauer", hoch or [], extra)
    return text, ampel


# ---------------- Gesamt ----------------
def baue_bericht(teile):
    heute = dt.datetime.now().strftime("%d.%m.%Y")
    ampeln = [a for _, a in teile]
    z = [f"WOCHENFAZIT vom {heute}",
         f"Alle Zahlen nach Kosten (ca. {zahl(COST_PCT)} % pro Kauf und Verkauf).", ""]
    for text, _ in teile:
        z += [text, ""]
    z.append("FAZIT")
    if "🟢" in ampeln:
        z.append("Mindestens ein System ist über 100 Fälle klar im Plus. Auch dann gilt: Vergangenheit ist keine "
                 "Garantie. Wenn überhaupt, dann nur mit kleinem Einsatz, den du verkraften kannst, und ohne Hebel.")
    elif "🟡" in ampeln:
        z.append("Mindestens ein System ist knapp im Plus, aber noch nicht sicher. Weiter sammeln, "
                 "nichts mit echtem Geld ausprobieren, bis die Zahlen klarer sind.")
    elif "🔴" in ampeln:
        z.append("Bisher verliert mindestens ein System nach Kosten. Mit echtem Geld nicht nachmachen.")
    else:
        z.append("Es gibt noch zu wenige Fälle für eine Aussage. Einfach weiterlaufen lassen und in ein paar Wochen "
                 "wieder schauen.")
    z.append("")
    z.append("Keine Anlageberatung. Die Auswertung zeigt nur, wie die Regeln in der Vergangenheit abgeschnitten haben.")
    text = "\n".join(z)
    if len(text.encode("utf-8")) > 3800:
        text = text.encode("utf-8")[:3750].decode("utf-8", "ignore") + "\n[gekürzt]"
    return text


def sende_push(titel, text):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC nicht gesetzt - keine Meldung gesendet.")
        return False
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=text.encode("utf-8"), method="POST",
        headers={"Title": titel.encode("ascii", "ignore").decode("ascii"), "Tags": "bar_chart"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        print("Push-Fehler:", e)
        return False


def main():
    nur_anzeigen = len(sys.argv) > 1 and sys.argv[1].lower() == "dry"
    teile = []
    for fn in (auswerten_radar, auswerten_muster, auswerten_waechter, auswerten_katalysator):
        try:
            teile.append(fn())
        except Exception as e:  # noqa: BLE001
            print(f"{fn.__name__}: Fehler {e}")
            teile.append((f"⚪ {fn.__name__.replace('auswerten_', '').upper()}\nAuswertung fehlgeschlagen ({e}).", "⚪"))
    text = baue_bericht(teile)
    print(text)
    if not nur_anzeigen:
        sende_push("Wochenfazit Bot", text)


if __name__ == "__main__":
    main()
