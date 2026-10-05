# 🤖 Eatventure AI Agent - Projektplan und Architektur

## 🎯 Projektziel
Entwicklung eines Reinforcement Learning (RL) Agenten, der autonom das Android-Spiel "Eatventure" spielt, indem er Computer Vision zur Zustandsanalyse und Android Automation zur sicheren Interaktion nutzt.

## 🗺️ Phasen-Übersicht (Der Fahrplan)
Der Projektfortschritt folgte einer strengen, sequenziellen Methodik, die spezialisierte Agenten orchestrierte:

1. **Design (AI Architect):** Architekturentwurf, Definition von State Space ($S_t$) und Action Space ($A_t$).
2. **Game Knowledge (Game Analyzer):** Detaillierte Analyse der Spielstrategie, Belohnungsstruktur und kritischer Schwellenwerte.
3. **Perception (Computer Vision Engineer):** Implementierung des Feature Extractors zur Umwandlung von Bildern in den Zustandsvektor $S_t$.
4. **Action (Android Controller):** Implementierung der sicheren Schnittstelle, um abstrakte Aktionen in physische Eingaben zu übersetzen.
5. **Learning Core (RL Researcher):** Implementierung des PPO-Trainingsloops, der `EatventureEnv` und der finalen Belohnungsfunktion $\mathcal{R}_t$.
6. **Monitoring (Experiment Manager):** Aufbau von Tracking- und CI/CD-Prozessen zur Gewährleistung von Reproduzierbarkeit und Qualitätssicherung.

## 📐 Architektonisches Design (Die Pipeline)

Der Agent arbeitet in einem kontinuierlichen **Sense-Think-Act-Feedback-Zyklus**:

$$\text{Image} \xrightarrow{\text{CV}} \text{State Features } (S_t) \xrightarrow{\text{RL Agent}} \text{Action } (A_t) \xrightarrow{\text{Controller}} \text{Action Execution} \xrightarrow{\text{Game}} \text{Reward } (R_{t+1}), \text{ Image}_{t+1}$$

### 🔑 Kernmechanismen

*   **Zustandsraum ($S_t$):** Nicht das gesamte Bild, sondern ein Vektor von extrahierten Features (GPM, Multiplikator, Upgrade Status, Klick-Zielkoordinaten).
*   **Aktionsraum ($A_t$):** Diskret definierte, logische Befehle (z.B. `ACT_UPGRADE_STATION_X`), nicht nur Pixelkoordinaten.
*   **Belohnungsfunktion ($\mathcal{R}_t$):** Die Belohnung ist **proportional zum multiplizierten GPM** ($\mathcal{R} \propto \text{GPM} \times \text{Multiplikator}$). Dies erzwingt strategisches Verhalten, das Multiplikatoren zu maximieren.
*   **RL Algorithmus:** Proximal Policy Optimization (PPO) aufgrund seiner Stabilität in dynamischen, komplexen Umgebungen.

## 🛡️ Projektregeln & Protokolle
*   **Sicherheit:** Alle Eingaben müssen über den `MouseController` laufen. Fokusverlust muss sofortiges Stoppen und Freigeben aller Inputs erzwingen.
*   **Datenintegrität:** Positive Belohnungen nur bei **bestätigtem** GPM-Wert (3 aufeinanderfolgende Messungen $\ge 0.95$ Konfidenz).
*   **Prozess:** Strikte Trennung von Trainings- und Evaluationssitzungen; Checkpoint-Zustand (inkl. Optimizer State) muss persistiert werden.

## ➡️ Aktueller Status
**Status:** Der komplette Kern-Prototyp ist implementiert und getestet. Die systemische Integration der Komponenten ist abgeschlossen.

**Nächste Schritte (Operationalisierung):**
1. Datensammlung (Ground Truth Annotation) für das CV-Training.
2. Integration der realen CV- und ADB-Treiber in die `EatventureEnv`.
3. Skalierung und Kalibrierung des Systems in einer Live-Spielumgebung.