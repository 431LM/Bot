#!/usr/bin/env python3
"""
muster.py - erkennt Chartmuster auf 15-Minuten-Kerzen und meldet den AUSBRUCH
per ntfy MIT Chartbild. Die Erfolgsquote jedes Musters kommt aus dem eigenen
Backtest (muster_stats.json), nicht aus Lehrbuchwerten.

  python3 muster.py dry        Scan ansehen (keine Push)
  python3 muster.py test       Demo-Push mit Chartbild (prueft ntfy + Bild)
  python3 muster.py run        Scan + Push (macht GitHub Actions)
  python3 muster.py backtest   Quote je Muster aus 60 Tagen -> muster_stats.json
Kein Finanzrat.
"""
import datetime as dt
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.signal import find_peaks

TZ_DE, TZ_US = ZoneInfo("Europe/Berlin"), ZoneInfo("America/New_York")
MIN_Q = 70            # Mindest-Formtreue in % (0-100)
PROM, TOL, EXT = 1.5, 1.5, 3.0   # Pivot-Hoehe, Toleranz, max. Abstand zur Linie (in typ. Kerzenspannen)
FENSTER, MAX_AGE = 120, 20       # Kerzen fuer die Erkennung, max. Alter der Daten in Min
MAX_PER_DAY = 8       # hoechstens so viele Meldungen pro Tag
COST_PCT = float(os.environ.get("COST_PCT", "0.7").replace(",", "."))   # Gebuehr + Spread in % (Platzhalter!)
ZIEL_X, STOP_X = 2.0, 1.0        # Ziel/Stopp in typischen 15-Min-Spannen
NUR_POSITIV, MIN_N = True, 30    # Muster mit Backtest-Minus (ab 30 Faellen) nicht melden
KRYPTO = "BTC-USD ETH-USD SOL-USD XRP-USD LTC-USD DOGE-USD ADA-USD LINK-USD AVAX-USD"
STATE, STATS, UNIVERSE_FILE = "muster_state.json", "muster_stats.json", "universe.txt"
SPIEGEL = {"Double top": "Double bottom", "Triple top": "Triple bottom",
           "Head and Shoulders": "Inverse Head and Shoulders", "Descending triangle": "Ascending triangle",
           "Rising wedge": "Falling wedge", "Bear flag": "Bull flag", "Bear pennant": "Bull pennant",
           "Symmetrical triangle (Bruch unten)": "Symmetrical triangle (Bruch oben)"}


# ---------------- Muster-Erkennung ----------------
def _piv(a, rng, tief=False):
    idx, _ = find_peaks(-a if tief else a, prominence=PROM * rng, distance=2)
    return [(int(i), float(a[i])) for i in idx]


def _fit(pts):
    x = np.array([p[0] for p in pts], float)
    y = np.array([p[1] for p in pts], float)
    a, b = np.polyfit(x, y, 1)
    return float(a), float(b), float(np.max(np.abs(a * x + b - y)))


def _c01(x):
    return float(min(1.0, max(0.0, x)))


def _bricht(C, a, b, last, rng):
    """Schlusskurs bricht JETZT von oben durch die Linie y = a*x + b (Richtung nach unten)."""
    l1, l0 = a * last + b, a * (last - 1) + b
    return C[last] < l1 and C[last - 1] >= l0 - 0.5 * rng and (l1 - C[last]) <= EXT * rng


