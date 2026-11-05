"""Zugriff auf Home Assistant über die Supervisor-Proxy-API (WebSocket + REST)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os

import aiohttp

_LOG = logging.getLogger(__name__)

TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
WS_URL = os.environ.get("HA_WS_URL", "ws://supervisor/core/websocket")
API_URL = os.environ.get("HA_API_URL", "http://supervisor/core/api")


class HAError(Exception):
    """Fehler bei der Kommunikation mit Home Assistant."""


def ts_of(value) -> int:
    """Zeitstempel aus HA-Statistik (ms-float, s-float oder ISO-String) -> Unix-Sekunden."""
    if isinstance(value, str):
        return int(dt.datetime.fromisoformat(value).timestamp())
    value = float(value)
    if value > 1e11:  # Millisekunden
        value /= 1000.0
    return int(round(value))


class HAClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._id = 0
        self._lock = asyncio.Lock()
        self._meta_variant: int | None = None

    async def _connect(self) -> None:
        ws = await self._session.ws_connect(WS_URL, max_msg_size=0, heartbeat=30)
        msg = await ws.receive_json(timeout=30)
        if msg.get("type") != "auth_required":
            raise HAError(f"Unerwartete Begrüßung: {msg}")
        await ws.send_json({"type": "auth", "access_token": TOKEN})
        msg = await ws.receive_json(timeout=30)
        if msg.get("type") != "auth_ok":
            await ws.close()
            raise HAError(f"Authentifizierung fehlgeschlagen: {msg}")
        self._ws = ws
        _LOG.info("WebSocket-Verbindung zu Home Assistant hergestellt (HA %s)", msg.get("ha_version"))

    async def close(self) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    async def call(self, payload: dict, timeout: float = 180):
        async with self._lock:
            last_err: Exception | None = None
            for _attempt in range(2):
                try:
                    if self._ws is None or self._ws.closed:
                        await self._connect()
                    self._id += 1
                    msg_id = self._id
                    await self._ws.send_json({"id": msg_id, **payload})
                    while True:
                        msg = await self._ws.receive_json(timeout=timeout)
                        if msg.get("id") != msg_id or msg.get("type") != "result":
                            continue
                        if not msg.get("success"):
                            raise HAError(str(msg.get("error")))
                        return msg.get("result")
                except HAError:
                    raise
                except (aiohttp.ClientError, asyncio.TimeoutError, TypeError, ValueError, ConnectionError) as err:
                    last_err = err
                    _LOG.warning("WebSocket-Fehler (%s) – verbinde neu", err)
                    if self._ws is not None:
                        await self._ws.close()
                    self._ws = None
            raise HAError(f"WebSocket-Aufruf fehlgeschlagen: {last_err}")

    # ------------------------------------------------------------------ Statistik
    async def statistics(
        self,
        statistic_ids: list[str],
        start: dt.datetime,
        end: dt.datetime,
        types: list[str],
        units: dict | None = None,
    ) -> dict[str, list[dict]]:
        """Stündliche Langzeitstatistik."""
        if not statistic_ids:
            return {}
        payload = {
            "type": "recorder/statistics_during_period",
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "statistic_ids": statistic_ids,
            "period": "hour",
            "types": types,
        }
        if units:
            payload["units"] = units
        return await self.call(payload) or {}

    async def import_statistics(self, metadata: dict, stats: list[dict]) -> None:
        """Externe Statistik (source:id) importieren. Probiert Metadaten-Varianten
        für unterschiedliche HA-Versionen durch und merkt sich die passende."""
        variants = [
            lambda m: m,
            lambda m: {k: v for k, v in m.items() if k != "has_mean"},
            lambda m: {k: v for k, v in m.items() if k != "unit_class"},
            lambda m: {k: v for k, v in m.items() if k not in ("unit_class", "mean_type")},
        ]
        order = list(range(len(variants)))
        if self._meta_variant is not None:
            order.remove(self._meta_variant)
            order.insert(0, self._meta_variant)
        last_err: Exception | None = None
        for idx in order:
            try:
                await self.call(
                    {"type": "recorder/import_statistics", "metadata": variants[idx](metadata), "stats": stats}
                )
                self._meta_variant = idx
                return
            except HAError as err:
                last_err = err
                _LOG.debug("import_statistics Variante %s abgelehnt: %s", idx, err)
        raise HAError(f"Statistik-Import für {metadata.get('statistic_id')} fehlgeschlagen: {last_err}")

    async def clear_statistics(self, statistic_ids: list[str]) -> None:
        await self.call({"type": "recorder/clear_statistics", "statistic_ids": statistic_ids})

    # ------------------------------------------------------------------ REST
    async def delete_state(self, entity_id: str) -> None:
        try:
            async with self._session.delete(
                f"{API_URL}/states/{entity_id}",
                headers={"Authorization": f"Bearer {TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status not in (200, 404):
                    _LOG.debug("Löschen von %s: HTTP %s", entity_id, resp.status)
        except aiohttp.ClientError as err:
            _LOG.debug("Löschen von %s fehlgeschlagen: %s", entity_id, err)

    async def set_state(self, entity_id: str, state, attributes: dict) -> None:
        url = f"{API_URL}/states/{entity_id}"
        headers = {"Authorization": f"Bearer {TOKEN}"}
        try:
            async with self._session.post(
                url,
                json={"state": state, "attributes": attributes},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status >= 400:
                    _LOG.warning("Setzen von %s fehlgeschlagen: HTTP %s %s", entity_id, resp.status, await resp.text())
        except aiohttp.ClientError as err:
            _LOG.warning("Setzen von %s fehlgeschlagen: %s", entity_id, err)
