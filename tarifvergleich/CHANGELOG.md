# Changelog

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