def detect_bearish(H, L, C, rng):
    """Muster mit Ausbruch nach UNTEN beim letzten Kurs. Fuer Ausbrueche nach oben werden die Kurse gespiegelt."""
    n = len(C)
    last = n - 1
    out = []
    P, T = _piv(H, rng), _piv(L, rng, tief=True)
    tol = TOL * rng

    if len(P) >= 3:                                              # Kopf-Schulter-Kopf
        (i1, h1), (i2, h2), (i3, h3) = P[-3:]
        if i2 - i1 >= 3 and i3 - i2 >= 3 and i3 - i1 <= 80 and last - i3 <= 30 and h2 >= max(h1, h3) + rng \
                and abs(h1 - h3) <= 2.5 * rng:
            j1 = i1 + int(np.argmin(L[i1:i2 + 1]))
            j2 = i2 + int(np.argmin(L[i2:i3 + 1]))
            a = (L[j2] - L[j1]) / (j2 - j1)
            b = float(L[j1] - a * j1)
            if abs(a * (i3 - i1)) <= 4 * rng and _bricht(C, a, b, last, rng):
                q = 100 * (0.4 * _c01(1 - abs(h1 - h3) / (2.5 * rng)) + 0.4 * _c01((h2 - max(h1, h3)) / (3 * rng))
                           + 0.2 * _c01(1 - abs(a * (i3 - i1)) / (4 * rng)))
                out.append(dict(name="Head and Shoulders", q=q, lines=[(a, b, j1, last)], pts=[P[-3], P[-2], P[-1]]))
    hs = bool(out)

    if len(P) >= 3 and not hs:                                   # Triple top
        pk = P[-3:]
        hv = [v for _, v in pk]
        if max(hv) - min(hv) <= 1.5 * tol and pk[1][0] - pk[0][0] >= 4 and pk[2][0] - pk[1][0] >= 4 \
                and pk[2][0] - pk[0][0] <= 90 and last - pk[2][0] <= 40:
            neck = float((L[pk[0][0]:pk[1][0] + 1].min() + L[pk[1][0]:pk[2][0] + 1].min()) / 2)
            depth = min(hv) - neck
            if depth >= 2.5 * rng and _bricht(C, 0, neck, last, rng):
                q = 100 * (0.55 * _c01(1 - (max(hv) - min(hv)) / (1.5 * tol)) + 0.45 * _c01(depth / (6 * rng)))
                out.append(dict(name="Triple top", q=q, lines=[(0, neck, pk[0][0], last)], pts=pk))
    tt = any(m["name"] == "Triple top" for m in out)

    if len(P) >= 2 and not hs and not tt:                        # Double top
        (i1, h1), (i2, h2) = P[-2:]
        if 4 <= i2 - i1 <= 60 and last - i2 <= 40 and abs(h1 - h2) <= tol:
            neck = float(L[i1:i2 + 1].min())
            depth = min(h1, h2) - neck
            if depth >= 2.5 * rng and H[i2:].max() <= max(h1, h2) + tol and _bricht(C, 0, neck, last, rng):
                q = 100 * (0.55 * _c01(1 - abs(h1 - h2) / tol) + 0.45 * _c01(depth / (6 * rng)))
                out.append(dict(name="Double top", q=q, lines=[(0, neck, i1, last)], pts=[P[-2], P[-1]]))

    Pw = [p for p in P if p[0] >= last - 80][-3:]                # Dreiecke und Keile
    Tw = [p for p in T if p[0] >= last - 80][-3:]
    if len(Pw) >= 3 and len(Tw) >= 2 and last - Pw[-1][0] <= 30 and last - Tw[-1][0] <= 30:
        x0 = min(Pw[0][0], Tw[0][0])
        span = last - x0
        if span >= 12:
            au, bu, ru = _fit(Pw)
            al, bl, rl = _fit(Tw)
            du, dl = au * span, al * span
            w0, w1 = (au * x0 + bu) - (al * x0 + bl), (au * last + bu) - (al * last + bl)
            tr = 1.2 * tol
            tv = [v for _, v in Tw]
            name, lo_line, rl_ = None, (al, bl), rl
            if len(Tw) >= 2 and max(tv) - min(tv) <= tr and du <= -2 * rng:
                m_ = float(np.mean(tv))
                v0, v1 = (au * x0 + bu) - m_, (au * last + bu) - m_
                if v0 > 0 and 0 < v1 <= 0.8 * v0:
                    name, lo_line, rl_ = "Descending triangle", (0.0, m_), max(tv) - m_
            elif du >= 2 * rng and dl >= 2 * rng and 0 < w1 <= 0.8 * w0:
                name = "Rising wedge"
            elif du <= -2 * rng and dl >= 2 * rng and 0 < w1 <= 0.75 * w0:
                name = "Symmetrical triangle (Bruch unten)"
            if name and max(ru, rl_) <= tr and _bricht(C, lo_line[0], lo_line[1], last, rng):
                q = 100 * (0.6 * _c01(1 - max(ru, rl_) / tr) + 0.4 * _c01((abs(du) + abs(dl)) / (8 * rng)))
                out.append(dict(name=name, q=q, lines=[(au, bu, x0, last), (lo_line[0], lo_line[1], x0, last)],
                                pts=Pw + Tw))

    a0, b0 = max(8, last - 40), last - 4                         # Flaggen und Wimpel nach Kursfall
    if b0 > a0:
        il = a0 + int(np.argmin(L[a0:b0]))
        ip = max(0, il - 15) + int(np.argmax(H[max(0, il - 15):il + 1]))
        drop = H[ip] - L[il]
        Pf = [p for p in P if il < p[0] < last][-3:]
        Tf = [p for p in T if il < p[0] < last][-3:]
        if drop >= 6 * rng and il - ip >= 2 and last - il >= 6 and len(Pf) >= 2 and len(Tf) >= 2 \
                and max(p[1] for p in Pf) - L[il] <= 0.65 * drop:
            au, bu, ru = _fit(Pf)
            al, bl, rl = _fit(Tf)
            m_ = last - il
            du, dl = au * m_, al * m_
            x0 = il + 1
            w0, w1 = (au * x0 + bu) - (al * x0 + bl), (au * last + bu) - (al * last + bl)
            name, par = None, 0.0
            if abs(du - dl) <= 2.5 * rng and du >= -rng and dl >= -rng and w1 > 0:
                name, par = "Bear flag", _c01(1 - abs(du - dl) / (2.5 * rng))
            elif du <= -rng and dl >= rng and 0 < w1 <= 0.6 * w0:
                name, par = "Bear pennant", _c01(1 - w1 / w0)
            res = max(ru, rl)
            if name and res <= 2.5 * rng and _bricht(C, al, bl, last, rng):
                q = 100 * (0.4 * _c01(drop / (12 * rng)) + 0.3 * _c01(1 - res / (2.5 * rng)) + 0.3 * par)
                out.append(dict(name=name, q=q, pts=[(ip, float(H[ip])), (il, float(L[il]))] + Pf + Tf,
                                lines=[(au, bu, x0, last), (al, bl, x0, last)]))
    return out


