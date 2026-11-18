#!/usr/bin/env python3
"""Auswertung der 15-Min-Meldungen aus radar_state.json.
Aufruf: python3 auswertung.py            (Kosten aus dem Radar)
        python3 auswertung.py 0.4        (eigene Kosten in Prozent testen)"""
import json
import math
import sys

import pandas as pd

KOSTEN = float(sys.argv[1].replace(",", ".")) if len(sys.argv) > 1 else None


def wilson(k, n, z=1.96):
    """95-%-Bereich fuer die Trefferquote."""
    if n == 0:
        return 0.0, 0.0
    p, d = k / n, 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


try:
    with open("radar_state.json", encoding="utf-8") as f:
        erg = json.load(f).get("ergebnisse", [])
except FileNotFoundError:
    sys.exit("radar_state.json fehlt. Erst 'git pull' machen.")
if not erg:
    sys.exit("Noch keine ausgewerteten Meldungen.")

df = pd.DataFrame(erg)
roh = len(df)
if "unvollstaendig" in df:
    df = df[~df["unvollstaendig"].fillna(False)]
if KOSTEN is not None:
    df["netto"] = df["pct"] - KOSTEN
df["zeit"] = pd.to_datetime(df["zeit_de"])
df["stunde"] = df["zeit"].dt.hour
df["markt"] = df["ticker"].str.endswith(".DE").map({True: "DE", False: "US"})
df["treffer"] = df["ergebnis"] == "ZIEL"
if "typ" not in df:
    df["typ"] = "NORMAL"
df["typ"] = df["typ"].fillna("NORMAL")

n = len(df)
k = int(df["treffer"].sum())
lo, hi = wilson(k, n)
m = df["netto"].mean()
se = df["netto"].std() / math.sqrt(n) if n > 1 else float("nan")
kosten = KOSTEN if KOSTEN is not None else float((df["pct"] - df["netto"]).mean())
z_, s_ = df["ziel_pct"].mean(), -df["stop_pct"].mean()
be = (s_ + kosten) / (z_ + s_) * 100

print(f"MELDUNGEN: {n} auswertbar (von {roh}), Kosten {kosten:.2f} % pro Trade")
print(f"Ziel {k} | Stopp {(df.ergebnis == 'STOPP').sum()} | nichts {(df.ergebnis == 'NICHTS').sum()}")
print(f"Trefferquote {k / n * 100:.0f} % (95-%-Bereich {lo * 100:.0f} bis {hi * 100:.0f} %)")
print(f"Break-even ca. {be:.0f} % (Ziel +{z_:.2f} %, Stopp -{s_:.2f} %)")
print(f"Schnitt nach Kosten: {m:+.3f} % pro Meldung (Fehler +-{1.96 * se:.3f})")

if n < 100:
    urteil = f"ZU WENIG DATEN ({n}/100). Noch kein Urteil moeglich."
elif m - 1.96 * se > 0:
    urteil = "Statistisch im Plus nach Kosten. Weiter beobachten, dann klein testen."
elif m + 1.96 * se < 0:
    urteil = "Statistisch im Minus nach Kosten. So nicht handeln, Regeln aendern."
else:
    urteil = "Unklar: Plus oder Minus liegen noch im Zufallsbereich."
print("\nURTEIL:", urteil)


def gruppe(titel, spalte, bins=None, labels=None):
    g = pd.cut(df[spalte], bins, labels=labels, right=False) if bins else df[spalte]
    print(f"\n{titel}")
    for name, d in df.groupby(g, observed=True):
        if len(d) >= 5:
            print(f"  {str(name):>8}: n={len(d):3d}  Ziel {d.treffer.mean() * 100:3.0f} %  netto {d.netto.mean():+.2f} %")


gruppe("PUNKTE", "score", [60, 70, 80, 101], ["60-69", "70-79", "80+"])
gruppe("VOLUMEN", "relvol", [0, 1, 2, 99], ["<1x", "1-2x", "2x+"])
gruppe("UHRZEIT (DE)", "stunde")
gruppe("TYP", "typ")
gruppe("MARKT", "markt")
print("\nHinweis: Gruppen mit wenig Faellen sind Zufall. Erst ab ca. 100 Faellen insgesamt")
print("und 30 pro Gruppe etwas daraus ableiten, sonst jagst du Rauschen.")
