"""Tarifvergleich – Einstiegspunkt: stündlicher Rechenlauf + Web-Oberfläche (Ingress)."""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path

import aiohttp
from aiohttp import web

from . import config, prices, sim
from .ha import HAClient, HAError, ts_of

_LOG = logging.getLogger("tarifvergleich")
TZ = prices.BERLIN
STATIC = Path(__file__).parent / "static"
STATE_FILE = Path(config.DATA_DIR) / "state.json"
PORT = int(os.environ.get("PORT", "8099"))

STAT_IDS = {
    "fix": ("tarifvergleich:kosten_festpreis", "Tarifvergleich Kosten Festpreis"),
    "dyn": ("tarifvergleich:kosten_dynamisch", "Tarifvergleich Kosten dynamisch"),
    "dyn_opt": ("tarifvergleich:kosten_dynamisch_optimiert", "Tarifvergleich Kosten dynamisch + Verschiebung"),
}
PRICE_STAT = ("tarifvergleich:preis_dynamisch", "Tarifvergleich Preis dynamisch")


def _iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def _series(stats: dict, sid: str, key: str) -> dict[int, float]:
    out: dict[int, float] = {}
    for row in stats.get(sid, []) or []:
        val = row.get(key)
        if val is None:
            continue
        out[ts_of(row["start"])] = float(val)
    return out


