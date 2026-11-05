"""Kostenrechnung Festpreis vs. dynamischer Tarif und Lastverschiebungs-Szenario.

Alle Energiewerte in kWh je Stunde, Preise in ct/kWh brutto, Kosten in EUR.
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


# --------------------------------------------------------------------------- Verschiebung
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


def battery_arbitrage(hours, imp, soc, price, o):
    """Akku in günstigen Stunden aus dem Netz laden und damit späteren teuren
    Netzbezug ersetzen. Freie Kapazität kommt aus dem realen SoC-Verlauf, so dass
    PV-Ladung nicht verdrängt wird. Greedy über die Paare mit größtem Spread."""
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

    new = [imp[h] + charged[h] - discharged[h] for h in range(n)]
    return new, {
        "kwh_netzladung": total_in,
        "kwh_entladen": total_in * eta,
        "vollzyklen": total_in / cap if cap else 0.0,
        "ersparnis_eur": saved_ct / 100.0,
    }


def shift_heatpump(hours, imp, wp, price, o):
    """Wärmepumpe: ein Anteil ihres Netzbezugs jeder Stunde darf innerhalb von
    ±Fenster Stunden in billigere Stunden wandern (Vorheizen/Nachlaufen über die
    thermische Speichermasse). Verschobene Energie kostet einen Mehrverbrauch
    (schlechtere JAZ bei höherer Vorlauftemperatur). Teuerste Stunden zuerst."""
    n = len(hours)
    frac = float(o["wp_anteil_verschiebbar"])
    win = int(o["wp_fenster_h"])
    maxkw = float(o["wp_max_kw"])
    pen = 1.0 + float(o["wp_mehrverbrauch_prozent"]) / 100.0
    spread = float(o["wp_mindest_spread_ct"])

    new = list(imp)
    # Netzanteil der WP: WP-Verbrauch, soweit in der Stunde Netzbezug da war
    grid = [max(0.0, min(wp[h], imp[h])) for h in range(n)]
    grid_new = list(grid)
    load = [max(0.0, w) for w in wp]
    moved = extra = saved_ct = 0.0

    order = sorted((h for h in range(n) if price[h] is not None and grid[h] > 0.01), key=lambda h: -price[h])
    for j in order:
        m = frac * grid[j]
        cands = [
            i for i in range(max(0, j - win), min(n, j + win + 1))
            if i != j and price[i] is not None and price[i] * pen + spread <= price[j]
        ]
        cands.sort(key=lambda i: price[i])
        for i in cands:
            if m <= 0.01:
                break
            cap = maxkw - load[i]
            if cap <= 0.01:
                continue
            x = min(m, cap / pen)
            load[i] += x * pen
            load[j] -= x
            new[i] += x * pen
            new[j] -= x
            grid_new[i] += x * pen
            grid_new[j] -= x
            m -= x
            moved += x
            extra += x * (pen - 1.0)
            saved_ct += x * (price[j] - price[i] * pen)

    info = {
        "wp_netz_kwh": sum(grid),
        "kwh_verschoben": moved,
        "kwh_mehrverbrauch": extra,
        "ersparnis_eur": saved_ct / 100.0,
    }
    return new, grid, grid_new, info


# --------------------------------------------------------------------------- Simulation
EMPTY_EV = {"sitzungen": 0, "kwh_verschoben": 0.0, "ersparnis_eur": 0.0}
EMPTY_BAT = {"kwh_netzladung": 0.0, "kwh_entladen": 0.0, "vollzyklen": 0.0, "ersparnis_eur": 0.0}


def _ev_and_battery(hours, base, ev, soc, price, o, tz):
    out, ev_info, bat_info = list(base), dict(EMPTY_EV), dict(EMPTY_BAT)
    if o.get("ev_verschiebung") and any(v > 0 for v in ev):
        out, ev_info = shift_ev(hours, out, ev, price, o, tz)
    if o.get("akku_arbitrage") and any(s is not None for s in soc):
        out, bat_info = battery_arbitrage(hours, out, soc, price, o)
    return out, ev_info, bat_info


def simulate(hours, imp, exp, ev, soc, spot, o, tz: ZoneInfo, wp=None) -> dict:
    """hours: Liste Unix-Stundenbeginn; imp/exp/ev/wp: kWh je Stunde; soc: % oder None;
    spot: €/MWh oder None."""
    n = len(hours)
    wp = wp if wp is not None else [0.0] * n
    price = [dyn_price_ct(s, o) if s is not None else None for s in spot]

    # Szenario A: E-Auto + Akku (unverändert gegenüber v0.1)
    opt, ev_info, bat_info = _ev_and_battery(hours, imp, ev, soc, price, o, tz)

    # Szenario B: nur Wärmepumpe – getrennt, damit der Effekt isoliert sichtbar ist
    imp_wp, wp_grid, wp_grid_new = list(imp), [0.0] * n, [0.0] * n
    wp_info = {"wp_netz_kwh": 0.0, "kwh_verschoben": 0.0, "kwh_mehrverbrauch": 0.0, "ersparnis_eur": 0.0}
    if o.get("wp_verschiebung") and any(v > 0 for v in wp):
        imp_wp, wp_grid, wp_grid_new, wp_info = shift_heatpump(hours, imp, wp, price, o)

    # Szenario C: alle Maßnahmen (erst WP, dann E-Auto + Akku auf dem Rest)
    imp_all, all_ev, all_bat = _ev_and_battery(hours, imp_wp, ev, soc, price, o, tz)

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
        rows.append(
            {
                "t": hours[k],
                "imp": imp[k],
                "opt": opt[k],
                "exp": exp[k],
                "ev": ev[k],
                "soc": soc[k],
                "spot": spot[k] / 10.0,
                "p": p,
                "fix": imp[k] * fix_ct / 100.0 + fix_h,
                "dyn": imp[k] * p / 100.0 + dyn_h,
                "dyn_opt": opt[k] * p / 100.0 + dyn_h,
                "feed": exp[k] * feed_ct / 100.0,
                "wp_grid": wp_grid[k],
                "wp_grid_new": wp_grid_new[k],
                "imp_wp": imp_wp[k],
                "dyn_wp": imp_wp[k] * p / 100.0 + dyn_h,
                "imp_all": imp_all[k],
                "dyn_all": imp_all[k] * p / 100.0 + dyn_h,
            }
        )

    return {
        "rows": rows,
        "missing_hours": missing,
        "ev": ev_info,
        "akku": bat_info,
        "wp": wp_info,
        "alle": {"ev": all_ev, "akku": all_bat},
    }


SUM_KEYS = ("imp", "opt", "exp", "fix", "dyn", "dyn_opt", "feed",
            "wp_grid", "wp_grid_new", "imp_wp", "dyn_wp", "imp_all", "dyn_all")


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


def hour_profile(rows: list[dict], tz: ZoneInfo) -> list[dict]:
    """Mittlerer Preis und mittlerer Bezug je Stunde des Tages."""
    keys = ("p", "imp", "opt", "wp_grid", "wp_grid_new")
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
