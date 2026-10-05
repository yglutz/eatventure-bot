import gymnasium as gym
from gymnasium import spaces
import numpy as np
import torch
import torch.nn as nn
from torch.distributions import Categorical
import random
import copy
import time

# --- 1. MOCK/INTERFACE COMPONENTS (External Dependencies) ---
# Diese Klassen simulieren die Integration mit CV und ADB Controller.

class MouseController:
    """
    Secure interface for all input actions, mandated by project rules.
    DO NOT bypass this class.
    """
    @staticmethod
    def click(x: int, y: int):
        print(f"[MC] Performing secure click at ({x}, {y})")
        # Hier erfolgt die tatsächliche ADB/Touch-Implementierung
        pass

    @staticmethod
    def press(x: int, y: int):
        print(f"[MC] Performing secure press down at ({x}, {y})")
        pass
    
    @staticmethod
    def release(x: int, y: int):
        print(f"[MC] Performing secure release at ({x}, {y})")
        pass

    @staticmethod
    def swipe(start_x: int, start_y: int, end_x: int, end_y: int, duration_ms: int):
        print(f"[MC] Performing secure swipe from ({start_x}, {start_y}) to ({end_x}, {end_y})")
        # Hier erfolgt die tatsächliche ADB/Touch-Implementierung
        pass
    
    @staticmethod
    def is_focused() -> bool:
        """Prüft, ob die Anwendung noch im Fokus ist."""
        return True

class ActionExecutor:
    """
    Übersetzt abstrakte RL-Aktionen und normalisierte Koordinaten in sichere,
    physische Eingaben mittels MouseController.
    """
    
    CALIBRATION_MAP: dict[str, tuple[int, int]] = {
        "MAIN_MENU_ICON": (100, 500),
        "UPGRADE_BUTTON_DEFAULT": (500, 800),
    }
    
    SCREEN_WIDTH: int = 1920
    SCREEN_HEIGHT: int = 1080
    
    def __init__(self):
        print("ActionExecutor initialisiert. Bereit für sichere Eingabemapping.")
        self.is_running = True

    def _transform_coords(self, normalized_x: float, normalized_y: float) -> tuple[int, int]:
        """Transformiert normalisierte Koordinaten (0.0 bis 1.0) in absolute Pixelkoordinaten."""
        absolute_x = int(normalized_x * self.SCREEN_WIDTH)
        absolute_y = int(normalized_y * self.SCREEN_HEIGHT)
        return absolute_x, absolute_y

    def _handle_safety_check(self, action_type: str) -> bool:
        """Führt das Sicherheitsprotokoll durch: Überprüfung des Fokus und Freigabe aller Inputs bei Fehler."""
        if not MouseController.is_focused():
            print(f"[SAFETY ALERT] Fokusverlust während {action_type}. Beendigung der Ausführung.")
            self.is_running = False
            try:
                MouseController.release(0, 0) 
            except Exception as e:
                print(f"[ERROR] Fehler beim Freigeben des Inputs: {e}")
            return False
        return True

    def execute_action(self, action_id: str, state_info: dict[str, any]):
        """Führt die abstrakte RL-Aktion aus."""
        if not self.is_running:
            return

        try:
            if not self._handle_safety_check(action_id):
                return

            print(f"\\n[ACTION] Verarbeitung der ID: {action_id}")

            # --- Aktionsmapping Logik ---
            if action_id.startswith("ACT_CLICK_"):
                target = state_info.get("click_targets")
                if not target or len(target) != 2:
                    raise ValueError("Klick-Aktion benötigt gültiges 'click_targets' [norm_x, norm_y].")
                
                norm_x, norm_y = target[0], target[1]
                abs_x, abs_y = self._transform_coords(norm_x, norm_y)
                
                MouseController.press(abs_x, abs_y)
                time.sleep(0.1) 
                MouseController.click(abs_x, abs_y)
                MouseController.release(abs_x, abs_y)
                
            elif action_id.startswith("ACT_SWIPE_"):
                swipe_target = state_info.get("swipe_targets")
                if not swipe_target or len(swipe_target) != 4:
                    raise ValueError("Swipe-Aktion benötigt gültiges 'swipe_targets' [start_x, start_y, end_x, end_y].")

                norm_sx, norm_sy, norm_ex, norm_ey = swipe_target
                abs_sx, abs_sy = self._transform_coords(norm_sx, norm_sy)
                abs_ex, abs_ey = self._transform_coords(norm_ex, norm_ey)

                MouseController.swipe(abs_sx, abs_sy, abs_ex, abs_ey, duration_ms=500)

            elif action_id in self.CALIBRATION_MAP:
                x, y = self.CALIBRATION_MAP[action_id]
                print(f"[INFO] Ausführung fester Aktion '{action_id}' an kalibrierten Koordinaten: ({x}, {y})")
                MouseController.click(x, y)
                
            else:
                print(f"[ERROR] Unbekannte Aktions-ID empfangen: {action_id}")

        except ValueError as e:
            print(f"[RUNTIME ERROR] Ungültiges Input-Format für {action_id}: {e}")
        except Exception as e:
            print(f"[CRITICAL FAILURE] Ein unerwarteter Fehler ist aufgetreten: {e}. Agent pausiert.")
            self.is_running = False
            MouseController.release(0, 0)