class Engine:
    def __init__(self, session: aiohttp.ClientSession, opts: dict) -> None:
        self.session = session
        self.opts = opts
        self.ha = HAClient(session)
        self.store = prices.PriceStore(str(Path(config.DATA_DIR) / "tarifvergleich.db"))
        self.result: dict | None = None
        self.daily: list[dict] = []
        self.monthly: list[dict] = []
        self.profile: list[dict] = []
        self.summary: dict = {}
        self.last_error: str | None = None
        self.last_run: str | None = None
        self.busy = asyncio.Lock()

    # ------------------------------------------------------------------ Lauf
    async def run(self) -> None:
        async with self.busy:
            try:
                await self._run()
                self.last_error = None
            except Exception as err:  # noqa: BLE001 – Lauf darf nie die App beenden
                self.last_error = f"{type(err).__name__}: {err}"
                _LOG.exception("Rechenlauf fehlgeschlagen")
            self.last_run = dt.datetime.now(TZ).isoformat(timespec="seconds")

    async def _run(self) -> None:
        o = self.opts
        start_date = dt.date.fromisoformat(o["startdatum"])
        start = dt.datetime.combine(start_date, dt.time(0), TZ)
        now = dt.datetime.now(dt.timezone.utc)
        end = now.replace(minute=0, second=0, microsecond=0)  # nur abgeschlossene Stunden

        await prices.refresh(self.store, self.session, start_date)

        ev_ids = list(o.get("ev_statistiken") or []) if o.get("ev_verschiebung") else []
        energy_ids = [o["statistik_netzbezug"], o["statistik_einspeisung"], *ev_ids]
        stats = await self.ha.statistics(energy_ids, start, end, ["change"], {"energy": "kWh"})
        soc_stats = {}
        if o.get("akku_arbitrage") and o.get("akku_soc_statistik"):
            soc_stats = await self.ha.statistics([o["akku_soc_statistik"]], start, end, ["mean"])

        imp_d = _series(stats, o["statistik_netzbezug"], "change")
        if not imp_d:
            raise HAError(f"Keine Statistik für {o['statistik_netzbezug']} gefunden")
        exp_d = _series(stats, o["statistik_einspeisung"], "change")
        ev_ds = [_series(stats, sid, "change") for sid in ev_ids]
        soc_d = _series(soc_stats, o.get("akku_soc_statistik", ""), "mean")

        first = max(int(start.timestamp()), min(imp_d))
        hours = list(range(first, int(end.timestamp()), 3600))
        spot_d = self.store.get(first, int(end.timestamp()))

        # negative "change"-Werte (Zählertausch/Reset) ignorieren
        imp = [max(0.0, imp_d.get(h, 0.0)) for h in hours]
        exp = [max(0.0, exp_d.get(h, 0.0)) for h in hours]
        ev = [sum(max(0.0, d.get(h, 0.0)) for d in ev_ds) for h in hours]
        soc = [soc_d.get(h) for h in hours]
        spot = [spot_d.get(h) for h in hours]

        res = sim.simulate(hours, imp, exp, ev, soc, spot, o, TZ)
        rows = res["rows"]
        self.result = res
        self.daily = sim.aggregate(rows, TZ, "day")
        self.monthly = sim.aggregate(rows, TZ, "month")
        self.profile = sim.hour_profile(rows, TZ)
        self.summary = self._summary(res, hours)
        _LOG.info(
            "Rechenlauf: %d h, fix %.2f €, dyn %.2f €, dyn+opt %.2f €",
            len(rows), self.summary["fix"], self.summary["dyn"], self.summary["dyn_opt"],
        )
        await self._publish_states()
        await self._import_statistics(rows)

    def _summary(self, res: dict, hours: list[int]) -> dict:
        rows = res["rows"]
        tot = {k: sum(r[k] for r in rows) for k in ("imp", "opt", "exp", "fix", "dyn", "dyn_opt", "feed")}
        pw = sum(r["imp"] * r["p"] for r in rows)
        pw_opt = sum(r["opt"] * r["p"] for r in rows)
        month_key = dt.datetime.now(TZ).strftime("%Y-%m")
        cur = next((m for m in self.monthly if m["key"] == month_key), None)
        return {
            **tot,
            "von": dt.datetime.fromtimestamp(rows[0]["t"], TZ).isoformat() if rows else None,
            "bis": dt.datetime.fromtimestamp(rows[-1]["t"] + 3600, TZ).isoformat() if rows else None,
            "stunden": len(rows),
            "stunden_ohne_preis": res["missing_hours"],
            "ersparnis": tot["fix"] - tot["dyn"],
            "ersparnis_opt": tot["fix"] - tot["dyn_opt"],
            "preis_gewichtet_ct": pw / tot["imp"] if tot["imp"] > 0 else None,
            "preis_gewichtet_opt_ct": pw_opt / tot["opt"] if tot["opt"] > 0 else None,
            "monat": cur,
            "ev": res["ev"],
            "akku": res["akku"],
        }

    # ------------------------------------------------------------------ Ausgabe an HA
    def future_prices(self, hours_ahead: int = 48) -> list[dict]:
        now_h = int(dt.datetime.now(dt.timezone.utc).timestamp()) // 3600 * 3600
        data = self.store.get(now_h - 3600 * dt.datetime.now(TZ).hour, now_h + 3600 * hours_ahead)
        return [
            {"start": dt.datetime.fromtimestamp(h, TZ).isoformat(), "t": h, "spot_ct": p / 10.0,
             "preis_ct": sim.dyn_price_ct(p, self.opts)}
            for h, p in sorted(data.items())
        ]

    async def _publish_states(self) -> None:
        s = self.summary
        common = {"zeitraum_von": s["von"], "zeitraum_bis": s["bis"], "netzbezug_kwh": round(s["imp"], 2)}

        def money(name: str, value: float, icon: str, extra: dict | None = None) -> dict:
            return {
                "friendly_name": name, "unit_of_measurement": "EUR", "device_class": "monetary",
                "icon": icon, **common, **(extra or {}),
            }

        await self.ha.set_state("sensor.tarifvergleich_kosten_festpreis", round(s["fix"], 2),
                                money("Tarifvergleich Kosten Festpreis", s["fix"], "mdi:cash-lock"))
        await self.ha.set_state("sensor.tarifvergleich_kosten_dynamisch", round(s["dyn"], 2),
                                money("Tarifvergleich Kosten dynamisch", s["dyn"], "mdi:cash-clock",
                                      {"preis_gewichtet_ct": _r(s["preis_gewichtet_ct"])}))
        await self.ha.set_state("sensor.tarifvergleich_kosten_dynamisch_optimiert", round(s["dyn_opt"], 2),
                                money("Tarifvergleich Kosten dynamisch + Verschiebung", s["dyn_opt"], "mdi:cash-fast",
                                      {"preis_gewichtet_ct": _r(s["preis_gewichtet_opt_ct"]),
                                       "ev_kwh_verschoben": _r(s["ev"]["kwh_verschoben"]),
                                       "akku_kwh_netzladung": _r(s["akku"]["kwh_netzladung"])}))
        await self.ha.set_state("sensor.tarifvergleich_ersparnis", round(s["ersparnis"], 2),
                                money("Tarifvergleich Ersparnis dynamisch", s["ersparnis"], "mdi:piggy-bank"))
        await self.ha.set_state("sensor.tarifvergleich_ersparnis_optimiert", round(s["ersparnis_opt"], 2),
                                money("Tarifvergleich Ersparnis dynamisch + Verschiebung", s["ersparnis_opt"],
                                      "mdi:piggy-bank-outline"))
        m = s.get("monat")
        if m:
            await self.ha.set_state("sensor.tarifvergleich_ersparnis_monat", round(m["fix"] - m["dyn"], 2),
                                    money("Tarifvergleich Ersparnis laufender Monat", 0, "mdi:calendar-month",
                                          {"monat": m["key"], "kosten_festpreis": round(m["fix"], 2),
                                           "kosten_dynamisch": round(m["dyn"], 2),
                                           "kosten_dynamisch_optimiert": round(m["dyn_opt"], 2)}))

        fut = self.future_prices()
        now_h = int(dt.datetime.now(dt.timezone.utc).timestamp()) // 3600 * 3600
        cur = next((p for p in fut if p["t"] == now_h), None)
        if cur:
            today = dt.datetime.now(TZ).date()
            await self.ha.set_state(
                "sensor.tarifvergleich_preis_aktuell",
                round(cur["preis_ct"] / 100.0, 4),
                {
                    "friendly_name": "Tarifvergleich Strompreis dynamisch",
                    "unit_of_measurement": "EUR/kWh",
                    "icon": "mdi:currency-eur",
                    "boersenpreis_ct": round(cur["spot_ct"], 3),
                    "festpreis_ct": self.opts["festpreis_arbeitspreis_ct_brutto"],
                    "heute": [{"start": p["start"], "preis_ct": round(p["preis_ct"], 2)} for p in fut
                              if dt.datetime.fromisoformat(p["start"]).date() == today],
                    "morgen": [{"start": p["start"], "preis_ct": round(p["preis_ct"], 2)} for p in fut
                               if dt.datetime.fromisoformat(p["start"]).date() == today + dt.timedelta(days=1)],
                },
            )

    def _signature(self) -> str:
        return hashlib.sha256(json.dumps(self.opts, sort_keys=True).encode()).hexdigest()

    async def _import_statistics(self, rows: list[dict]) -> None:
        if not rows:
            return
        sig = self._signature()
        state = {}
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            pass
        full = state.get("signature") != sig or state.get("first_t") != rows[0]["t"]
        cutoff = rows[-1]["t"] - 72 * 3600 if not full else -1

        for key, (sid, name) in STAT_IDS.items():
            cum = 0.0
            stats = []
            for r in rows:
                cum += r[key]
                if r["t"] >= cutoff:
                    stats.append({"start": _iso(r["t"]), "sum": round(cum, 4), "state": round(cum, 4)})
            meta = {"has_mean": False, "mean_type": 0, "has_sum": True, "name": name, "source": "tarifvergleich",
                    "statistic_id": sid, "unit_of_measurement": "EUR", "unit_class": None}
            await self.ha.import_statistics(meta, stats)

        stats = [{"start": _iso(r["t"]), "mean": round(r["p"], 3), "min": round(r["p"], 3), "max": round(r["p"], 3)}
                 for r in rows if r["t"] >= cutoff]
        meta = {"has_mean": True, "mean_type": 1, "has_sum": False, "name": PRICE_STAT[1], "source": "tarifvergleich",
                "statistic_id": PRICE_STAT[0], "unit_of_measurement": "ct/kWh", "unit_class": None}
        await self.ha.import_statistics(meta, stats)

        STATE_FILE.write_text(json.dumps({"signature": sig, "first_t": rows[0]["t"]}), encoding="utf-8")
        _LOG.info("Statistiken importiert (%s)", "vollständig" if full else "letzte 72 h")


