# Lernender Eatventure-Agent mit Bildnetz und Gems-Belohnungen

## Ziel und Architektur

Der Agent lernt direkt aus Screenshots, zunächst durch Nachahmen des vorhandenen Bots und anschließend durch eigenes Spielen mit Belohnungen und Strafen. Hauptkennzahl sind **verdiente Gems pro Minute**. Die bestehenden verbotenen Klickbereiche bleiben verbindlich.

- Windows, `uv` und Python 3.13 bleiben bestehen.
- PyTorch, Gymnasium und **MaskablePPO mit CNN** bilden die Lernarchitektur. MaskablePPO unterstützt Bildnetze und das Ausschließen unzulässiger Aktionen. [Dokumentation](https://sb3-contrib.readthedocs.io/en/master/modules/ppo_mask.html)
- Standardmäßig CPU-Training für den vorhandenen Rechner mit Radeon 780M und rund 32 GB RAM.
- Der bisherige Bot bleibt als Demonstrationsquelle und Vergleichsmodus verfügbar. Im Lernmodus entscheidet das Netz; bestehende Bildvorlagen dienen nur der Erfolgskontrolle und Erkennung unterstützter Spielsituationen.

## Bildschirm, Aktionen und unveränderliche Sperrbereiche

- Bildschirmaufnahme auf korrekte Client-Koordinaten prüfen; fehlgeschlagene Aufnahmen und Änderungen der kalibrierten Fenstergröße unterbrechen die Eingaben.
- Netz-Eingabe: vier aufeinanderfolgende Graustufenbilder mit jeweils 96 × 208 Pixeln. Gems werden separat aus dem ursprünglichen Bild gelesen.
- Diskreter Aktionsraum: Klick oder eine Sekunde Halten auf einem Raster aus 18 × 36 Positionen, Scrollen nach oben/unten und Warten. Aktionen verwenden ein einheitliches Zeitfenster von zunächst 1,5 Sekunden.
- Alle vorhandenen Sperrzonen aus `config.py` einschließlich der unteren Sperrfläche bleiben unverändert. Ungültige Rasterpunkte werden maskiert.
- Zusätzlich prüft eine zentrale Ausführungsschicht jede Eingabe unmittelbar vor ihrer Ausführung. Das gilt für neuronalen Agenten, Demonstrationsbot, absolute Koordinaten, Halteaktionen und vollständige Drag-Pfade.
- Direkte Win32-Eingaben außerhalb dieser Schicht werden entfernt. Bereits vorhandene Umg...