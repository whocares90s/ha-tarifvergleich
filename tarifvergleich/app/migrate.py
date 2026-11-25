"""Einmalige Übernahme: Stundenwerte einer Quellstatistik (z. B. Wechselrichter-Netzzähler)
in die Langzeitstatistik eines Zählers (z. B. eBZ) für die Zeit vor dessen Live-Daten.

Ziel-Zeilen vor der ersten Zeile mit Zählerstand („Live-Beginn“) werden durch Stundenwerte der
Quelle ersetzt. Die Summen werden vom letzten vorhandenen Summenwert vor dem Live-Beginn
rückwärts gerechnet, damit alle späteren Zeilen unverändert anschließen.
"""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from .ha import HAClient, HAError, ts_of

_LOG = logging.getLogger(__name__)


def _iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


async def copy_hourly(ha: HAClient, src: str, dst: str, tz: ZoneInfo, dry_run: bool = True) -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    far = dt.datetime(2015, 1, 1, tzinfo=dt.timezone.utc)
    dst_rows = (await ha.statistics([dst], far, now, ["sum", "state", "change"]) or {}).get(dst, [])
    live = [r for r in dst_rows if r.get("state") is not None]
    if not live:
        raise HAError(f"{dst}: keine Zeile mit Zählerstand gefunden – Live-Beginn unbekannt")
    b = ts_of(live[0]["start"])
    pre = [r for r in dst_rows if ts_of(r["start"]) < b and r.get("sum") is not None]
    if not pre:
        return {"ziel": dst, "hinweis": "keine Importzeilen vor Live-Beginn", "live_beginn": _iso(b)}

    anchor_t, anchor_sum = ts_of(pre[-1]["start"]), float(pre[-1]["sum"])
    first_t = ts_of(pre[0]["start"])
    first_change = float(pre[0].get("change") or 0.0)
    dst_total = anchor_sum - (float(pre[0]["sum"]) - first_change)

    day0 = dt.datetime.fromtimestamp(first_t, tz).replace(hour=0, minute=0, second=0, microsecond=0)
    src_rows = (await ha.statistics([src], day0, dt.datetime.fromtimestamp(b, dt.timezone.utc),
                                    ["change"], {"energy": "kWh"}) or {}).get(src, [])
    d = {ts_of(r["start"]): max(0.0, float(r["change"])) for r in src_rows if r.get("change") is not None}
    if not d:
        raise HAError(f"{src}: keine Stundenwerte im Zeitraum")
    start = min(d)
    hours = list(range(start, b, 3600))

    # Summen: am Anker exakt der bisherige Wert, davor rückwärts, danach (bis Live-Beginn) vorwärts
    sums: dict[int, float] = {}
    acc = anchor_sum
    for h in reversed([h for h in hours if h <= anchor_t]):
        sums[h] = acc
        acc -= d.get(h, 0.0)
    acc = anchor_sum
    for h in [h for h in hours if h > anchor_t]:
        acc += d.get(h, 0.0)
        sums[h] = acc

    # Abgleich mit den bisherigen (Tages-)Summen
    diffs = [abs(sums[ts_of(r["start"])] - float(r["sum"])) for r in pre if ts_of(r["start"]) in sums]
    live_first = live[0]
    gap = None
    if live_first.get("sum") is not None and (b - 3600) in sums:
        gap = float(live_first["sum"]) - float(live_first.get("change") or 0.0) - sums[b - 3600]

    report = {
        "quelle": src, "ziel": dst,
        "von": dt.datetime.fromtimestamp(start, tz).isoformat(),
        "live_beginn": dt.datetime.fromtimestamp(b, tz).isoformat(),
        "stunden": len(hours),
        "bisherige_zeilen_vorher": len(pre),
        "summe_quelle_kwh": round(sum(d.get(h, 0.0) for h in hours), 3),
        "summe_ziel_bisher_kwh": round(dst_total, 3),
        "max_abweichung_tagessumme_kwh": round(max(diffs), 3) if diffs else None,
        "anschluss_live_kwh": round(gap, 3) if gap is not None else None,
        "ziel_beginnt_vor_quelle": first_t < start,
        "geschrieben": False,
    }
    if dry_run:
        return report

    meta = {"has_mean": False, "mean_type": 0, "has_sum": True, "name": None, "source": "recorder",
            "statistic_id": dst, "unit_of_measurement": "kWh", "unit_class": "energy"}
    rows = [{"start": _iso(h), "sum": round(sums[h], 4)} for h in hours]
    for i in range(0, len(rows), 2000):
        await ha.import_statistics(meta, rows[i:i + 2000])
    report["geschrieben"] = True
    _LOG.info("Statistik %s: %d Stundenwerte aus %s übernommen", dst, len(rows), src)
    return report