def _r(v, nd: int = 2):
    return round(v, nd) if isinstance(v, (int, float)) else v


# --------------------------------------------------------------------------- Web
def build_web(engine: Engine) -> web.Application:
    app = web.Application()

    async def index(_req):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    async def api_data(_req):
        return web.json_response(
            {
                "summary": engine.summary,
                "monthly": engine.monthly,
                "daily": engine.daily,
                "profile": engine.profile,
                "future": engine.future_prices(),
                "tarif": sim.breakdown(engine.opts),
                "optionen": {k: engine.opts[k] for k in ("startdatum", "ev_verschiebung", "akku_arbitrage",
                                                          "ev_max_kw", "ev_abend_ab_uhr", "ev_fertig_bis_uhr",
                                                          "akku_kapazitaet_kwh", "akku_max_netzladeleistung_kw",
                                                          "akku_wirkungsgrad", "akku_mindest_spread_ct",
                                                          "einspeiseverguetung_ct")},
                "status": {"letzter_lauf": engine.last_run, "fehler": engine.last_error,
                           "laeuft": engine.busy.locked()},
            },
            dumps=lambda o: json.dumps(o, default=str),
        )

    async def api_day(req):
        day = req.query.get("date", "")
        try:
            d = dt.date.fromisoformat(day)
        except ValueError:
            return web.json_response({"error": "date=YYYY-MM-DD erwartet"}, status=400)
        start = int(dt.datetime.combine(d, dt.time(0), TZ).timestamp())
        end = int(dt.datetime.combine(d + dt.timedelta(days=1), dt.time(0), TZ).timestamp())
        rows = [r for r in (engine.result or {}).get("rows", []) if start <= r["t"] < end]
        return web.json_response({"date": day, "rows": rows})

    async def api_refresh(_req):
        if not engine.busy.locked():
            asyncio.create_task(engine.run())
        return web.json_response({"ok": True})

    app.router.add_get("/", index)
    app.router.add_get("/api/data", api_data)
    app.router.add_get("/api/day", api_day)
    app.router.add_post("/api/refresh", api_refresh)
    return app


async def _main() -> None:
    opts = config.load()
    logging.basicConfig(
        level=getattr(logging, str(opts.get("log_level", "info")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    async with aiohttp.ClientSession() as session:
        engine = Engine(session, opts)
        runner = web.AppRunner(build_web(engine))
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        _LOG.info("Web-Oberfläche auf Port %d", PORT)
        try:
            while True:
                await engine.run()
                now = dt.datetime.now(TZ)
                nxt = (now + dt.timedelta(hours=1)).replace(minute=5, second=0, microsecond=0)
                await asyncio.sleep(max(60.0, (nxt - now).total_seconds()))
        finally:
            await engine.ha.close()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(_main())