def beste(w):
    """Bestes Muster mit Ausbruch auf der letzten Kerze von w oder None."""
    H, L, C = (w[c].to_numpy(float) for c in ("High", "Low", "Close"))
    if len(C) < 40:
        return None
    rng = float(np.median(H - L))
    if not rng > 0:
        return None
    alle = []
    for m in detect_bearish(H, L, C, rng):
        m["d"] = -1
        alle.append(m)
    for m in detect_bearish(-L, -H, -C, rng):
        m["name"] = SPIEGEL[m["name"]]
        m["lines"] = [(-a, -b, x0, x1) for a, b, x0, x1 in m["lines"]]
        m["pts"] = [(i, -v) for i, v in m["pts"]]
        m["d"] = 1
        alle.append(m)
    alle = [m for m in alle if m["q"] >= MIN_Q]
    return max(alle, key=lambda m: m["q"]) if alle else None


# ---------------- Plan, Daten, Markt ----------------
def spanne(w):
    return float(((w["High"] - w["Low"]) / w["Close"]).tail(60).median() * 100)


def plan(k, sp, d):
    sp = max(sp, 0.05)
    return {"ziel": k * (1 + d * ZIEL_X * sp / 100), "stop": k * (1 - d * STOP_X * sp / 100),
            "limit": k * (1 + d * 0.5 * ZIEL_X * sp / 100), "ziel_pct": ZIEL_X * sp, "stop_pct": STOP_X * sp}


