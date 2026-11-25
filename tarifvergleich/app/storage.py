"""Speicher-Auswertung: Was hat der Batteriespeicher bisher eingespart?

Je Stunde (Gegenfaktum „ohne Speicher“):
  Ersparnis = Entladung × Bezugspreis            (sonst aus dem Netz gekauft)
            − PV-Ladung × Einspeisevergütung     (sonst eingespeist und vergütet)
            − Netzladung × Bezugspreis           (Akku hat aus dem Netz geladen)

Netzladung: mit Netzdaten = min(Ladung, Netzbezug) der Stunde, sonst Ladung über PV-Erzeugung.
Aufteilung auf zwei Speicher: vor Inbetriebnahme der Erweiterung alles Bestand; danach nach
gemessener Leistung je Akku (falls vorhanden), sonst nach Kapazität.
Zusatznutzen der Erweiterung: stündliche Simulation „Bestand allein“ vs. „beide“ auf denselben
PV-Überschüssen und demselben Bedarf.
"""
from __future__ import annotations

import datetime as dt
import math
from zoneinfo import ZoneInfo

from .sim import dyn_price_ct

C_RATE = 0.5  # max. Lade-/Entladeleistung je kWh Kapazität in der Simulation


def parse_price_history(entries, fallback_ct: float, tz: ZoneInfo) -> list[tuple[int, float]]:
    """['2025-01-01=26.19', '2026-10-27=29.17'] -> [(unix_start, ct), ...] sortiert."""
    out = []
    for e in entries or []:
        try:
            d, v = str(e).split("=", 1)
            start = dt.datetime.combine(dt.date.fromisoformat(d.strip()), dt.time(0), tz)
            out.append((int(start.timestamp()), float(v.strip().replace(",", "."))))
        except ValueError:
            continue
    out.sort()
    return out or [(0, float(fallback_ct))]


def price_at(hist: list[tuple[int, float]], ts: int) -> float:
    p = hist[0][1]
    for start, ct in hist:
        if ts >= start:
            p = ct
        else:
            break
    return p


def simulate_storage(surplus, deficit, cap_kwh, max_kw, eta):
    """Einfacher Eigenverbrauchsspeicher: lädt aus Überschuss, entlädt für Bedarf.
    cap_kwh/max_kw dürfen Listen (je Stunde) oder Zahlen sein."""
    n = len(surplus)
    ec = ed = math.sqrt(max(0.5, min(1.0, eta)))
    caps = cap_kwh if isinstance(cap_kwh, list) else [cap_kwh] * n
    pmax = max_kw if isinstance(max_kw, list) else [max_kw] * n
    soc = caps[0] * 0.5 if n else 0.0
    ins, outs = [0.0] * n, [0.0] * n
    for k in range(n):
        cap = caps[k]
        soc = min(soc, cap)
        c = min(surplus[k], pmax[k], max(0.0, cap - soc) / ec)
        soc += c * ec
        d = min(deficit[k], pmax[k], soc * ed)
        soc -= d / ed
        ins[k], outs[k] = c, d
    return ins, outs


