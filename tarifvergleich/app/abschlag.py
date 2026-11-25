"""Abschlag Ist/Soll: Netzbezug im laufenden Vertragsjahr gegen die vereinbarte Jahresmenge.

Zwei Varianten:
- saisonal: die Jahresmenge wird nach dem eigenen Monatsprofil verteilt (Ø Tagesbezug des jeweils
  letzten vollständigen gleichnamigen Monats). Das Profil lernt mit: jeder abgeschlossene Monat ersetzt
  den Vorjahresmonat.
- linear: gleichmäßig je Tag.
Prognose = Ist / bisheriger Soll-Anteil, d. h. die bisherige Abweichung wird auf den Rest übertragen.
"""
from __future__ import annotations

import calendar
import datetime as dt
from zoneinfo import ZoneInfo

from .sim import parse_price_history, price_at


def _add_year(d: dt.date) -> dt.date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:  # 29.02.
        return d.replace(year=d.year + 1, day=28)


def contract_start(o: dict) -> dt.date | None:
    s = (o.get("abschlag_vertragsbeginn") or "").strip()
    if s:
        return dt.date.fromisoformat(s)
    hist = [str(e).split("=", 1)[0].strip() for e in (o.get("bezugspreis_historie") or []) if "=" in str(e)]
    return max(dt.date.fromisoformat(d) for d in hist) if hist else None


def month_profile(daily: dict[dt.date, float], first: dt.date, today: dt.date) -> tuple[dict[int, float], dict[int, str]]:
    """Ø Tagesbezug je Kalendermonat aus dem jeweils jüngsten vollständigen Monat."""
    avg, src = {}, {}
    for back in range(1, 25):
        y, m = today.year, today.month - back
        while m <= 0:
            y, m = y - 1, m + 12
        if m in avg:
            continue
        m_first = dt.date(y, m, 1)
        n = calendar.monthrange(y, m)[1]
        if m_first < first:
            continue
        avg[m] = sum(daily.get(m_first + dt.timedelta(days=i), 0.0) for i in range(n)) / n
        src[m] = f"{y}-{m:02d}"
    if avg:
        mean = sum(avg.values()) / len(avg)
        for m in range(1, 13):
            avg.setdefault(m, mean)
    else:
        avg = {m: 1.0 for m in range(1, 13)}
    return avg, src