def bewerte_kerze(d, k, pl, hi, lo, cl):
    """Naechste 15-Min-Kerze: Stopp wird zuerst geprueft (vorsichtig). Gibt (Ergebnis, Prozent nach Kosten)."""
    if (d > 0 and lo <= pl["stop"]) or (d < 0 and hi >= pl["stop"]):
        return "STOPP", -pl["stop_pct"] - COST_PCT
    if (d > 0 and hi >= pl["ziel"]) or (d < 0 and lo <= pl["ziel"]):
        return "ZIEL", pl["ziel_pct"] - COST_PCT
    return "NICHTS", d * (cl / k - 1) * 100 - COST_PCT


def zerlege(daten, tickers):
    out = {}
    if daten is None or len(daten) == 0:
        return out
    if isinstance(daten.columns, pd.MultiIndex):
        e0 = list(daten.columns.get_level_values(0).unique())
        for t in tickers:
            if t in e0:
                df = daten[t].dropna(how="all")
                if not df.empty:
                    out[t] = df
    elif len(tickers) == 1:
        out[tickers[0]] = daten.dropna(how="all")
    return out


def lade(tickers, period="5d", chunk=40):
    import yfinance as yf
    out = {}
    for i in range(0, len(tickers), chunk):
        teil = tickers[i:i + chunk]
        try:
            d = yf.download(teil, period=period, interval="15m", group_by="ticker",
                            auto_adjust=True, progress=False, threads=True)
            out.update(zerlege(d, teil))
        except Exception as e:  # noqa: BLE001
            print("Download-Fehler:", e)
        time.sleep(0.5)
    return out


def vollstaendig(df, jetzt):
    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    if df.empty:
        return df
    if df.index.tz is None:
        df = df.tz_localize("UTC")
    return df[df.index + pd.Timedelta(minutes=15) <= jetzt]


def universum():
    t = []
    if os.path.exists(UNIVERSE_FILE):
        with open(UNIVERSE_FILE, encoding="utf-8") as f:
            t = [z.strip().upper() for z in f if z.strip() and not z.startswith("#")]
    return sorted(set(t + KRYPTO.split()))


def offen(t, jetzt):
    if t.endswith("-USD"):
        return True                      # Krypto: rund um die Uhr
    tz, von, bis = (TZ_DE, dt.time(9, 0), dt.time(17, 50)) if t.endswith(".DE") else (TZ_US, dt.time(9, 30), dt.time(16, 20))
    lok = jetzt.astimezone(tz)
    return lok.weekday() < 5 and von <= lok.time() <= bis