def analyze(hours, b_in, b_out, pv, imp, exp, spot, w_old, w_ext, o, tz: ZoneInfo) -> dict:
    """hours: Unix-Stundenbeginn; b_in/b_out/pv: kWh; imp/exp: kWh oder None (keine Netzdaten);
    spot: €/MWh oder None; w_old/w_ext: |mittlere Leistung| je Akku in W oder None."""
    n = len(hours)
    hist = parse_price_history(o.get("bezugspreis_historie"), o["festpreis_arbeitspreis_ct_brutto"], tz)
    feed = float(o["einspeiseverguetung_ct"])
    cap_old = float(o["speicher_bestand_kwh"])
    cap_ext = float(o["speicher_erweiterung_kwh"])
    usable = float(o["speicher_nutzbar_prozent"]) / 100.0
    inv_kw = float(o["wechselrichter_max_kw"])
    has_ext = cap_ext > 0 and bool(o.get("speicher_erweiterung_start"))
    t_ext = (int(dt.datetime.combine(dt.date.fromisoformat(o["speicher_erweiterung_start"]), dt.time(0), tz).timestamp())
             if has_ext else None)

    rows = []
    for k in range(n):
        h = hours[k]
        P = price_at(hist, h)
        known = imp[k] is not None
        gc = min(b_in[k], imp[k]) if known else max(0.0, b_in[k] - pv[k])
        pvc = b_in[k] - gc
        avoided = b_out[k] * P / 100.0
        lost_feed = pvc * feed / 100.0
        grid_cost = gc * P / 100.0
        s_fix = avoided - lost_feed - grid_cost
        pd = dyn_price_ct(spot[k], o) if spot[k] is not None else None
        s_dyn = (b_out[k] * pd - pvc * feed - gc * pd) / 100.0 if pd is not None else None

        if not has_ext or h < t_ext:
            share = 0.0
        elif w_old[k] is not None and w_ext[k] is not None and (w_old[k] + w_ext[k]) > 10:
            share = w_ext[k] / (w_old[k] + w_ext[k])
        else:
            share = cap_ext / (cap_old + cap_ext)
        rows.append({
            "t": h, "P": P, "b_in": b_in[k], "b_out": b_out[k], "pvc": pvc, "gc": gc,
            "avoided": avoided, "lost_feed": lost_feed, "grid_cost": grid_cost,
            "s_fix": s_fix, "s_ext": s_fix * share, "s_old": s_fix * (1.0 - share),
            "s_dyn": s_dyn, "known": known, "imp": imp[k], "exp": exp[k],
            "marg": 0.0, "marg_year": 0.0,
        })

    # --- Zusatznutzen der Erweiterung (nur wo Netzdaten vorhanden sind)
    tot_in, tot_out = sum(b_in), sum(b_out)
    eta = tot_out / tot_in if tot_in > 0 else 0.95
    idx = [k for k in range(n) if rows[k]["known"]]
    marg = {"verfuegbar": bool(idx and has_ext), "eta": eta}
    if idx and has_ext:
        surplus = [rows[k]["pvc"] + (exp[k] or 0.0) for k in idx]
        deficit = [rows[k]["b_out"] + max(0.0, (imp[k] or 0.0) - rows[k]["gc"]) for k in idx]
        price = [rows[k]["P"] for k in idx]
        old_cap, both_cap = cap_old * usable, (cap_old + cap_ext) * usable
        old_kw, both_kw = min(inv_kw, C_RATE * cap_old), min(inv_kw, C_RATE * (cap_old + cap_ext))
        real_cap = [both_cap if hours[k] >= t_ext else old_cap for k in idx]
        real_kw = [both_kw if hours[k] >= t_ext else old_kw for k in idx]

        def value(ins, outs):
            return [(outs[j] * price[j] - ins[j] * feed) / 100.0 for j in range(len(idx))]

        v_old = value(*simulate_storage(surplus, deficit, old_cap, old_kw, eta))
        v_both = value(*simulate_storage(surplus, deficit, both_cap, both_kw, eta))
        v_real = value(*simulate_storage(surplus, deficit, real_cap, real_kw, eta))
        for j, k in enumerate(idx):
            rows[k]["marg_year"] = v_both[j] - v_old[j]
            if hours[k] >= t_ext:
                rows[k]["marg"] = v_real[j] - v_old[j]
        days = len(idx) / 24.0
        measured = sum(rows[k]["avoided"] - rows[k]["lost_feed"] for k in idx)
        marg.update(
            seit_inbetriebnahme_eur=sum(rows[k]["marg"] for k in idx),
            zeitraum_tage=days,
            zeitraum_eur=sum(v_both[j] - v_old[j] for j in range(len(idx))),
            pro_jahr_eur=sum(v_both[j] - v_old[j] for j in range(len(idx))) * 365.0 / days if days else 0.0,
            modell_simuliert_eur=sum(v_real),
            modell_gemessen_eur=measured,
            nutzbar_bestand_kwh=old_cap, nutzbar_gesamt_kwh=both_cap,
        )
    return {"rows": rows, "zusatznutzen": marg, "preise": hist, "t_ext": t_ext}


