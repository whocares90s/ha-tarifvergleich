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

from . import config, prices, sim, storage
from .ha import HAClient, HAError, ts_of

_LOG = logging.getLogger("tarifvergleich")
TZ = prices.BERLIN
STATIC = Path(__file__).parent / "static"
STATE_FILE = Path(config.DATA_DIR) / "state.json"
PORT = int(os.environ.get("PORT", "8099"))

STAT_IDS = {
    "fix": ("tarifvergleich:kosten_festpreis", "Tarifvergleich Kosten Festpreis"),
    "dyn": ("tarifvergleich:kosten_dynamisch", "Tarifvergleich Kosten dynamisch"),
    "dyn_all": ("tarifvergleich:kosten_dynamisch_optimiert", "Tarifvergleich Kosten dynamisch + Lastverschiebung"),
}
PRICE_STAT = ("tarifvergleich:preis_dynamisch", "Tarifvergleich Preis dynamisch")
STORAGE_STAT = ("tarifvergleich:speicher_ersparnis", "Tarifvergleich Ersparnis Batteriespeicher")
# aus v0.2.0, werden einmalig entfernt
OBSOLETE_STATS = ["tarifvergleich:kosten_dynamisch_wp", "tarifvergleich:kosten_dynamisch_alle"]
OBSOLETE_STATES = ["sensor.tarifvergleich_kosten_dynamisch_wp", "sensor.tarifvergleich_ersparnis_wp",
                   "sensor.tarifvergleich_kosten_dynamisch_alle"]

OPTION_KEYS = (
    "startdatum", "einspeiseverguetung_ct", "pv_wenig_kwh_tag",
    "e_auto_vorhanden", "ev_max_kw", "ev_abend_ab_uhr", "ev_fertig_bis_uhr",
    "waermepumpe_vorhanden", "wp_heiz_anteil_verschiebbar", "wp_heiz_fenster_h", "wp_heiz_max_kw",
    "wp_heiz_mehrverbrauch_prozent", "ww_schwelle_w", "ww_fenster_h", "ww_max_kw", "ww_mehrverbrauch_prozent",
    "wp_mindest_spread_ct",
    "hausakku_vorhanden", "akku_kapazitaet_kwh", "akku_max_netzladeleistung_kw", "akku_wirkungsgrad",
    "akku_mindest_spread_ct",
)


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


