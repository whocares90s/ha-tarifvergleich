"""Kostenrechnung Festpreis vs. dynamischer Tarif und Lastverschiebungs-Szenarien.

Alle Energiewerte in kWh je Stunde, Preise in ct/kWh brutto, Kosten in EUR.

Szenarien (jeweils mit dynamischem Preis):
  dyn       – Verbrauch wie gemessen
  dyn_ev    – nur E-Auto-Ladung verschoben
  dyn_wp    – nur Wärmepumpe verschoben (Heizung + Warmwasser)
  dyn_bat   – nur Hausakku lädt günstig aus dem Netz
  dyn_all   – alle vorhandenen Maßnahmen kombiniert (WP -> E-Auto -> Akku)
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

HOURS_PER_YEAR = 8760.0


# --------------------------------------------------------------------------- Tarife
def vat(o: dict) -> float:
    return 1.0 + float(o["mwst_prozent"]) / 100.0


def dyn_surcharges_ct_netto(o: dict) -> float:
    """Alle verbrauchsabhängigen Aufschläge auf den Börsenpreis (netto)."""
    return (
        o["dyn_aufschlag_ct_netto"]
        + o["netzentgelt_ct_netto"]
        + o["aufschlag_netznutzung_ct_netto"]
        + o["kwkg_umlage_ct_netto"]
        + o["offshore_umlage_ct_netto"]
        + o["stromsteuer_ct_netto"]
        + o["konzessionsabgabe_ct_netto"]
    )


def dyn_price_ct(spot_eur_mwh: float, o: dict) -> float:
    """Endpreis dynamisch in ct/kWh brutto (Börsenpreis €/MWh / 10 = ct/kWh)."""
    return (spot_eur_mwh / 10.0 + dyn_surcharges_ct_netto(o)) * vat(o)


def dyn_fixed_eur_year(o: dict) -> float:
    return (
        o["dyn_grundgebuehr_eur_monat_brutto"] * 12.0
        + o["netz_grundpreis_eur_jahr_netto"] * vat(o)
        + o["messstelle_dyn_eur_jahr_brutto"]
    )


def fix_fixed_eur_year(o: dict) -> float:
    return float(o["festpreis_grundpreis_eur_jahr_brutto"])


def breakdown(o: dict) -> dict:
    v = vat(o)
    return {
        "mwst_faktor": v,
        "aufschlaege_ct_netto": {
            "Tibber-Aufschlag (Beschaffung/Marge)": o["dyn_aufschlag_ct_netto"],
            "Netzentgelt": o["netzentgelt_ct_netto"],
            "Aufschlag besondere Netznutzung (§19)": o["aufschlag_netznutzung_ct_netto"],
            "KWKG-Umlage": o["kwkg_umlage_ct_netto"],
            "Offshore-Umlage": o["offshore_umlage_ct_netto"],
            "Stromsteuer": o["stromsteuer_ct_netto"],
            "Konzessionsabgabe": o["konzessionsabgabe_ct_netto"],
        },
        "aufschlaege_ct_brutto_summe": dyn_surcharges_ct_netto(o) * v,
        "fix_ct_brutto": o["festpreis_arbeitspreis_ct_brutto"],
        "fix_grundpreis_eur_jahr": fix_fixed_eur_year(o),
        "dyn_grundpreis_eur_jahr": dyn_fixed_eur_year(o),
        "dyn_grundpreis_teile": {
            "Tibber-Grundgebühr": o["dyn_grundgebuehr_eur_monat_brutto"] * 12.0,
            "Netz-Grundpreis": o["netz_grundpreis_eur_jahr_netto"] * v,
            "Messstelle (Smart Meter)": o["messstelle_dyn_eur_jahr_brutto"],
        },
        # Börsenpreis, ab dem dynamisch teurer ist als fix (nur Arbeitspreis)
        "break_even_spot_ct": o["festpreis_arbeitspreis_ct_brutto"] / v - dyn_surcharges_ct_netto(o),
    }


# --------------------------------------------------------------------------- E-Auto
EMPTY_EV = {"sitzungen": 0, "kwh_verschoben": 0.0, "ersparnis_eur": 0.0}


def shift_ev(hours, imp, ev, price, o, tz: ZoneInfo):
    """Netzbezug von Abend-/Nacht-Ladevorgängen in die günstigsten Stunden bis
    zur Fertig-Uhrzeit verschieben. Tagsüber-Ladevorgänge (meist PV) bleiben."""
    n = len(hours)
    new = list(imp)
    used = list(ev)  # aktuelle EV-Last je Stunde
    maxkw = float(o["ev_max_kw"])
    ab = int(o["ev_abend_ab_uhr"])
    bis = int(o["ev_fertig_bis_uhr"])
    moved_kwh = 0.0
    sessions = 0
    saved_eur = 0.0

    h = 0
    while h < n:
        if ev[h] < 0.1:
            h += 1
            continue
        s = e = h
        gap = 0
        k = h + 1
        while k < n:
            if ev[k] >= 0.1:
                e = k
                gap = 0
            else:
                gap += 1
                if gap > 1:
                    break
            k += 1
        h = e + 1

        local = dt.datetime.fromtimestamp(hours[s], tz)
        if not (local.hour >= ab or local.hour < bis):
            continue
        deadline = local.replace(hour=bis, minute=0, second=0, microsecond=0)
        if deadline <= local:
            deadline = (local + dt.timedelta(days=1)).replace(hour=bis, minute=0, second=0, microsecond=0)
        last_start = deadline.timestamp() - 3600
        w_end = s
        while w_end + 1 < n and hours[w_end + 1] <= last_start:
            w_end += 1
        w_end = max(w_end, e)
        window = list(range(s, w_end + 1))
        if any(price[i] is None for i in window):
            continue

        grid = {i: max(0.0, min(ev[i], imp[i])) for i in range(s, e + 1)}
        total = sum(grid.values())
        if total < 0.1:
            continue

        caps = {i: max(0.0, maxkw - (used[i] - grid.get(i, 0.0))) for i in window}
        remaining = total
        alloc: dict[int, float] = {}
        for i in sorted(window, key=lambda x: price[x]):
            if remaining <= 1e-9:
                break
            a = min(caps[i], remaining)
            if a > 0:
                alloc[i] = a
                remaining -= a
        allocated = total - remaining
        if allocated < 0.1:
            continue
        frac = allocated / total
        old_cost = sum(g * frac * price[i] for i, g in grid.items())
        new_cost = sum(a * price[i] for i, a in alloc.items())
        if new_cost >= old_cost:
            continue
        for i, g in grid.items():
            new[i] -= g * frac
            used[i] -= g * frac
        for i, a in alloc.items():
            new[i] += a
            used[i] += a
        sessions += 1
        moved_kwh += allocated
        saved_eur += (old_cost - new_cost) / 100.0

    return new, {"sitzungen": sessions, "kwh_verschoben": moved_kwh, "ersparnis_eur": saved_eur}


# --------------------------------------------------------------------------- Hausakku
EMPTY_BAT = {"kwh_netzladung": 0.0, "kwh_entladen": 0.0, "vollzyklen": 0.0, "ersparnis_eur": 0.0}


def battery_arbitrage(hours, imp, soc, price, o):
    """Akku in günstigen Stunden aus dem Netz laden und damit späteren teuren
    Netzbezug ersetzen. Freie Kapazität kommt aus dem realen SoC-Verlauf, so dass
    PV-Ladung nicht verdrängt wird – an PV-armen Tagen ist der Akku leer und es
    wird entsprechend viel Netzladung möglich. Greedy über die Paare mit größtem Spread."""
    n = len(hours)
    cap = float(o["akku_kapazitaet_kwh"])
    maxkw = float(o["akku_max_netzladeleistung_kw"])
    eta = float(o["akku_wirkungsgrad"])
    min_spread = float(o["akku_mindest_spread_ct"])

    free = [cap * max(0.0, 100.0 - s) / 100.0 if s is not None else 0.0 for s in soc]
    pairs = []
    for i in range(n):
        if price[i] is None or free[i] < 0.2:
            continue
        for j in range(i + 1, min(n, i + 25)):
            if price[j] is None or imp[j] < 0.05:
                continue
            gain = eta * price[j] - price[i]
            if gain >= min_spread:
                pairs.append((gain, i, j))
    pairs.sort(reverse=True)

    charged = [0.0] * n
    discharged = [0.0] * n
    gain_at = [0.0] * n  # Ersparnis in EUR, der Ladestunde zugeordnet
    extra = [0.0] * n
    total_in = 0.0
    saved_ct = 0.0
    for gain, i, j in pairs:
        x = maxkw - charged[i]
        if x <= 0.01:
            continue
        x = min(x, (imp[j] - discharged[j]) / eta, (maxkw - discharged[j]) / eta)
        if x <= 0.01:
            continue
        for h in range(i, j):
            x = min(x, free[h] - extra[h])
            if x <= 0.01:
                break
        if x <= 0.01:
            continue
        charged[i] += x
        discharged[j] += x * eta
        for h in range(i, j):
            extra[h] += x
        total_in += x
        saved_ct += x * gain
        gain_at[i] += x * gain / 100.0

    new = [imp[h] + charged[h] - discharged[h] for h in range(n)]
    return new, {
        "kwh_netzladung": total_in,
        "kwh_entladen": total_in * eta,
        "vollzyklen": total_in / cap if cap else 0.0,
        "ersparnis_eur": saved_ct / 100.0,
    }, charged, gain_at


# --------------------------------------------------------------------------- Wärmepumpe
def _empty_shift() -> dict:
    return {"kwh_netz": 0.0, "kwh_verschoben": 0.0, "kwh_mehrverbrauch": 0.0, "ersparnis_eur": 0.0, "stunden": 0}


EMPTY_WP = {"wp_netz_kwh": 0.0, "kwh_verschoben": 0.0, "kwh_mehrverbrauch": 0.0, "ersparnis_eur": 0.0,
            "heizung": _empty_shift(), "warmwasser": _empty_shift()}


def split_heatpump(wp, pmax, o):
    """WP-Energie je Stunde in Heizung und Warmwasser aufteilen. Eine Stunde gilt
    als Warmwasser-Stunde, wenn die Spitzenleistung die Schwelle erreicht; der
    WW-Anteil ist der Verbrauch über dem mittleren Heizbedarf der Nachbarstunden."""
    n = len(wp)
    thr = float(o["ww_schwelle_w"])
    is_ww = [pm is not None and pm >= thr for pm in pmax]
    heat, dhw = list(wp), [0.0] * n
    for h in range(n):
        if not is_ww[h] or wp[h] <= 0:
            continue
        nb = [wp[k] for k in range(max(0, h - 3), min(n, h + 4)) if k != h and not is_ww[k]]
        base = sum(nb) / len(nb) if nb else 0.0
        dhw[h] = max(0.0, wp[h] - min(base, wp[h]))
        heat[h] = wp[h] - dhw[h]
    return heat, dhw, is_ww


def _greedy_shift(n, price, grid, load, cap_kw, frac, win, pen, spread):
    """Teuerste Stunden zuerst entlasten: frac·grid[j] wandert in die billigsten
    Stunden im Fenster ±win, solange load[i] < cap_kw. load wird fortgeschrieben."""
    delta = [0.0] * n
    moved = extra = saved_ct = 0.0
    order = sorted((h for h in range(n) if price[h] is not None and grid[h] > 0.01), key=lambda h: -price[h])
    for j in order:
        m = frac * grid[j]
        cands = [i for i in range(max(0, j - win), min(n, j + win + 1))
                 if i != j and price[i] is not None and price[i] * pen + spread <= price[j]]
        cands.sort(key=lambda i: price[i])
        for i in cands:
            if m <= 0.01:
                break
            cap = cap_kw - load[i]
            if cap <= 0.01:
                continue
            x = min(m, cap / pen)
            load[i] += x * pen
            load[j] -= x
            delta[i] += x * pen
            delta[j] -= x
            m -= x
            moved += x
            extra += x * (pen - 1.0)
            saved_ct += x * (price[j] - price[i] * pen)
    return delta, {"kwh_verschoben": moved, "kwh_mehrverbrauch": extra, "ersparnis_eur": saved_ct / 100.0}


def shift_heatpump(hours, imp, wp, pmax, price, o):
    """Warmwasser (Speicher, sehr flexibel) und Heizung (thermische Masse, begrenzt)
    getrennt verschieben. Als WP-Netzanteil zählt der WP-Verbrauch, soweit in der
    Stunde Netzbezug da war; er wird anteilig auf Heizung/Warmwasser verteilt."""
    n = len(hours)
    spread = float(o["wp_mindest_spread_ct"])
    heat, dhw, is_ww = split_heatpump(wp, pmax, o)
    grid = [max(0.0, min(wp[h], imp[h])) for h in range(n)]
    g_ww = [grid[h] * dhw[h] / wp[h] if wp[h] > 0 else 0.0 for h in range(n)]
    g_heat = [grid[h] - g_ww[h] for h in range(n)]

    # 1) Warmwasser: ganze Ladung verschiebbar, bis zur WW-Leistung
    load_all = [max(0.0, w) for w in wp]
    d_ww, i_ww = _greedy_shift(n, price, g_ww, load_all, float(o["ww_max_kw"]), 1.0, int(o["ww_fenster_h"]),
                               1.0 + float(o["ww_mehrverbrauch_prozent"]) / 100.0, spread)
    # 2) Heizung: Anteil verschiebbar, begrenzt durch die Heizleistung
    load_heat = [max(0.0, x) for x in heat]
    d_h, i_h = _greedy_shift(n, price, g_heat, load_heat, float(o["wp_heiz_max_kw"]),
                             float(o["wp_heiz_anteil_verschiebbar"]), int(o["wp_heiz_fenster_h"]),
                             1.0 + float(o["wp_heiz_mehrverbrauch_prozent"]) / 100.0, spread)

    new = [imp[h] + d_ww[h] + d_h[h] for h in range(n)]
    grid_new = [grid[h] + d_ww[h] + d_h[h] for h in range(n)]
    i_ww.update(kwh_netz=sum(g_ww), stunden=sum(1 for x in is_ww if x))
    i_h.update(kwh_netz=sum(g_heat), stunden=0)
    info = {
        "wp_netz_kwh": sum(grid),
        "kwh_verschoben": i_ww["kwh_verschoben"] + i_h["kwh_verschoben"],
        "kwh_mehrverbrauch": i_ww["kwh_mehrverbrauch"] + i_h["kwh_mehrverbrauch"],
        "ersparnis_eur": i_ww["ersparnis_eur"] + i_h["ersparnis_eur"],
        "heizung": i_h,
        "warmwasser": i_ww,
    }
    return new, grid, grid_new, g_ww, info


# --------------------------------------------------------------------------- Simulation
def available(o: dict, ev, soc, wp) -> dict:
    """Welche Maßnahmen sind laut Einstellungen vorhanden und haben Daten?"""
    return {
        "ev": bool(o.get("e_auto_vorhanden")) and any(v > 0 for v in ev),
        "wp": bool(o.get("waermepumpe_vorhanden")) and any(v > 0 for v in wp),
        "akku": bool(o.get("hausakku_vorhanden")) and any(s is not None for s in soc),
    }


def simulate(hours, imp, exp, ev, soc, spot, o, tz: ZoneInfo, wp=None, wp_pmax=None, pv=None) -> dict:
    """hours: Unix-Stundenbeginn; imp/exp/ev/wp/pv: kWh je Stunde; soc: % oder None;
    wp_pmax: Spitzenleistung WP in W oder None; spot: €/MWh oder None."""
    n = len(hours)
    zeros = [0.0] * n
    wp = wp if wp is not None else zeros
    wp_pmax = wp_pmax if wp_pmax is not None else [None] * n
    pv = pv if pv is not None else zeros
    price = [dyn_price_ct(s, o) if s is not None else None for s in spot]
    have = available(o, ev, soc, wp)

    # --- jede Maßnahme isoliert (Vergleich jeweils gegen "dyn")
    imp_ev, ev_info = shift_ev(hours, imp, ev, price, o, tz) if have["ev"] else (imp, dict(EMPTY_EV))
    if have["wp"]:
        imp_wp, wp_grid, wp_grid_new, wp_ww, wp_info = shift_heatpump(hours, imp, wp, wp_pmax, price, o)
    else:
        imp_wp, wp_grid, wp_grid_new, wp_ww, wp_info = imp, zeros, zeros, zeros, dict(EMPTY_WP)
    if have["akku"]:
        imp_bat, bat_info, bat_in, bat_gain = battery_arbitrage(hours, imp, soc, price, o)
    else:
        imp_bat, bat_info, bat_in, bat_gain = imp, dict(EMPTY_BAT), zeros, zeros

    # --- alle Maßnahmen kombiniert: erst WP, dann E-Auto, dann Akku auf dem Rest
    imp_all = imp_wp
    all_ev, all_bat = dict(EMPTY_EV), dict(EMPTY_BAT)
    if have["ev"]:
        imp_all, all_ev = shift_ev(hours, imp_all, ev, price, o, tz)
    if have["akku"]:
        imp_all, all_bat, _, _ = battery_arbitrage(hours, imp_all, soc, price, o)

    fix_ct = float(o["festpreis_arbeitspreis_ct_brutto"])
    fix_h = fix_fixed_eur_year(o) / HOURS_PER_YEAR
    dyn_h = dyn_fixed_eur_year(o) / HOURS_PER_YEAR
    feed_ct = float(o["einspeiseverguetung_ct"])

    rows = []
    missing = 0
    for k in range(n):
        if price[k] is None:
            missing += 1
            continue
        p = price[k]
        cost = lambda e: e * p / 100.0 + dyn_h  # noqa: E731
        rows.append(
            {
                "t": hours[k],
                "imp": imp[k],
                "exp": exp[k],
                "pv": pv[k],
                "ev": ev[k],
                "soc": soc[k],
                "spot": spot[k] / 10.0,
                "p": p,
                "fix": imp[k] * fix_ct / 100.0 + fix_h,
                "dyn": cost(imp[k]),
                "dyn_ev": cost(imp_ev[k]),
                "dyn_wp": cost(imp_wp[k]),
                "dyn_bat": cost(imp_bat[k]),
                "dyn_all": cost(imp_all[k]),
                "imp_all": imp_all[k],
                "feed": exp[k] * feed_ct / 100.0,
                "wp_grid": wp_grid[k],
                "wp_grid_new": wp_grid_new[k],
                "wp_ww": wp_ww[k],
                "bat_in": bat_in[k],
                "bat_gain": bat_gain[k],
            }
        )

    return {
        "rows": rows,
        "missing_hours": missing,
        "vorhanden": have,
        "ev": ev_info,
        "akku": bat_info,
        "wp": wp_info,
        "alle": {"ev": all_ev, "akku": all_bat},
    }


SUM_KEYS = ("imp", "exp", "pv", "ev", "fix", "dyn", "dyn_ev", "dyn_wp", "dyn_bat", "dyn_all", "imp_all",
            "feed", "wp_grid", "wp_grid_new", "wp_ww", "bat_in", "bat_gain")


def aggregate(rows: list[dict], tz: ZoneInfo, key: str) -> list[dict]:
    """key: 'day' oder 'month' (lokale Zeit)."""
    out: dict[str, dict] = {}
    for r in rows:
        local = dt.datetime.fromtimestamp(r["t"], tz)
        k = local.strftime("%Y-%m-%d") if key == "day" else local.strftime("%Y-%m")
        a = out.get(k)
        if a is None:
            a = out[k] = {"key": k, "pw": 0.0, "pw_wp": 0.0, "spot_sum": 0.0, "hours": 0,
                          **{s: 0.0 for s in SUM_KEYS}}
        for s in SUM_KEYS:
            a[s] += r[s]
        a["pw"] += r["imp"] * r["p"]
        a["pw_wp"] += r["wp_grid"] * r["p"]
        a["spot_sum"] += r["spot"]
        a["hours"] += 1
    result = []
    for a in out.values():
        a["preis_gewichtet_ct"] = a["pw"] / a["imp"] if a["imp"] > 0.01 else None
        a["preis_wp_ct"] = a["pw_wp"] / a["wp_grid"] if a["wp_grid"] > 0.01 else None
        a["spot_mittel_ct"] = a["spot_sum"] / a["hours"] if a["hours"] else None
        del a["pw"], a["pw_wp"], a["spot_sum"]
        result.append(a)
    result.sort(key=lambda x: x["key"])
    return result


def pv_poor_days(daily: list[dict], threshold_kwh: float) -> dict:
    """Akku-Netzladung an PV-armen Tagen auswerten (nur sinnvoll mit PV-Statistik)."""
    poor = [d for d in daily if d["pv"] < threshold_kwh]
    with_charge = [d for d in poor if d["bat_in"] > 0.1]
    return {
        "schwelle_kwh": threshold_kwh,
        "tage_gesamt": len(daily),
        "tage_pv_arm": len(poor),
        "tage_mit_netzladung": len(with_charge),
        "kwh_netzladung": sum(d["bat_in"] for d in poor),
        "ersparnis_eur": sum(d["bat_gain"] for d in poor),  # dem Ladetag zugeordnet
        "kwh_netzladung_gesamt": sum(d["bat_in"] for d in daily),
    }


def hour_profile(rows: list[dict], tz: ZoneInfo) -> list[dict]:
    """Mittlerer Preis und mittlerer Bezug je Stunde des Tages."""
    keys = ("p", "imp", "imp_all", "wp_grid", "wp_grid_new", "bat_in")
    acc = [{"h": h, "n": 0, **{k: 0.0 for k in keys}} for h in range(24)]
    for r in rows:
        a = acc[dt.datetime.fromtimestamp(r["t"], tz).hour]
        for k in keys:
            a[k] += r[k]
        a["n"] += 1
    for a in acc:
        if a["n"]:
            for k in keys:
                a[k] /= a["n"]
    return acc