SUM_KEYS = ("b_in", "b_out", "pvc", "gc", "avoided", "lost_feed", "grid_cost", "s_fix", "s_old", "s_ext",
            "marg", "marg_year")


def aggregate(rows, tz: ZoneInfo, key: str) -> list[dict]:
    out: dict[str, dict] = {}
    for r in rows:
        local = dt.datetime.fromtimestamp(r["t"], tz)
        k = local.strftime("%Y-%m-%d") if key == "day" else local.strftime("%Y-%m")
        a = out.get(k)
        if a is None:
            a = out[k] = {"key": k, "s_dyn": 0.0, "dyn_hours": 0, "hours": 0, **{s: 0.0 for s in SUM_KEYS}}
        for s in SUM_KEYS:
            a[s] += r[s]
        if r["s_dyn"] is not None:
            a["s_dyn"] += r["s_dyn"]
            a["dyn_hours"] += 1
        a["hours"] += 1
    return [out[k] for k in sorted(out)]


def summary(res: dict, o: dict, tz: ZoneInfo, live: dict) -> dict:
    rows = res["rows"]
    if not rows:
        return {}
    tot = {s: sum(r[s] for r in rows) for s in SUM_KEYS}
    dyn_rows = [r for r in rows if r["s_dyn"] is not None]
    last = rows[-1]["t"]
    y_rows = [r for r in rows if r["t"] > last - 365 * 86400]
    y_days = len(y_rows) / 24.0
    year_old = sum(r["s_old"] for r in y_rows) * (365.0 / y_days if y_days else 0.0)
    cost_old = float(o.get("speicher_bestand_kosten_eur") or 0)
    cost_ext = float(o.get("speicher_erweiterung_kosten_eur") or 0)
    marg = res["zusatznutzen"]

    def payback(cost, done, per_year):
        if not cost:
            return None
        rest = max(0.0, cost - done)
        return {"kosten": cost, "erspart": done, "prozent": done / cost * 100.0,
                "rest_eur": rest, "rest_jahre": rest / per_year if per_year > 0 else None,
                "gesamt_jahre": cost / per_year if per_year > 0 else None}

    ext_start = o.get("speicher_erweiterung_start") or None
    return {
        **tot,
        "von": dt.datetime.fromtimestamp(rows[0]["t"], tz).isoformat(),
        "bis": dt.datetime.fromtimestamp(last + 3600, tz).isoformat(),
        "tage": len(rows) / 24.0,
        "ct_je_kwh": tot["s_fix"] / tot["b_out"] * 100.0 if tot["b_out"] else None,
        "eta": marg.get("eta"),
        "letzte_12_monate": sum(r["s_fix"] for r in y_rows),
        "dyn": sum(r["s_dyn"] for r in dyn_rows),
        "dyn_fix_vergleich": sum(r["s_fix"] for r in dyn_rows),
        "dyn_von": dt.datetime.fromtimestamp(dyn_rows[0]["t"], tz).isoformat() if dyn_rows else None,
        "bestand": {"kwh": o["speicher_bestand_kwh"], "start": o.get("speicher_bestand_start"),
                    "pro_jahr": year_old, "amortisation": payback(cost_old, tot["s_old"], year_old),
                    **live.get("bestand", {})},
        "erweiterung": {"kwh": o["speicher_erweiterung_kwh"], "start": ext_start,
                        "anteilig": tot["s_ext"], "zusatznutzen": marg,
                        "amortisation": payback(cost_ext, marg.get("seit_inbetriebnahme_eur", 0.0),
                                                marg.get("pro_jahr_eur", 0.0)) if ext_start else None,
                        **live.get("erweiterung", {})},
        "preise": [{"ab": dt.datetime.fromtimestamp(s, tz).date().isoformat() if s else None, "ct": c}
                   for s, c in res["preise"]],
        "einspeiseverguetung_ct": o["einspeiseverguetung_ct"],
    }