# ---------------- Chartbild und Push ----------------
def zeichne(w, ticker, m, pl, jetzt):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    d = w.tail(70)
    off = len(w) - len(d)
    fig, ax = plt.subplots(figsize=(9, 5), dpi=110)
    for j, (o, h, l, c) in enumerate(zip(d["Open"], d["High"], d["Low"], d["Close"])):
        col = "#1a9850" if c >= o else "#d73027"
        ax.plot([j, j], [l, h], color=col, lw=1)
        ax.add_patch(Rectangle((j - 0.35, min(o, c)), 0.7, max(abs(c - o), 1e-9), color=col))
    for a, b, x0, x1 in m["lines"]:
        xs = np.array([max(x0, off), x1], float)
        ax.plot(xs - off, a * xs + b, color="#2166ac", lw=2.2)
    for i, v in m["pts"]:
        if i >= off:
            ax.plot(i - off, v, "o", mfc="none", mec="#2166ac", ms=13, mew=2)
    ax.axhline(pl["ziel"], color="#1a9850", ls="--", lw=1)
    ax.axhline(pl["stop"], color="#d73027", ls="--", lw=1)
    ax.text(0, pl["ziel"], " Ziel", color="#1a9850", fontsize=9, va="bottom")
    ax.text(0, pl["stop"], " Stopp", color="#d73027", fontsize=9, va="bottom")
    n = len(d) - 1
    ax.annotate("Ausbruch", (n, d["Close"].iloc[-1]), xytext=(n - 12, d["Close"].iloc[-1] + (1 if m["d"] < 0 else -1) * 0.0),
                fontsize=10, color="#2166ac", arrowprops=dict(arrowstyle="->", color="#2166ac"))
    lo, hi = min(d["Low"].min(), pl["stop"], pl["ziel"]), max(d["High"].max(), pl["stop"], pl["ziel"])
    pad = (hi - lo) * 0.06
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(-1, n + 3)
    ticks = list(range(0, len(d), 10))
    ax.set_xticks(ticks)
    ax.set_xticklabels([d.index[i].tz_convert(TZ_DE).strftime("%d.%m %H:%M") for i in ticks], fontsize=8)
    ax.grid(alpha=.25)
    ax.set_title(f"{ticker} 15 Min | {m['name']} | {'Long' if m['d'] > 0 else 'Short'} | Formtreue {m['q']:.0f} %", fontsize=11)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def sende_push(titel, text, bild=None, prio="default"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC nicht gesetzt - keine Push gesendet.")
        return False
    if bild:
        q = urllib.parse.urlencode({"title": titel, "message": text, "priority": prio,
                                    "tags": "chart_with_upwards_trend", "filename": "chart.png"})
        req = urllib.request.Request(f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh')}/{topic}?{q}", data=bild, method="PUT",
                                     headers={"Content-Type": "image/png"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                if 200 <= r.status < 300:
                    print("Push mit Chartbild gesendet.")
                    return True
        except Exception as e:  # noqa: BLE001
            print("Bild-Push fehlgeschlagen, sende ohne Bild:", e)
    q = urllib.parse.urlencode({"title": titel, "priority": prio, "tags": "chart_with_upwards_trend"})
    req = urllib.request.Request(f"{os.environ.get('NTFY_SERVER', 'https://ntfy.sh')}/{topic}?{q}", data=text.encode("utf-8"), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        print("Push-Fehler:", e)
        return False


def baue_text(t, m, pl, k, alter, s, jetzt):
    w = "USD" if t.endswith("-USD") else ("EUR" if t.endswith(".DE") else "USD")
    bis = (jetzt + dt.timedelta(minutes=15)).astimezone(TZ_DE).strftime("%H:%M")
    richt = "LONG (Kurs steigt)" if m["d"] > 0 else "SHORT (Kurs faellt)"
    z = [f"MUSTER-AUSBRUCH: {m['name']} -> {richt}",
         f"{t} Kurs {k:.2f} {w}",
         f"Formtreue {m['q']:.0f} % (wie genau das Muster passt, KEINE Gewinnchance).", ""]
    if s and s["n"] >= 10:
        z.append(f"Backtest {m['name']} (n={s['n']}): Ziel in 15 Min {s['ziel_pct']:.0f} %, "
                 f"im Plus nach Kosten {s['plus_pct']:.0f} %, Schnitt {s['netto']:+.2f} % pro Trade.")
    else:
        z.append("Backtest: noch keine ausreichenden Daten, Muster ist UNGEPRUEFT.")
    z += ["", f"JETZT 15 MIN BEOBACHTEN bis {bis} Uhr (DE). Einstieg nur bis Kurs {pl['limit']:.2f}, sonst: Chance verpasst.",
          f"Ziel {pl['ziel']:.2f} ({'+' if m['d'] > 0 else '-'}{pl['ziel_pct']:.2f} %), Stopp {pl['stop']:.2f} ({'-' if m['d'] > 0 else '+'}{pl['stop_pct']:.2f} %).",
          f"Nach Kosten (ca. {COST_PCT} %) bleibt beim Ziel {pl['ziel_pct'] - COST_PCT:+.2f} % uebrig.",
          f"Daten ca. {alter:.0f} Min alt. Kurs in der Bitpanda-App pruefen. Short nur, wenn Bitpanda es fuer den Wert anbietet. Kein Finanzrat."]
    return "\n".join(z)


# ---------------- Zustand, Lauf, Backtest ----------------
def lade_json(pfad):
    try:
        with open(pfad, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def speichere_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def cmd_run(jetzt=None, daten_fn=None, push_fn=None, trocken=False):
    jetzt = jetzt or dt.datetime.now(dt.timezone.utc)
    push_fn = push_fn or sende_push
    heute = jetzt.astimezone(TZ_DE).strftime("%Y-%m-%d")
    tag = lade_json(STATE).get(heute, {"count": 0, "gesehen": []})
    tickers = [t for t in universum() if offen(t, jetzt)]
    print(f"Pruefe {len(tickers)} Werte auf Muster ...")
    daten = (daten_fn or lade)(tickers) if tickers else {}
    stats, kand = lade_json(STATS), []
    for t, df in daten.items():
        df = vollstaendig(df, jetzt)
        if len(df) < 70:
            continue
        alter = (jetzt - (df.index[-1] + pd.Timedelta(minutes=15))).total_seconds() / 60
        if alter > MAX_AGE:
            continue
        w = df.tail(FENSTER)
        m = beste(w)
        if not m or f"{t}|{m['name']}" in tag["gesehen"]:
            continue
        s = stats.get(m["name"])
        if NUR_POSITIV and s and s["n"] >= MIN_N and s["netto"] < 0:
            print(f"{t}: {m['name']} uebersprungen (Backtest nach Kosten negativ).")
            continue
        kand.append((m["q"], t, w, m, alter, s))
    kand.sort(key=lambda x: x[0], reverse=True)
    print(f"{len(kand)} Muster-Ausbrueche gefunden.")
    for q, t, w, m, alter, s in kand:
        if tag["count"] >= MAX_PER_DAY:
            print("Tageslimit erreicht.")
            break
        k = float(w["Close"].iloc[-1])
        pl = plan(k, spanne(w), m["d"])
        text = baue_text(t, m, pl, k, alter, s, jetzt)
        titel = f"{t}: {m['name']} ({'Long' if m['d'] > 0 else 'Short'}) {m['q']:.0f} %"
        print("\n" + titel + "\n" + text + "\n")
        if trocken:
            continue
        try:
            bild = zeichne(w, t, m, pl, jetzt)
        except Exception as e:  # noqa: BLE001
            print("Chart-Fehler:", e)
            bild = None
        if push_fn(titel, text, bild, "high" if m["q"] >= 80 else "default"):
            tag["count"] += 1
            tag["gesehen"].append(f"{t}|{m['name']}")
    if not trocken:
        speichere_state({heute: tag})


def kenn(g):
    n = len(g)
    z = int((g["erg"] == "ZIEL").sum())
    p, d = z / n, 1 + 3.84 / n
    c = (p + 1.92 / n) / d
    h = 1.96 * np.sqrt(p * (1 - p) / n + 0.96 / n ** 2) / d
    return {"n": n, "ziel_pct": round(p * 100, 1), "stopp_pct": round((g["erg"] == "STOPP").mean() * 100, 1),
            "plus_pct": round(float((g["net"] > 0).mean() * 100), 1), "netto": round(float(g["net"].mean()), 3),
            "ziel_von": round(float((c - h) * 100), 1), "ziel_bis": round(float((c + h) * 100), 1)}


def cmd_backtest():
    tickers = universum()
    print(f"Lade 60 Tage 15-Min-Daten fuer {len(tickers)} Werte ...")
    daten = lade(tickers, "60d")
    zeilen = []
    for nr, (t, df) in enumerate(daten.items(), 1):
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        n = len(df)
        if n < 100:
            continue
        C = df["Close"].to_numpy(float)
        letzte = {}
        for i in range(70, n - 1):
            w = df.iloc[max(0, i + 1 - FENSTER):i + 1]
            if abs(C[i] - C[i - 1]) < 0.25 * float((w["High"] - w["Low"]).median()):
                continue                      # ohne Bewegung kein Ausbruch (spart Zeit)
            m = beste(w)
            if not m or i - letzte.get(m["name"], -99) < 8:
                continue
            letzte[m["name"]] = i
            k = float(C[i])
            pl = plan(k, spanne(w), m["d"])
            nx = df.iloc[i + 1]
            erg, net = bewerte_kerze(m["d"], k, pl, float(nx["High"]), float(nx["Low"]), float(nx["Close"]))
            zeilen.append((m["name"], erg, net))
        if nr % 20 == 0:
            print(f"  {nr}/{len(daten)} Werte, {len(zeilen)} Faelle bisher")
    if not zeilen:
        print("Keine Faelle gefunden.")
        return
    g = pd.DataFrame(zeilen, columns=["name", "erg", "net"])
    stats = {name: kenn(x) for name, x in g.groupby("name")}
    stats["ALLE"] = kenn(g)
    with open(STATS, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(f"\n{'Muster':<38}{'n':>5}{'Ziel%':>7}{'Plus%':>7}{'Netto%':>8}")
    for name, s in sorted(stats.items(), key=lambda x: -x[1]["n"]):
        print(f"{name:<38}{s['n']:>5}{s['ziel_pct']:>7.0f}{s['plus_pct']:>7.0f}{s['netto']:>+8.2f}")
    print("\nHinweis: unter ca. 30 Faellen pro Muster ist die Quote Zufall. Gespeichert in", STATS)


def cmd_test():
    """Demo: kuenstliches Doppeltop mit Ausbruch, damit du Push und Bild pruefen kannst."""
    rs = np.random.default_rng(7)
    x = np.arange(75)
    ank = [(0, 100), (20, 100.2), (30, 104), (40, 100), (50, 104.1), (58, 100.4), (66, 100.2), (67, 100.15), (68, 99.3)]
    c = np.interp(x, [a for a, _ in ank], [b for _, b in ank]) + rs.normal(0, .05, len(x))
    c[:20] += rs.normal(0, .1, 20)
    o = np.r_[c[0], c[:-1]]
    idx = pd.date_range(end=dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0), periods=len(x), freq="15min")
    w = pd.DataFrame({"Open": o, "High": np.maximum(o, c) + .06, "Low": np.minimum(o, c) - .06, "Close": c}, index=idx)
    m = beste(w) or dict(name="Double top", q=88.0, d=-1, pts=[(30, 104), (50, 104.1)], lines=[(0, 99.95, 30, 74)])
    k = float(c[-1])
    pl = plan(k, spanne(w), m["d"])
    jetzt = dt.datetime.now(dt.timezone.utc)
    text = "TEST (Demo-Daten, kein echtes Signal)\n" + baue_text("DEMO", m, pl, k, 3, None, jetzt)
    ok = sende_push(f"TEST: {m['name']} (Demo)", text, zeichne(w, "DEMO", m, pl, jetzt), "default")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    modus = sys.argv[1] if len(sys.argv) > 1 else "run"
    if modus == "test":
        cmd_test()
    elif modus == "backtest":
        cmd_backtest()
    elif modus in ("run", "dry"):
        cmd_run(trocken=(modus == "dry"))
    else:
        print(__doc__)
