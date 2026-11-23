#!/usr/bin/env python3
"""
afterwaechter.py - prüft NACH jeder Meldung, ob die Prognose aufgegangen ist,
und baut daraus die Bot-Zentrale (docs/index.html).

Prognose = "Der Kurs erreicht das Gewinnziel, bevor er die Verlustgrenze berührt."
  ✅ ERFOLG      Gewinnziel zuerst erreicht
  ❌ FEHLSCHLAG  Verlustgrenze zuerst berührt
  ➖ NEUTRAL     weder noch (Ergebnis = Kursänderung am Ende des Fensters)
Alle Ergebnisse in Prozent NACH Kosten.

  python afterwaechter.py        auswerten, Handy-Meldung bei neuen Ergebnissen, Seite bauen
  python afterwaechter.py demo   nur eine Vorschau mit Beispieldaten bauen (docs/vorschau.html)

Benötigt auswertung.py und scanner.py im selben Ordner. Keine Anlageberatung.
"""
import datetime as dt
import json
import os
import sys

import numpy as np

import auswertung as A

LEDGER = "afterwaechter.json"
ERG = {"ZIEL": "ERFOLG", "STOPP": "FEHLSCHLAG", "NICHTS": "NEUTRAL"}
LABEL = {"⚪": "Zu früh", "🔴": "Im Minus", "🟡": "Unsicher", "🟢": "Im Plus"}
ICON = {"ERFOLG": "✅", "FEHLSCHLAG": "❌", "NEUTRAL": "➖"}


def sammle():
    z = []
    for e in A.lese_json("radar_state.json").get("ergebnisse", []):
        z.append(dict(id=f"R|{e['ticker']}|{e['zeit_de']}", system="Radar", ticker=e["ticker"],
                      zeit=str(e["zeit_de"])[:16], ergebnis=ERG.get(e["ergebnis"], "NEUTRAL"),
                      netto=float(e["netto"]), gemeldet=True, push_schon=True))
    try:
        A.auswerten_muster()  # füllt muster_ergebnisse.json
    except Exception as ex:  # noqa: BLE001
        print("Muster-Auswertung:", ex)
    cache = A.lese_json(A.MUSTER_CACHE)
    for r in A.lese_csv("muster_log.csv"):
        key = f"{r['ticker']}|{r['muster']}|{r['kerze_utc']}"
        e = cache.get(key)
        if e:
            g = str(r.get("gemeldet")) == "1"
            z.append(dict(id="M|" + key, system="Muster" if g else "Muster (still)", ticker=r["ticker"],
                          zeit=r["kerze_utc"], ergebnis=ERG.get(e[0], "NEUTRAL"), netto=float(e[1]),
                          gemeldet=g, push_schon=False))
    rows = [r for r in A.lese_csv("signals_log.csv") if r.get("type") == "LONG"]
    if rows:
        try:
            import scanner
            data = scanner.load(sorted({r["ticker"] for r in rows}))
            for r in rows:
                df = data.get(r["ticker"])
                if df is None:
                    continue
                tage = [x.date() for x in df.index]
                tag = dt.datetime.strptime(r["bar"], "%Y-%m-%d").date()
                if tag not in tage:
                    continue
                res = scanner.outcome(df.reset_index(drop=True), tage.index(tag))
                if res is None:
                    continue
                erg = "ERFOLG" if res >= scanner.TP else "FEHLSCHLAG" if res <= -scanner.SL else "NEUTRAL"
                z.append(dict(id=f"W|{r['ticker']}|{r['bar']}", system="Wächter", ticker=r["ticker"], zeit=r["bar"],
                              ergebnis=erg, netto=res * 100 - A.COST_PCT, gemeldet=True, push_schon=False))
        except Exception as ex:  # noqa: BLE001
            print("Wächter-Auswertung:", ex)
    return sorted(z, key=lambda x: x["zeit"])


def kennzahlen(liste):
    n = len(liste)
    if not n:
        return dict(n=0, erfolg=0, fehl=0, neutral=0, quote=0, schnitt=0, ampel="⚪", label="Zu früh")
    ne = [x["netto"] for x in liste]
    amp, _ = A.urteil(ne)
    f = lambda k: sum(1 for x in liste if x["ergebnis"] == k)
    return dict(n=n, erfolg=f("ERFOLG"), fehl=f("FEHLSCHLAG"), neutral=f("NEUTRAL"),
                quote=round(f("ERFOLG") / n * 100), schnitt=round(float(np.mean(ne)), 2), ampel=amp, label=LABEL[amp])


