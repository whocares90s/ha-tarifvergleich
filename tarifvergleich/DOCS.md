# Tarifvergleich

Rechnet für jede Stunde aus, was euer tatsächlicher Netzbezug (eBZ-Zähler, HA-Langzeitstatistik)
mit einem **dynamischen Tarif nach Tibber-Formel** gekostet hätte – im Vergleich zum Festpreis.
Rückwirkend ab dem Startdatum und danach stündlich fortlaufend.

## Preisformel

```
dynamisch [ct/kWh brutto] = (Börsenpreis Day-Ahead DE-LU + Tibber-Aufschlag + Netzentgelt
                             + §19-Aufschlag + KWKG + Offshore + Stromsteuer + Konzessionsabgabe) × (1 + MwSt)
Grundpreis dynamisch      = Tibber-Grundgebühr + Netz-Grundpreis × (1 + MwSt) + Smart-Meter-Messstelle
```

Börsenpreise: Energy-Charts (Fraunhofer ISE; Daten Bundesnetzagentur | SMARD.de, CC BY 4.0),
Fallback aWATTar. Viertelstundenpreise werden zu Stundenmitteln zusammengefasst, weil die
HA-Statistik stündlich ist.

## Szenarien

| Szenario | Was passiert |
|---|---|
| Festpreis | Bezug × Arbeitspreis + Grundpreis anteilig |
| Dynamisch | gleicher Bezug, stündlicher dynamischer Preis |
| Dynamisch + Verschiebung | E-Auto-Netzladung abends/nachts in die billigsten Stunden bis zur Fertig-Uhrzeit; Akku lädt in billigen Stunden aus dem Netz, wenn der reale Ladestand Platz hatte, und ersetzt teuren Bezug innerhalb von 24 h |
| Dynamisch + WP-Verschiebung (eigener Reiter) | Ein Anteil des WP-Netzbezugs jeder Stunde wandert innerhalb ±Fenster in billigere Stunden (Vorheizen/Nachlaufen), mit Mehrverbrauch für die verschobene Energie und begrenzt durch die WP-Höchstleistung |
| Alle Maßnahmen | WP-Verschiebung, danach E-Auto und Akku auf dem verbleibenden Bezug |

Die Verschiebung rechnet mit den bekannten Day-Ahead-Preisen (perfekte Voraussicht innerhalb des
Fensters) und ist damit eine **Obergrenze** dessen, was evcc o. ä. erreicht.

## Ergebnisse in Home Assistant

Sensoren (werden stündlich gesetzt):

- `sensor.tarifvergleich_kosten_festpreis`, `_kosten_dynamisch`, `_kosten_dynamisch_optimiert`
- `sensor.tarifvergleich_ersparnis`, `_ersparnis_optimiert`, `_ersparnis_monat`
- `sensor.tarifvergleich_preis_aktuell` (EUR/kWh, Attribute `heute`/`morgen` mit Stundenpreisen)

Langzeitstatistiken (rückwirkend, für Statistik-Diagramm-Karten):

- `tarifvergleich:kosten_festpreis`, `tarifvergleich:kosten_dynamisch`,
  `tarifvergleich:kosten_dynamisch_optimiert` (Summe in EUR)
- `tarifvergleich:preis_dynamisch` (Mittelwert ct/kWh)

## Grenzen

- Stundenauflösung statt 15 min, kleine Abweichungen möglich.
- Netzentgelte/Umlagen werden konstant über den ganzen Zeitraum angesetzt (Preisblatt 2026).
- Neukundenbonus u. ä. Einmaleffekte sind nicht enthalten.
- Die Einspeisevergütung ist in beiden Tarifen gleich und nur informativ ausgewiesen.
