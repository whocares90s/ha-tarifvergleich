# Changelog

## 0.3.2

- „Tibber-Aufschlag“ heißt jetzt „Strombeschaffungskosten“; Standard 1,807 ct netto (= 2,15 ct brutto laut Tibber)

## 0.3.1

- Akku-Ersparnis wird dem Ladetag zugeordnet (vorher Verzerrung, wenn über Mitternacht entladen wurde)
- Standard-Grenze „PV-armer Tag“ auf 10 kWh

## 0.3.0

- Ansichten neu: **Übersicht** (Festpreis vs. dynamisch wie gemessen) und **Lastverschiebung** (E-Auto, Wärmepumpe, Hausakku, alle kombiniert)
- Einstellungen `e_auto_vorhanden`, `waermepumpe_vorhanden`, `hausakku_vorhanden` – nicht vorhandene Geräte werden nicht gerechnet und ausgeblendet
- Wärmepumpe: Warmwasser (erkannt an der Spitzenleistung) und Heizung getrennt modelliert – Warmwasser voll verschiebbar (±6 h, 5 kW), Heizung anteilig (±3 h, 3 kW)
- Jede Maßnahme wird einzeln gegen „dynamisch wie gemessen“ gerechnet, zusätzlich alle kombiniert
- Hausakku: Auswertung der Netzladung an PV-armen Tagen (optionale PV-Statistiken)
- `sensor.tarifvergleich_kosten_dynamisch_optimiert` / `_ersparnis_optimiert` = alle Maßnahmen, Einzeleffekte als Attribute; Sensoren und Statistiken aus 0.2.0 werden entfernt
- Umbenannte Optionen: `ev_verschiebung` → `e_auto_vorhanden`, `wp_verschiebung` → `waermepumpe_vorhanden`, `akku_arbitrage` → `hausakku_vorhanden`, `wp_anteil_verschiebbar`/`wp_fenster_h`/`wp_max_kw`/`wp_mehrverbrauch_prozent` → `wp_heiz_*`

## 0.2.0

- Neues Szenario **Wärmepumpen-Verschiebung** mit eigenem Reiter „Wärmepumpe“ (getrennt von E-Auto/Akku)
- Zusätzliches Szenario „Alle Maßnahmen“ (WP + E-Auto + Akku)
- Neue Sensoren `sensor.tarifvergleich_kosten_dynamisch_wp`, `_ersparnis_wp`, `_kosten_dynamisch_alle`
- Neue Langzeitstatistiken `tarifvergleich:kosten_dynamisch_wp`, `tarifvergleich:kosten_dynamisch_alle`
- Energy-Charts: bei Rate-Limit (HTTP 429) erst warten und erneut versuchen, dann aWATTar

## 0.1.0

- Erste Version: Backtest ab Startdatum, stündliche Fortschreibung, Festpreis vs. dynamisch (Tibber-Formel)
- Szenario Lastverschiebung (E-Auto + Akku-Netzladung)
- Ingress-Oberfläche, HA-Sensoren, rückwirkende Langzeitstatistiken