def baue_daten(ledger, demo=False):
    gem = [x for x in ledger if x["gemeldet"]]
    systeme = {}
    for s in ("Radar", "Muster", "Wächter", "Muster (still)"):
        sub = [x for x in ledger if x["system"] == s]
        if sub:
            systeme[s] = kennzahlen(sub)
    kurve = [round(float(v), 2) for v in np.cumsum([x["netto"] for x in gem])] if gem else []
    paper = None
    try:
        import papertrader
        if os.path.exists(papertrader.STATE):
            paper = papertrader.zusammenfassung()
    except Exception as ex:  # noqa: BLE001
        print("Paper-Konto:", ex)
    return dict(demo=demo, paper=paper, stand=dt.datetime.now().strftime("%d.%m.%Y %H:%M"), kosten=A.COST_PCT,
                gesamt=kennzahlen(gem), systeme=systeme, kurve=kurve[-120:],
                letzte=[{k: x[k] for k in ("system", "ticker", "zeit", "ergebnis", "netto")} for x in ledger[::-1][:40]])


def schreibe_seite(daten, pfad):
    os.makedirs(os.path.dirname(pfad) or ".", exist_ok=True)
    js = json.dumps(daten, ensure_ascii=False).replace("</", "<\\/")
    with open(pfad, "w", encoding="utf-8") as f:
        f.write(SEITE.replace("__DATEN__", js))


def demo_daten():
    rs = np.random.default_rng(5)
    z = []
    for i in range(70):
        s = rs.choice(["Radar", "Muster", "Wächter", "Muster (still)"], p=[.4, .25, .15, .2])
        e = rs.choice(["ERFOLG", "FEHLSCHLAG", "NEUTRAL"], p=[.28, .5, .22])
        n = {"ERFOLG": 0.9, "FEHLSCHLAG": -1.4, "NEUTRAL": -0.5}[e] + float(rs.normal(0, .3))
        z.append(dict(id=str(i), system=str(s), ticker=str(rs.choice(["NVDA", "SAP.DE", "BTC-USD", "AMD", "RHM.DE", "COIN"])),
                      zeit=f"2026-11-{10 + i // 6:02d} {9 + i % 8:02d}:{(i * 7) % 60:02d}", ergebnis=str(e), netto=n,
                      gemeldet=s != "Muster (still)", push_schon=True))
    return sorted(z, key=lambda x: x["zeit"])


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "demo":
        schreibe_seite(baue_daten(demo_daten(), demo=True), os.path.join("docs", "vorschau.html"))
        print("docs/vorschau.html gebaut (Beispieldaten).")
        return
    alt = {x["id"] for x in A.lese_json(LEDGER).get("eintraege", [])}
    ledger = sammle()
    neu = [x for x in ledger if x["id"] not in alt and not x["push_schon"] and x["gemeldet"]]
    if neu and alt:  # beim allerersten Lauf keine Flut an Meldungen
        z = [f"{ICON[x['ergebnis']]} {x['ticker']} ({x['system']}): {x['ergebnis'].replace('FEHLSCHLAG', 'Fehlschlag').title()}, "
             f"{A.pz(x['netto'])} % nach Kosten" for x in neu[:8]]
        A.sende_push("Afterwaechter: Ergebnisse", "Prognosen geprüft:\n" + "\n".join(z) + "\n\nKeine Anlageberatung.")
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump({"eintraege": ledger[-400:]}, f, ensure_ascii=False, indent=0)
    schreibe_seite(baue_daten(ledger), os.path.join("docs", "index.html"))
    print(f"{len(ledger)} Ergebnisse, {len(neu)} neu. docs/index.html gebaut.")


