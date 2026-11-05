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

## Ansichten und Szenarien

**Übersicht:** Festpreis gegen dynamisch mit dem Verbrauch wie gemessen.

**Lastverschiebung** (nur sichtbar, wenn mindestens ein Gerät als vorhanden eingestellt ist). Jede
Maßnahme wird einzeln gegen „dynamisch wie gemessen“ gerechnet:

| Maßnahme | Modell |
|---|---|
| E-Auto | Netzanteil abendlicher/nächtlicher Ladevorgänge in die billigsten Stunden bis zur Fertig-Uhrzeit |
| Wärmepumpe – Warmwasser | Stunden mit Spitzenleistung ≥ Schwelle gelten als WW-Bereitung; der WW-Anteil (Verbrauch über dem Heizbedarf der Nachbarstunden) ist voll verschiebbar (±6 h, bis WW-Leistung) |
| Wärmepumpe – Heizung | ein Anteil des Heizstroms ist über die Speichermasse verschiebbar (±3 h, bis Heizleistung), mit Mehrverbrauch |
| Hausakku | lädt in billigen Stunden aus dem Netz, soweit der reale Ladestand Platz lässt (an PV-armen Tagen viel), und ersetzt teureren Bezug innerhalb von 24 h |
| Alle Maßnahmen | WP, dann E-Auto, dann Akku auf dem verbleibenden Bezug |

Gerechnet wird mit den bekannten Day-Ahead-Preisen (perfekte Voraussicht innerhalb des Fensters) –
das ist eine **Obergrenze** dessen, was evcc o. ä. erreicht.

## Ergebnisse in Home Assistant

Sensoren (werden stündlich gesetzt):

- `sensor.tarifvergleich_kosten_festpreis`, `_kosten_dynamisch`
- `sensor.tarifvergleich_kosten_dynamisch_optimiert` (alle Maßnahmen, Einzeleffekte als Attribute)
- `sensor.tarifvergleich_ersparnis`, `_ersparnis_optimiert`, `_ersparnis_monat`
- `sensor.tarifvergleich_preis_aktuell` (EUR/kWh, Attribute `heute`/`morgen` mit Stundenpreisen)

Langzeitstatistiken (rückwirkend, für Statistik-Diagramm-Karten):

- `tarifvergleich:kosten_festpreis`, `tarifvergleich:kosten_dynamisch`,
  `tarifvergleich:kosten_dynamisch_optimiert` (alle Maßnahmen; Summe in EUR)
- `tarifvergleich:preis_dynamisch` (Mittelwert ct/kWh)

## Grenzen

- Stundenauflösung statt 15 min, kleine Abweichungen möglich.
- Netzentgelte/Umlagen werden konstant über den ganzen Zeitraum angesetzt (Preisblatt 2026).
- Neukundenbonus u. ä. Einmaleffekte sind nicht enthalten.
- Die Einspeisevergütung ist in beiden Tarifen gleich und nur informativ ausgewiesen.