class GameAnalyzer:
    """
    Implementiert die Geschäftslogik und die Berechnung der Belohnung (R_t).
    """
    def __init__(self):
        self.gem_reading_history = []
        self.current_multiplier = 1.0

    def get_current_multiplier(self):
        return self.current_multiplier

    def calculate_reward(self, state, action_id, next_state, game_events):
        """Berechnet die Gesamtbelohnung R_t."""
        R_GPM = 0.0
        R_Strategic = 0.0
        R_Penalty = 0.0
        
        multiplier = self.get_current_multiplier()

        # --- R_GPM (Gems Per Minute) ---
        measured_gpm = game_events.get("measured_gpm", 0.0)
        confidence = game_events.get("confidence", 0.0)
        
        # Update History und Überprüfung der Bestätigungsregel
        self.gem_reading_history.append(confidence)
        if len(self.gem_reading_history) > 3:
            self.gem_reading_history.pop(0)

        if len(self.gem_reading_history) == 3 and all(c >= 0.95 for c in self.gem_reading_history):
            R_GPM = measured_gpm * multiplier
        
        # --- R_Strategic & R_Penalty ---
        if action_id == 1: # Beispiel: Upgrade-Aktion
            R_Strategic = 0.5 
        if game_events.get("negative_event", False):
            R_Penalty = 10.0 
        
        total_reward = R_GPM + R_Strategic - R_Penalty
        return total_reward

# --- 2. ENVIRONMENT IMPLEMENTATION (Gym Interface) ---

class EatventureEnv(gym.Env):
    """
    Die Gym/Gymnasium-Schnittstelle für den Eatventure Agenten.
    Kapselt die gesamte Spiel-RL-Pipeline.
    """
    metadata = {'render_modes': ['human'], 'render_fps': 30}
    
    def __init__(self, render_mode=None):
        super().__init__()
        
        self.analyzer = GameAnalyzer()
        self.executor = ActionExecutor()
        
        # Zustand (Feature Vector, 64 Dimensionen)
        self.observation_space = spaces.Box(low=0, high=1, shape=(64,), dtype=np.float32)
        
        # Aktionsraum: Abstrahierte Aktionen (z.B. 0: Produzieren, 1: Upgrade, ...)
        self.action_space = spaces.Discrete(5)
        
        self.current_state = None

    def _get_obs(self) -> np.ndarray:
        """Simuliert die Abfrage des aktuellen Zustands (CV-Pipeline)."""
        # HIER wird der Output des CV Engineer Moduls eingebunden
        return np.random.rand(64).astype(np.float32)

    def reset(self, seed=None, options=None):
        """Startet einen neuen Spielzustand."""
        super().reset(seed=seed)
        self.analyzer.gem_reading_history = []
        self.analyzer.current_multiplier = 1.0 
        self.current_state = self._get_obs()
        observation = self.current_state
        info = {}
        return observation, info

    def step(self, action_id: int):
        """Nimmt eine abstrakte Aktion entgegen und führt den Spielzyklus ab."""
        # 1. Kontroll-Phase
        self.executor.execute_action(str(action_id), {}) # state_info ist hier mock
        
        # 2. Spiel-Reaktions-Phase
        next_state = self._get_obs()
        
        # Mock: Game Events (Input für Reward Function)
        game_events = {
            "measured_gpm": random.uniform(10, 20),
            "confidence": random.uniform(0.8, 1.0),
            "negative_event": random.random() < 0.05, 
            "multiplier": self.analyzer.get_current_multiplier()
        }
        
        # 3. Belohnungs-Phase
        reward = self.analyzer.calculate_reward(self.current_state, action_id, next_state, game_events)
        
        # 4. Zustand-Update
        self.current_state = next_state
        done = False
        info = {"reward_breakdown": {}}

        return next_state, reward, done, False, info

if __name__ == '__main__':
    # --- Demo-Ausführung ---
    print("--- Starte Demo der Eatventure Umgebung ---")
    env = EatventureEnv()
    obs, info = env.reset()
    print(f"Reset State Shape: {obs.shape}")
    
    # Test Aktion 1
    next_obs, reward, done, truncated, info = env.step(1) 
    print(f"Reward nach Aktion 1: {reward:.2f}")
    
    # Test Aktion 2
    next_obs, reward, done, truncated, info = env.step(0)
    print(f"Reward nach Aktion 2: {reward:.2f}")