def _r(v, nd: int = 2):
    return round(v, nd) if isinstance(v, (int, float)) else v


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


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
        self.storage: dict = {}
        self.storage_error: str | None = None
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

        price_start = start_date
        if self._storage_enabled():
            price_start = min(start_date, dt.date.fromisoformat(o["speicher_bestand_start"]))
        await prices.refresh(self.store, self.session, price_start)

        ev_ids = [s for s in (o.get("ev_statistiken") or []) if s] if o.get("e_auto_vorhanden") else []
        wp_id = (o.get("wp_statistik") or "") if o.get("waermepumpe_vorhanden") else ""
        wp_p_id = (o.get("wp_leistung_statistik") or "") if o.get("waermepumpe_vorhanden") else ""
        soc_id = (o.get("akku_soc_statistik") or "") if o.get("hausakku_vorhanden") else ""
        pv_ids = [s for s in (o.get("pv_statistiken") or []) if s]

        energy_ids = [o["statistik_netzbezug"], o["statistik_einspeisung"], *ev_ids, *pv_ids]
        if wp_id:
            energy_ids.append(wp_id)
        stats = await self.ha.statistics(energy_ids, start, end, ["change"], {"energy": "kWh"})
        soc_stats = await self.ha.statistics([soc_id], start, end, ["mean"]) if soc_id else {}
        pmax_stats = await self.ha.statistics([wp_p_id], start, end, ["max"], {"power": "W"}) if wp_p_id else {}

        imp_d = _series(stats, o["statistik_netzbezug"], "change")
        if not imp_d:
            raise HAError(f"Keine Statistik für {o['statistik_netzbezug']} gefunden")
        exp_d = _series(stats, o["statistik_einspeisung"], "change")
        ev_ds = [_series(stats, sid, "change") for sid in ev_ids]
        pv_ds = [_series(stats, sid, "change") for sid in pv_ids]
        wp_d = _series(stats, wp_id, "change") if wp_id else {}
        soc_d = _series(soc_stats, soc_id, "mean") if soc_id else {}
        pmax_d = _series(pmax_stats, wp_p_id, "max") if wp_p_id else {}

        first = max(int(start.timestamp()), min(imp_d))
        hours = list(range(first, int(end.timestamp()), 3600))
        spot_d = self.store.get(first, int(end.timestamp()))

        # negative "change"-Werte (Zählertausch/Reset) ignorieren
        pos = lambda d, h: max(0.0, d.get(h, 0.0))  # noqa: E731
        imp = [pos(imp_d, h) for h in hours]
        exp = [pos(exp_d, h) for h in hours]
        ev = [sum(pos(d, h) for d in ev_ds) for h in hours]
        pv = [sum(pos(d, h) for d in pv_ds) for h in hours]
        wp = [pos(wp_d, h) for h in hours]
        wp_pmax = [pmax_d.get(h) for h in hours]
        soc = [soc_d.get(h) for h in hours]
        spot = [spot_d.get(h) for h in hours]

        res = sim.simulate(hours, imp, exp, ev, soc, spot, o, TZ, wp=wp, wp_pmax=wp_pmax, pv=pv)
        rows = res["rows"]
        self.result = res
        self.daily = sim.aggregate(rows, TZ, "day")
        self.monthly = sim.aggregate(rows, TZ, "month")
        self.profile = sim.hour_profile(rows, TZ)
        self.summary = self._summary(res, bool(pv_ids))
        s = self.summary
        _LOG.info(
            "Rechenlauf: %d h, fix %.2f €, dyn %.2f €, Effekte E-Auto %.2f / WP %.2f / Akku %.2f / alle %.2f €",
            len(rows), s["fix"], s["dyn"], s["effekt_ev"], s["effekt_wp"], s["effekt_akku"], s["effekt_alle"],
        )
        await self._cleanup_obsolete()
        await self._publish_states()
        await self._import_statistics(rows)

        try:
            await self._run_storage(end)
            self.storage_error = None
        except Exception as err:  # noqa: BLE001 – Speicher-Auswertung darf den Rest nicht stören
            self.storage_error = f"{type(err).__name__}: {err}"
            _LOG.exception("Speicher-Auswertung fehlgeschlagen")

    # ------------------------------------------------------------------ Speicher
    def _storage_enabled(self) -> bool:
        o = self.opts
        return bool(o.get("hausakku_vorhanden") and o.get("akku_statistik_laden")
                    and o.get("akku_statistik_entladen") and o.get("speicher_bestand_start"))

    async def _battery_live(self) -> dict:
        out = {}
        for key, opt in (("bestand", "akku_leistung_bestand"), ("erweiterung", "akku_leistung_erweiterung")):
            eid = self.opts.get(opt) or ""
            if not eid.endswith("_power"):
                continue
            base = eid[: -len("_power")]
            vals = {}
            for name, suffix in (("soh", "_state_of_health"), ("zyklen", "_cycle_count"), ("soc", "_state_of_charge")):
                st = await self.ha.get_state(base + suffix)
                try:
                    vals[name] = float(st) if st not in (None, "unknown", "unavailable") else None
                except ValueError:
                    vals[name] = None
            out[key] = vals
        return out

    async def _run_storage(self, end: dt.datetime) -> None:
        o = self.opts
        if not self._storage_enabled():
            self.storage = {}
            return
        start = dt.datetime.combine(dt.date.fromisoformat(o["speicher_bestand_start"]), dt.time(0), TZ)
        pv_ids = [s for s in (o.get("pv_statistiken") or []) if s]
        roof_id = o.get("pv_dach_statistik") or ""
        ids = [o["akku_statistik_laden"], o["akku_statistik_entladen"], o["statistik_netzbezug"],
               o["statistik_einspeisung"], *pv_ids]
        if roof_id and roof_id not in ids:
            ids.append(roof_id)
        stats = await self.ha.statistics(ids, start, end, ["change"], {"energy": "kWh"})
        p_ids = [x for x in (o.get("akku_leistung_bestand"), o.get("akku_leistung_erweiterung")) if x]
        p_stats = await self.ha.statistics(p_ids, start, end, ["mean"], {"power": "W"}) if p_ids else {}

        in_d = _series(stats, o["akku_statistik_laden"], "change")
        if not in_d:
            raise HAError(f"Keine Statistik für {o['akku_statistik_laden']} gefunden")
        out_d = _series(stats, o["akku_statistik_entladen"], "change")
        imp_d = _series(stats, o["statistik_netzbezug"], "change")
        exp_d = _series(stats, o["statistik_einspeisung"], "change")
        pv_ds = [_series(stats, sid, "change") for sid in pv_ids]
        roof_d = _series(stats, roof_id, "change") if roof_id else None
        w_old_d = _series(p_stats, o.get("akku_leistung_bestand") or "", "mean")
        w_ext_d = _series(p_stats, o.get("akku_leistung_erweiterung") or "", "mean")

        first = max(int(start.timestamp()), min(in_d))
        hours = list(range(first, int(end.timestamp()), 3600))
        spot_d = self.store.get(first, int(end.timestamp()))
        pos = lambda d, h: max(0.0, d.get(h, 0.0))  # noqa: E731
        opt_pos = lambda d, h: max(0.0, d[h]) if h in d else None  # noqa: E731
        absw = lambda d, h: abs(d[h]) if h in d else None  # noqa: E731

        res = storage.analyze(
            hours,
            [pos(in_d, h) for h in hours],
            [pos(out_d, h) for h in hours],
            [sum(pos(d, h) for d in pv_ds) for h in hours],
            [opt_pos(imp_d, h) for h in hours],
            [opt_pos(exp_d, h) for h in hours],
            [spot_d.get(h) for h in hours],
            [absw(w_old_d, h) for h in hours],
            [absw(w_ext_d, h) for h in hours],
            o, TZ,
            pv_roof=[pos(roof_d, h) for h in hours] if roof_d is not None else None,
        )
        live = await self._battery_live()
        daily = storage.aggregate(res["rows"], TZ, "day")
        self.storage = {
            "summary": storage.summary(res, o, TZ, live),
            "monthly": storage.aggregate(res["rows"], TZ, "month"),
            "daily": [{"key": d["key"], "s_old": d["s_old"], "s_ext": d["s_ext"], "marg": d["marg"],
                       "s_fix": d["s_fix"], "s_pv_roof": d["s_pv_roof"], "s_pv_other": d["s_pv_other"]}
                      for d in daily],
        }
        s = self.storage["summary"]
        _LOG.info("Speicher: %.0f kWh entladen, Ersparnis %.2f € (Bestand %.2f €, Erweiterung anteilig %.2f €, "
                  "Zusatznutzen %.2f €)", s["b_out"], s["s_fix"], s["s_old"], s["s_ext"],
                  s["erweiterung"]["zusatznutzen"].get("seit_inbetriebnahme_eur", 0.0))
        await self._publish_storage()
        await self._import_storage_statistics(res["rows"])

    async def _publish_storage(self) -> None:
        s = self.storage["summary"]
        b, e = s["bestand"], s["erweiterung"]
        z = e["zusatznutzen"]
        attrs = {
            "friendly_name": "Tarifvergleich Ersparnis Batteriespeicher",
            "unit_of_measurement": "EUR", "device_class": "monetary", "icon": "mdi:home-battery",
            "seit": s["von"], "entladen_kwh": round(s["b_out"], 1), "ct_je_kwh": _r(s["ct_je_kwh"]),
            "letzte_12_monate": round(s["letzte_12_monate"], 2),
            "bestand_eur": round(s["s_old"], 2),
            "erweiterung_anteilig_eur": round(s["s_ext"], 2),
            "erweiterung_zusatznutzen_eur": _r(z.get("seit_inbetriebnahme_eur")),
            "erweiterung_zusatznutzen_pro_jahr_eur": _r(z.get("pro_jahr_eur")),
        }
        if b.get("amortisation"):
            attrs["bestand_amortisiert_prozent"] = round(b["amortisation"]["prozent"], 1)
        if e.get("amortisation"):
            attrs["erweiterung_amortisiert_prozent"] = round(e["amortisation"]["prozent"], 1)
        await self.ha.set_state("sensor.tarifvergleich_speicher_ersparnis", round(s["s_fix"], 2), attrs)

        a = s.get("anlage") or {}
        if a.get("amortisation"):
            am = a["amortisation"]
            await self.ha.set_state("sensor.tarifvergleich_pv_anlage_ersparnis", round(a["ersparnis"], 2), {
                "friendly_name": "Tarifvergleich Ersparnis PV-Anlage gesamt",
                "unit_of_measurement": "EUR", "device_class": "monetary", "icon": "mdi:solar-power-variant",
                "seit": s["von"], "investition_eur": round(am["kosten"], 2),
                "amortisiert_prozent": round(am["prozent"], 1), "pro_jahr_eur": round(a["pro_jahr"], 2),
                "rest_jahre": _r(am.get("rest_jahre"), 1), "break_even": am.get("break_even"),
                "dach_pv_eur": round(s["s_pv_roof"], 2), "speicher_eur": round(s["s_fix"], 2),
                "weitere_pv_eur": round(s["s_pv_other"], 2),
            })

    async def _import_storage_statistics(self, rows: list[dict]) -> None:
        if not rows:
            return
        sig = self._signature()
        state = _read_state()
        full = state.get("speicher_sig") != sig or state.get("speicher_first_t") != rows[0]["t"]
        cutoff = rows[-1]["t"] - 72 * 3600 if not full else -1
        cum, stats = 0.0, []
        for r in rows:
            cum += r["s_fix"]
            if r["t"] >= cutoff:
                stats.append({"start": _iso(r["t"]), "sum": round(cum, 4), "state": round(cum, 4)})
        meta = {"has_mean": False, "mean_type": 0, "has_sum": True, "name": STORAGE_STAT[1],
                "source": "tarifvergleich", "statistic_id": STORAGE_STAT[0], "unit_of_measurement": "EUR",
                "unit_class": None}
        await self.ha.import_statistics(meta, stats)
        state.update(speicher_sig=sig, speicher_first_t=rows[0]["t"])
        _write_state(state)

    def _summary(self, res: dict, has_pv: bool) -> dict:
        rows = res["rows"]
        tot = {k: sum(r[k] for r in rows) for k in sim.SUM_KEYS}
        pw = sum(r["imp"] * r["p"] for r in rows)
        pw_all = sum(r["imp_all"] * r["p"] for r in rows)
        pw_wp = sum(r["wp_grid"] * r["p"] for r in rows)
        pw_wp_new = sum(r["wp_grid_new"] * r["p"] for r in rows)
        month_key = dt.datetime.now(TZ).strftime("%Y-%m")
        cur = next((m for m in self.monthly if m["key"] == month_key), None)
        return {
            **tot,
            "von": dt.datetime.fromtimestamp(rows[0]["t"], TZ).isoformat() if rows else None,
            "bis": dt.datetime.fromtimestamp(rows[-1]["t"] + 3600, TZ).isoformat() if rows else None,
            "stunden": len(rows),
            "stunden_ohne_preis": res["missing_hours"],
            "vorhanden": res["vorhanden"],
            "ersparnis": tot["fix"] - tot["dyn"],
            "ersparnis_alle": tot["fix"] - tot["dyn_all"],
            "effekt_ev": tot["dyn"] - tot["dyn_ev"],
            "effekt_wp": tot["dyn"] - tot["dyn_wp"],
            "effekt_akku": tot["dyn"] - tot["dyn_bat"],
            "effekt_alle": tot["dyn"] - tot["dyn_all"],
            "preis_gewichtet_ct": pw / tot["imp"] if tot["imp"] > 0 else None,
            "preis_gewichtet_alle_ct": pw_all / tot["imp_all"] if tot["imp_all"] > 0 else None,
            "preis_wp_ct": pw_wp / tot["wp_grid"] if tot["wp_grid"] > 0 else None,
            "preis_wp_neu_ct": pw_wp_new / tot["wp_grid_new"] if tot["wp_grid_new"] > 0 else None,
            "monat": cur,
            "ev": res["ev"],
            "akku": res["akku"],
            "wp": res["wp"],
            "alle": res["alle"],
            "pv_arm": sim.pv_poor_days(self.daily, float(self.opts.get("pv_wenig_kwh_tag", 5.0))) if has_pv else None,
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

    async def _cleanup_obsolete(self) -> None:
        state = _read_state()
        if state.get("cleanup_v030"):
            return
        for eid in OBSOLETE_STATES:
            await self.ha.delete_state(eid)
        try:
            await self.ha.clear_statistics(OBSOLETE_STATS)
        except HAError as err:
            _LOG.warning("Alte Statistiken konnten nicht entfernt werden: %s", err)
        state["cleanup_v030"] = True
        _write_state(state)

    async def _publish_states(self) -> None:
        s = self.summary
        have = s["vorhanden"]
        common = {"zeitraum_von": s["von"], "zeitraum_bis": s["bis"], "netzbezug_kwh": round(s["imp"], 2)}

        def money(name: str, icon: str, extra: dict | None = None) -> dict:
            return {"friendly_name": name, "unit_of_measurement": "EUR", "device_class": "monetary",
                    "icon": icon, **common, **(extra or {})}

        effects = {}
        if have["ev"]:
            effects["effekt_e_auto"] = round(s["effekt_ev"], 2)
        if have["wp"]:
            effects["effekt_waermepumpe"] = round(s["effekt_wp"], 2)
        if have["akku"]:
            effects["effekt_hausakku"] = round(s["effekt_akku"], 2)

        await self.ha.set_state("sensor.tarifvergleich_kosten_festpreis", round(s["fix"], 2),
                                money("Tarifvergleich Kosten Festpreis", "mdi:cash-lock"))
        await self.ha.set_state("sensor.tarifvergleich_kosten_dynamisch", round(s["dyn"], 2),
                                money("Tarifvergleich Kosten dynamisch", "mdi:cash-clock",
                                      {"preis_gewichtet_ct": _r(s["preis_gewichtet_ct"])}))
        await self.ha.set_state("sensor.tarifvergleich_ersparnis", round(s["ersparnis"], 2),
                                money("Tarifvergleich Ersparnis dynamisch", "mdi:piggy-bank"))
        if any(have.values()):
            await self.ha.set_state("sensor.tarifvergleich_kosten_dynamisch_optimiert", round(s["dyn_all"], 2),
                                    money("Tarifvergleich Kosten dynamisch + Lastverschiebung", "mdi:cash-fast",
                                          {"preis_gewichtet_ct": _r(s["preis_gewichtet_alle_ct"]), **effects}))
            await self.ha.set_state("sensor.tarifvergleich_ersparnis_optimiert", round(s["ersparnis_alle"], 2),
                                    money("Tarifvergleich Ersparnis dynamisch + Lastverschiebung",
                                          "mdi:piggy-bank-outline", effects))
        m = s.get("monat")
        if m:
            await self.ha.set_state("sensor.tarifvergleich_ersparnis_monat", round(m["fix"] - m["dyn"], 2),
                                    money("Tarifvergleich Ersparnis laufender Monat", "mdi:calendar-month",
                                          {"monat": m["key"], "kosten_festpreis": round(m["fix"], 2),
                                           "kosten_dynamisch": round(m["dyn"], 2),
                                           "kosten_dynamisch_lastverschiebung": round(m["dyn_all"], 2)}))

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
        state = _read_state()
        full = state.get("signature") != sig or state.get("first_t") != rows[0]["t"]
        cutoff = rows[-1]["t"] - 72 * 3600 if not full else -1

        keys = dict(STAT_IDS)
        if not any(self.summary["vorhanden"].values()):
            keys.pop("dyn_all")
        for key, (sid, name) in keys.items():
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

        state.update(signature=sig, first_t=rows[0]["t"])
        _write_state(state)
        _LOG.info("Statistiken importiert (%s)", "vollständig" if full else "letzte 72 h")


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
                "optionen": {k: engine.opts.get(k) for k in OPTION_KEYS},
                "speicher": engine.storage,
                "status": {"letzter_lauf": engine.last_run, "fehler": engine.last_error,
                           "speicher_fehler": engine.storage_error, "laeuft": engine.busy.locked()},
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
