"""Optionen der App laden (/data/options.json, vom Supervisor geschrieben)."""
from __future__ import annotations

import json
import logging
import os

OPTIONS_PATH = os.environ.get("OPTIONS_PATH", "/data/options.json")
DATA_DIR = os.environ.get("DATA_DIR", "/data")

DEFAULTS: dict = {
    "startdatum": "2026-02-01",
    "statistik_netzbezug": "sensor.ebz_energy_in",
    "statistik_einspeisung": "sensor.ebz_energy_out",
    "festpreis_arbeitspreis_ct_brutto": 29.17,
    "festpreis_grundpreis_eur_jahr_brutto": 197.09,
    "mwst_prozent": 19.0,
    "netzentgelt_ct_netto": 7.17,
    "aufschlag_netznutzung_ct_netto": 1.559,
    "kwkg_umlage_ct_netto": 0.446,
    "offshore_umlage_ct_netto": 0.941,
    "stromsteuer_ct_netto": 2.05,
    "konzessionsabgabe_ct_netto": 1.59,
    "netz_grundpreis_eur_jahr_netto": 76.65,
    "dyn_aufschlag_ct_netto": 2.0,
    "dyn_grundgebuehr_eur_monat_brutto": 5.99,
    "messstelle_dyn_eur_jahr_brutto": 50.0,
    "einspeiseverguetung_ct": 7.9,
    "ev_verschiebung": True,
    "ev_statistiken": [],
    "ev_max_kw": 11.0,
    "ev_abend_ab_uhr": 15,
    "ev_fertig_bis_uhr": 7,
    "akku_arbitrage": True,
    "akku_soc_statistik": "",
    "akku_kapazitaet_kwh": 29.3,
    "akku_max_netzladeleistung_kw": 6.0,
    "akku_wirkungsgrad": 0.9,
    "akku_mindest_spread_ct": 3.0,
    "log_level": "info",
}


def load() -> dict:
    opts = dict(DEFAULTS)
    try:
        with open(OPTIONS_PATH, encoding="utf-8") as fh:
            opts.update(json.load(fh))
    except FileNotFoundError:
        logging.getLogger(__name__).warning(
            "%s nicht gefunden – verwende Standardwerte", OPTIONS_PATH
        )
    return opts