def compute(hours: list[int], imp: list[float], o: dict, tz: ZoneInfo, end_ts: int) -> dict | None:
    eur_m = float(o.get("abschlag_eur_monat") or 0)
    kwh_y = float(o.get("abschlag_kwh_jahr") or 0)
    start = contract_start(o)
    if eur_m <= 0 or kwh_y <= 0 or not start or not hours:
        return None
    stop = _add_year(start)
    ndays = (stop - start).days
    s_ts = int(dt.datetime.combine(start, dt.time(0), tz).timestamp())

    daily: dict[dt.date, float] = {}
    for h, v in zip(hours, imp):
        d = dt.datetime.fromtimestamp(h, tz).date()
        daily[d] = daily.get(d, 0.0) + v
    first = dt.datetime.fromtimestamp(hours[0], tz).date()
    now_local = dt.datetime.fromtimestamp(end_ts, tz)
    today = now_local.date()
    avg, src = month_profile(daily, first, today)

    days = [start + dt.timedelta(days=i) for i in range(ndays)]
    w_raw = [avg[d.month] for d in days]
    tot = sum(w_raw) or 1.0
    w = [x / tot for x in w_raw]

    # bisher abgelaufener Anteil (heute anteilig nach abgeschlossenen Stunden)
    if today >= stop:
        el_days, w_el, idx_today = float(ndays), 1.0, ndays
    elif today < start:
        return None
    else:
        idx_today = (today - start).days
        frac = (now_local - dt.datetime.combine(today, dt.time(0), tz)).total_seconds() / 86400.0
        el_days = idx_today + frac
        w_el = sum(w[:idx_today]) + w[idx_today] * frac

    hist = parse_price_history(o.get("bezugspreis_historie"), o["festpreis_arbeitspreis_ct_brutto"], tz)
    grund = float(o["festpreis_grundpreis_eur_jahr_brutto"])
    ist_kwh = ist_ct = 0.0
    for h, v in zip(hours, imp):
        if s_ts <= h < end_ts:
            ist_kwh += v
            ist_ct += v * price_at(hist, h)
    ist_eur = ist_ct / 100.0 + grund * el_days / ndays
    price_now = price_at(hist, end_ts)
    pay_year = eur_m * 12.0
    pay_sofar = pay_year * el_days / ndays

    def variant(share: float) -> dict:
        soll = kwh_y * share
        prog = ist_kwh / share if share > 0 else None
        prog_eur = prog * price_now / 100.0 + grund if prog is not None else None
        return {
            "soll_kwh": soll,
            "abw_kwh": ist_kwh - soll,
            "abw_pct": (ist_kwh / soll - 1.0) * 100.0 if soll > 0 else None,
            "prog_kwh": prog,
            "prog_eur": prog_eur,
            "saldo_eur": prog_eur - pay_year if prog_eur is not None else None,  # > 0 = Nachzahlung
            "abschlag_passend": prog_eur / 12.0 if prog_eur is not None else None,
        }

    saison, linear = variant(w_el), variant(el_days / ndays)

    # Tagesverlauf (kumuliert) für das Diagramm
    verlauf, cum_w, cum_ist = [], 0.0, 0.0
    ps, pl = saison["prog_kwh"], linear["prog_kwh"]
    for i, d in enumerate(days):
        cum_w += w[i]
        lin = (i + 1) / ndays
        row = {"d": d.isoformat(), "soll_s": kwh_y * cum_w, "soll_l": kwh_y * lin, "ist": None, "prog_s": None, "prog_l": None}
        if i <= idx_today:  # daily[heute] enthält die bisher abgeschlossenen Stunden
            cum_ist += daily.get(d, 0.0)
            row["ist"] = cum_ist
        if i >= idx_today and ps is not None:
            row["prog_s"] = ist_kwh + (ps - ist_kwh) * max(0.0, cum_w - w_el) / max(1e-9, 1.0 - w_el)
            row["prog_l"] = ist_kwh + (pl - ist_kwh) * max(0.0, lin - el_days / ndays) / max(1e-9, 1.0 - el_days / ndays)
        verlauf.append(row)

    # Monate im Vertragsjahr (bisher)
    monate: dict[str, dict] = {}
    for i, d in enumerate(days[: min(idx_today + 1, ndays)]):
        k = d.strftime("%Y-%m")
        part = (el_days - idx_today) if d == today else 1.0
        m = monate.setdefault(k, {"key": k, "ist": 0.0, "soll_s": 0.0, "soll_l": 0.0, "voll": True})
        m["ist"] += daily.get(d, 0.0)
        m["soll_s"] += kwh_y * w[i] * part
        m["soll_l"] += kwh_y / ndays * part
        if d == today:
            m["voll"] = False

    return {
        "start": start.isoformat(),
        "ende": (stop - dt.timedelta(days=1)).isoformat(),
        "tage": ndays,
        "tag": el_days,
        "abschlag_monat": eur_m,
        "kwh_jahr": kwh_y,
        "abschlag_jahr": pay_year,
        "abschlag_bisher": pay_sofar,
        "arbeitspreis_ct": price_now,
        "grundpreis_eur_jahr": grund,
        "ist_kwh": ist_kwh,
        "ist_eur": ist_eur,
        "saison": saison,
        "linear": linear,
        "unsicher": el_days < 60,
        "linear_anzeigen": bool(o.get("abschlag_linear_anzeigen", True)),
        "profil": [{"monat": m, "kwh_tag": avg[m], "quelle": src.get(m)} for m in range(1, 13)],
        "monate": list(monate.values()),
        "verlauf": verlauf,
    }
