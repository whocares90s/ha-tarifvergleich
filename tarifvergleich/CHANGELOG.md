# Changelog

## 0.9.1

- Standard-Wirkungsgrad Akku 0,87 (Rundlauf inkl. Wechselrichter), Beschreibung ergänzt

## 0.9.0 – WattWert

- Neuer Name **WattWert** (Kennung `tarifvergleich` und Entitäts-IDs bleiben unverändert)
- PV-Anlage: **Autarkie**, **Eigenverbrauchsquote** und **CO₂** (stündliche CO₂-Intensität von Energy-Charts, Ersatzwert konfigurierbar)
- **Akku-Gesundheit**: SoH und Zyklen im Verlauf, SoH-Verlust je 100 Zyklen, Restlebensdauer, Kosten je Zyklus und Verschleiß je kWh
- **Monatsbericht** als Push-Nachricht am Monatsersten (`bericht_dienst`, `bericht_uhrzeit`), Vorschau und Testversand in der App
- Neue Optionen `co2_faktor_g_kwh`, `akku_zyklen_lebensdauer`, `akku_soh_ende_prozent`, `bericht_dienst`, `bericht_uhrzeit`

## 0.8.0

- Zeitfilter auch im Reiter PV & Speicher (Amortisation, Prognosen und Live-Werte bleiben „seit Inbetriebnahme“)
- Neuer Endpunkt `GET /api/storage_range?von=…&bis=…`

## 0.7.0

- Zeitfilter für Übersicht und Lastverschiebung: Gesamt, Jahr, Monat, Tag und freier Zeitraum, mit Blättern
- Auflösung passt sich an (Monate / Tage / Stunden), Kennzahlen und Tabellen gelten für den gewählten Zeitraum
- Neuer Endpunkt `GET /api/range?von=YYYY-MM-DD&bis=YYYY-MM-DD`

## 0.6.0

- Eine Datenquelle: alle Auswertungen basieren auf dem eBZ (Optionen `speicher_statistik_*` entfernt)
- Tarifvergleich ab Inbetriebnahme (Standard-Startdatum 2025-01-23)
- Festpreis im Tarifvergleich nach `bezugspreis_historie` (vorher immer aktueller Festpreis)
- `POST /api/migrate?src=…&dst=…&dry=1` – einmalige Übernahme von Stundenwerten in eine Zählerstatistik

## 0.5.1

- PV & Speicher nutzt eigene Netzzähler (Standard: Deye), weil die eBZ-Daten vor Feb. 2026 nur als Tageswerte importiert sind

## 0.5.0

- Reiter heißt jetzt **PV & Speicher** und zeigt zusätzlich die **gesamte PV-Anlage**: Ersparnis gegenüber
  „ohne PV“ (Eigenverbrauch × Bezugspreis + Einspeisung × Vergütung + Speicher-Mehrwert), aufgeteilt in
  Dach-PV, weitere PV (Garage) und Speicher
- Amortisation der Gesamtinvestition inkl. Break-even-Prognose; neuer Sensor `sensor.tarifvergleich_pv_anlage_ersparnis`
- Neue Optionen `pv_anlage_kosten_eur`, `pv_dach_statistik`, `pv_kosten_inkl_weitere_pv`
- Hinweis: Netzladung zum Kalibrieren kostet unterm Strich nur die Verluste

## 0.4.0

- Neuer Reiter **Speicher**: Ersparnis des Batteriespeichers seit Inbetriebnahme gegenüber PV ohne Speicher
  (Entladung × Bezugspreis − PV-Ladung × Einspeisevergütung − Netzladung × Bezugspreis), mit Bezugspreis-Historie
- Aufteilung Bestandsakku/Erweiterung: anteilig nach gemessener Leistung bzw. Kapazität, dazu der simulierte
  **Zusatznutzen** der Erweiterung (Bestand allein vs. beide) inkl. Hochrechnung pro Jahr und Modellgüte
- Amortisation je Speicher (optionale Anschaffungskosten), Live-Werte SoH/Zyklen/Ladestand
- Bewertung zusätzlich mit dynamischem Tarif; Börsenpreise werden dafür ab Speicher-Inbetriebnahme geladen
- Neuer Sensor `sensor.tarifvergleich_speicher_ersparnis`, Langzeitstatistik `tarifvergleich:speicher_ersparnis`

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
