# Tarifvergleich – Home-Assistant-App

Simuliert, was der tatsächliche Netzbezug mit einem dynamischen Stromtarif (Tibber-Formel:
Börsenpreis + Netzentgelte, Umlagen, Steuern) gekostet hätte – im Vergleich zum Festpreis.
Details siehe [tarifvergleich/DOCS.md](tarifvergleich/DOCS.md).

## Installation

### Variante A – GitHub (empfohlen)

1. In Home Assistant: **Einstellungen → Apps → App-Store → ⋮ → Repositories** →
   `https://github.com/whocares90s/ha-tarifvergleich` eintragen.
2. „Tarifvergleich“ installieren, unter **Konfiguration** Werte prüfen, starten, „In Seitenleiste anzeigen“.
3. Updates: Version in `tarifvergleich/config.yaml` erhöhen, committen, pushen – HA bietet das Update dann an.

### Variante B – lokale App

Ordner `tarifvergleich/` nach `/addons/tarifvergleich/` auf dem HA-Host kopieren (z. B. mit der
Samba- oder Studio-Code-Server-App), dann im App-Store **⋮ → Nach Updates suchen** – die App erscheint
unter „Lokale Apps“.

## Aufbau

```
tarifvergleich/
  config.yaml        App-Manifest + Optionen
  Dockerfile
  app/main.py        stündlicher Rechenlauf, Web-Server (Ingress), Ausgabe an HA
  app/ha.py          WebSocket/REST-Client (Supervisor-Proxy)
  app/prices.py      Börsenpreise (Energy-Charts, Fallback aWATTar) + SQLite-Cache
  app/sim.py         Tarifformel, Simulation, Lastverschiebung
  app/static/        Oberfläche
```