SEITE = r"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Bot-Zentrale</title>
<style>
:root{--bg:#f4f6fb;--card:#fff;--tx:#14213d;--mu:#6b7690;--ln:#e3e8f2;--ok:#16a34a;--bad:#dc2626;--mid:#d97706;--gr:#94a3b8;--ac:#4f46e5;color-scheme:light}
@media(prefers-color-scheme:dark){:root{--bg:#0d1220;--card:#161d30;--tx:#e8ecf6;--mu:#93a0bd;--ln:#242d45;--ok:#4ade80;--bad:#f87171;--mid:#fbbf24;--gr:#64748b;--ac:#818cf8;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:16px/1.45 -apple-system,system-ui,"Segoe UI",sans-serif;padding:env(safe-area-inset-top) 0 env(safe-area-inset-bottom)}
main{max-width:880px;margin:0 auto;padding:18px 14px 40px}h1{font-size:26px;margin:6px 0 2px}.mu{color:var(--mu);font-size:13px}
.demo{background:var(--mid);color:#000;border-radius:10px;padding:8px 12px;margin:10px 0;font-size:14px;font-weight:600}
.card{background:var(--card);border:1px solid var(--ln);border-radius:18px;padding:16px;margin:12px 0;box-shadow:0 1px 3px rgba(0,0,0,.05)}
.hero{display:flex;gap:18px;flex-wrap:wrap;align-items:center}.big{font-size:38px;font-weight:800;line-height:1}.lab{font-size:12px;color:var(--mu);text-transform:uppercase;letter-spacing:.05em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}.grid .card{margin:0}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:700;color:#fff}
.bar{display:flex;height:10px;border-radius:6px;overflow:hidden;background:var(--ln);margin:10px 0}.bar i{display:block}
.row{display:flex;align-items:center;gap:10px;padding:10px 0;border-top:1px solid var(--ln)}.row:first-child{border-top:0}.row b{display:block}.row .r{margin-left:auto;font-weight:700;font-variant-numeric:tabular-nums}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}.chip{border:1px solid var(--ln);background:var(--card);color:var(--tx);border-radius:99px;padding:7px 14px;font-size:14px}.chip.on{background:var(--ac);color:#fff;border-color:var(--ac)}
.pos{color:var(--ok)}.neg{color:var(--bad)}svg{width:100%;height:70px;display:block}small{color:var(--mu)}
</style></head><body><main>
<h1>Bot-Zentrale</h1><div class="mu" id="stand"></div><div id="demo"></div>
<div class="card"><div class="lab">Gesamtbilanz (nur gemeldete Prognosen, nach Kosten)</div><div class="hero" id="hero"></div><div id="kurve"></div></div>
<div id="paper"></div><div class="lab" style="margin-top:18px">Die Systeme</div><div class="grid" id="sys"></div>
<div class="lab" style="margin-top:22px">Letzte Ergebnisse</div><div class="chips" id="chips"></div><div class="card" id="liste"></div>
<p class="mu">Prognose = Gewinnziel wird vor der Verlustgrenze erreicht. Ampel: ⚪ unter 30 Fälle = Zufall · 🔴 Minus · 🟡 knapp im Plus · 🟢 ab 100 Fällen klar im Plus. Vergangenheit ist keine Garantie. Keine Anlageberatung.</p>
</main><script>
const D=__DATEN__;
const F=(x,n=2)=>x.toFixed(n).replace(".",","),P=x=>(x>0?"+":"")+F(x)+" %";
const COL={"⚪":"var(--gr)","🔴":"var(--bad)","🟡":"var(--mid)","🟢":"var(--ok)"},IC={ERFOLG:"✅",FEHLSCHLAG:"❌",NEUTRAL:"➖"};
function h(t,c,x){const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e}
document.getElementById("stand").textContent="Stand: "+D.stand+" · Kosten "+F(D.kosten,1)+" % pro Trade";
if(D.demo)document.getElementById("demo").append(h("div","demo","VORSCHAU mit Beispieldaten, keine echten Ergebnisse"));
const g=D.gesamt,hero=document.getElementById("hero");
[[g.n,"Prognosen geprüft"],[g.quote+" %","Erfolgsquote"],[P(g.schnitt),"Ø pro Prognose"]].forEach(([v,l])=>{const d=h("div");d.append(h("div","big",v),h("div","lab",l));hero.append(d)});
const pill=h("span","pill",g.ampel+" "+g.label);pill.style.background=COL[g.ampel];hero.append(pill);
if(D.kurve.length>1){const k=D.kurve,mn=Math.min(0,...k),mx=Math.max(0,...k),r=(mx-mn)||1,w=300,hh=70;
const pts=k.map((v,i)=>(i/(k.length-1)*w).toFixed(1)+","+(hh-4-(v-mn)/r*(hh-8)).toFixed(1)).join(" ");
const y0=(hh-4-(0-mn)/r*(hh-8)).toFixed(1),up=k[k.length-1]>=0;
document.getElementById("kurve").innerHTML='<svg viewBox="0 0 300 70" preserveAspectRatio="none"><line x1="0" x2="300" y1="'+y0+'" y2="'+y0+'" stroke="var(--ln)" stroke-dasharray="4"/><polyline fill="none" stroke-width="2.5" stroke="'+(up?"var(--ok)":"var(--bad)")+'" points="'+pts+'"/></svg>';
document.getElementById("kurve").append(h("small",null,"Summe aller Ergebnisse nach Kosten, Verlauf: "+P(k[k.length-1])))}
if(D.paper){const p=D.paper,c=h("div","card"),E=x=>(x>0?"+":"")+F(x)+" €";c.append(h("div","lab","Paper-Konto (Übung, kein echtes Geld) · "+p.hebel+"x Hebel"));
const hr=h("div","hero");[[F(p.kapital)+" €","Kapital (Start "+F(p.start,0)+" €)"],[E(p.netto),"Netto nach Kosten"],[p.n+" / "+p.ziel_trades,"Trades bis zur Prüfung"]].forEach(([v,l])=>{const d=h("div");d.append(h("div","big",v),h("div","lab",l));hr.append(d)});c.append(hr);
const b=h("div","bar"),i=h("i");i.style.width=Math.min(100,p.n/p.ziel_trades*100)+"%";i.style.background="var(--ac)";b.append(i);c.append(b);
const pl=h("span","pill",p.freigabe?"Prüfung bestanden":"Echtgeld bleibt aus");pl.style.background=p.freigabe?"var(--ok)":"var(--gr)";c.append(pl);
if(p.pausiert)c.append(h("div","demo","PAUSE: Drawdown-Grenze erreicht, keine neuen Trades"));
p.pruefung.forEach(t=>c.append(h("small",null,t),h("br")));
p.trades.forEach(x=>{const r=h("div","row"),m=h("div");m.append(h("b",null,(x.richtung>0?"LONG ":"SHORT ")+x.ticker),h("small",null,x.system+" · "+x.grund));r.append(h("div",null,x.netto>0?"✅":"❌"),m,h("div","r "+(x.netto>=0?"pos":"neg"),E(x.netto)));c.append(r)});
document.getElementById("paper").append(c)}
const sy=document.getElementById("sys");
Object.entries(D.systeme).forEach(([n,s])=>{const c=h("div","card"),t=h("div");t.append(h("b",null,n+" "));const p=h("span","pill",s.ampel+" "+s.label);p.style.background=COL[s.ampel];t.append(p);c.append(t);
const b=h("div","bar");[["ERFOLG","var(--ok)"],["NEUTRAL","var(--gr)"],["FEHLSCHLAG","var(--bad)"]].forEach(([k,col])=>{const i=h("i");i.style.width=(s.n?{ERFOLG:s.erfolg,NEUTRAL:s.neutral,FEHLSCHLAG:s.fehl}[k]/s.n*100:0)+"%";i.style.background=col;b.append(i)});c.append(b);
c.append(h("div",null,"✅ "+s.erfolg+"  ➖ "+s.neutral+"  ❌ "+s.fehl),h("small",null,s.n+" Fälle · Ø "+P(s.schnitt)+" nach Kosten"));sy.append(c)});
if(!Object.keys(D.systeme).length)sy.append(h("div","card","Noch keine ausgewerteten Prognosen. Das System sammelt, schau in ein paar Tagen wieder rein."));
let filt="ALLE";const ch=document.getElementById("chips"),li=document.getElementById("liste");
function zeige(){li.replaceChildren();const l=D.letzte.filter(x=>filt=="ALLE"||x.ergebnis==filt);
if(!l.length)li.append(h("small",null,"Keine Einträge."));
l.forEach(x=>{const r=h("div","row"),m=h("div");m.append(h("b",null,x.ticker),h("small",null,x.system+" · "+x.zeit));const v=h("div","r "+(x.netto>=0?"pos":"neg"),P(x.netto));r.append(h("div",null,IC[x.ergebnis]),m,v);li.append(r)})}
[["ALLE","Alle"],["ERFOLG","✅ Erfolg"],["FEHLSCHLAG","❌ Fehlschlag"],["NEUTRAL","➖ Weder noch"]].forEach(([k,t])=>{const b=h("button","chip"+(k==filt?" on":""),t);b.onclick=()=>{filt=k;[...ch.children].forEach(c=>c.classList.remove("on"));b.classList.add("on");zeige()};ch.append(b)});zeige();
</script></body></html>"""

if __name__ == "__main__":
    main()
