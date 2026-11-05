"""Day-Ahead-Börsenpreise (DE-LU) holen und lokal cachen.

Quelle 1: Energy-Charts (Fraunhofer ISE, Daten: Bundesnetzagentur | SMARD.de, CC BY 4.0)
Quelle 2 (Fallback): aWATTar Marketdata API
Viertelstundenwerte werden zu Stundenmitteln (€/MWh) zusammengefasst.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from zoneinfo import ZoneInfo

import aiohttp

_LOG = logging.getLogger(__name__)
BERLIN = ZoneInfo("Europe/Berlin")
TIMEOUT = aiohttp.ClientTimeout(total=60)


class PriceStore:
    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS prices ("
            " hour INTEGER PRIMARY KEY,"  # Unix-Sekunden, Stundenbeginn (UTC)
            " eur_mwh REAL NOT NULL,"
            " n INTEGER NOT NULL,"
            " src TEXT)"
        )
        self._db.commit()

    def upsert(self, rows: dict[int, tuple[float, int]], src: str) -> None:
        self._db.executemany(
            "INSERT OR REPLACE INTO prices(hour, eur_mwh, n, src) VALUES (?, ?, ?, ?)",
            [(h, p, n, src) for h, (p, n) in rows.items()],
        )
        self._db.commit()

    def get(self, start_ts: int, end_ts: int) -> dict[int, float]:
        cur = self._db.execute(
            "SELECT hour, eur_mwh FROM prices WHERE hour >= ? AND hour < ? ORDER BY hour",
            (start_ts, end_ts),
        )
        return {int(h): float(p) for h, p in cur.fetchall()}

    def first_hour(self) -> int | None:
        row = self._db.execute("SELECT MIN(hour) FROM prices").fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def last_hour(self) -> int | None:
        row = self._db.execute("SELECT MAX(hour) FROM prices").fetchone()
        return int(row[0]) if row and row[0] is not None else None


def _aggregate(points) -> dict[int, tuple[float, int]]:
    buckets: dict[int, list[float]] = {}
    for ts, price in points:
        if price is None:
            continue
        hour = int(ts) // 3600 * 3600
        buckets.setdefault(hour, []).append(float(price))
    return {h: (sum(v) / len(v), len(v)) for h, v in buckets.items()}


async def _energy_charts(session: aiohttp.ClientSession, day_from: dt.date, day_to: dt.date):
    params = {"bzn": "DE-LU", "start": day_from.isoformat(), "end": day_to.isoformat()}
    async with session.get("https://api.energy-charts.info/price", params=params, timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    return list(zip(data.get("unix_seconds") or [], data.get("price") or []))


async def _awattar(session: aiohttp.ClientSession, day_from: dt.date, day_to: dt.date):
    start = dt.datetime.combine(day_from, dt.time(0), BERLIN)
    end = dt.datetime.combine(day_to + dt.timedelta(days=1), dt.time(0), BERLIN)
    params = {"start": int(start.timestamp() * 1000), "end": int(end.timestamp() * 1000)}
    async with session.get("https://api.awattar.de/v1/marketdata", params=params, timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    points = []
    for item in data.get("data") or []:
        s = int(item["start_timestamp"]) // 1000
        e = int(item["end_timestamp"]) // 1000
        # Einträge, die eine ganze Stunde abdecken, zählen einfach; 15-min-Einträge werden gemittelt
        step = 900 if e - s <= 900 else 3600
        for t in range(s, e, step):
            points.append((t, item["marketprice"]))
    return points


async def refresh(store: PriceStore, session: aiohttp.ClientSession, start_date: dt.date) -> None:
    """Fehlende Preise ab start_date bis morgen nachladen; letzte 2 Tage immer neu."""
    today = dt.datetime.now(BERLIN).date()
    first = store.first_hour()
    last = store.last_hour()
    if last is None or first is None or dt.datetime.fromtimestamp(first, BERLIN).date() > start_date:
        day = start_date - dt.timedelta(days=1)
    else:
        day = max(start_date, dt.datetime.fromtimestamp(last, BERLIN).date() - dt.timedelta(days=2))
    until = today + dt.timedelta(days=1)

    while day <= until:
        chunk_end = min(day + dt.timedelta(days=30), until)
        points, src = [], ""
        for attempt in range(3):
            try:
                points, src = await _energy_charts(session, day, chunk_end), "energy-charts"
                break
            except aiohttp.ClientResponseError as err:
                if err.status == 429 and attempt < 2:
                    await asyncio.sleep(10 * (attempt + 1))  # Rate-Limit: kurz warten
                    continue
                _LOG.warning("Energy-Charts %s–%s fehlgeschlagen (%s) – versuche aWATTar", day, chunk_end, err)
                break
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as err:
                _LOG.warning("Energy-Charts %s–%s fehlgeschlagen (%s) – versuche aWATTar", day, chunk_end, err)
                break
        if not points:
            try:
                points, src = await _awattar(session, day, chunk_end), "awattar"
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as err:
                _LOG.error("aWATTar %s–%s fehlgeschlagen: %s", day, chunk_end, err)
        rows = _aggregate(points)
        if rows:
            store.upsert(rows, src)
            _LOG.info("Preise %s–%s geladen (%d Stunden, %s)", day, chunk_end, len(rows), src)
        day = chunk_end + dt.timedelta(days=1)
        await asyncio.sleep(1)
