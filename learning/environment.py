import time

import gymnasium as gym
import numpy as np

from learning.core import (Frames, Gems, Metrics, Repetition, POINTS, SCROLLS,
                           SIZE, WAIT, static_mask)


class Controls:
    def __init__(self, paused=True):
        self.paused = paused
        self.stopped = False
        self.pause_seconds = 0.
        self.pause_start = time.monotonic() if paused else None
        self.generation = 0
        self.release = lambda: None
        self.held = set()
        self.deadline = None
        self.hotkeys_enabled = lambda: True
        self.stop_reason = None

    def pause(self):
        if not self.paused:
            self.paused = True
            self.generation += 1
            self.pause_start = time.monotonic()
            self.release()

    def start(self):
        from pynput import keyboard

        def press(key):
            char = (getattr(key, 'char', '') or '').lower()
            if not self.hotkeys_enabled():
                return
            if char in self.held:
                return
            self.held.add(char)
            if char == 'p':
                self.stopped = True
                self.stop_reason = 'P in game window'
                self.release()
            elif char == 'z':
                if self.paused:
                    self.pause_seconds += time.monotonic() - self.pause_start
                    self.paused, self.pause_start = False, None
                else:
                    self.pause()

        def release(key):
            self.held.discard((getattr(key, 'char', '') or '').lower())

        self.listener = keyboard.Listener(on_press=press, on_release=release)
        self.listener.start()

    def active_time(self):
        now = time.monotonic()
        return now - self.pause_seconds - (now-self.pause_start if self.paused else 0)

    def wait(self):
        while self.paused and not self.stopped:
            if self.deadline and time.monotonic() >= self.deadline:
                self.stopped = True
                self.stop_reason = 'time limit'
            time.sleep(.05)
        if self.deadline and time.monotonic() >= self.deadline:
            self.stopped = True
            self.stop_reason = 'time limit'
        if self.stopped:
            raise KeyboardInterrupt

    def close(self):
        self.release()
        if hasattr(self, 'listener'):
            self.listener.stop()


class LiveInterface:
    def __init__(self, capture, mouse, vision, controls):
        self.capture, self.mouse, self.vision, self.controls = capture, mouse, vision, controls
        controls.release = mouse.release
        controls.hotkeys_enabled = capture.is_window_active
        mouse.guard = self.ready
        mouse.path_guard = vision.path_supported

    def mask(self):
        points = [self.vision.path_supported(p, p) for p in POINTS]
        return np.array(points*2 + [self.vision.path_supported(a, b) for a,b in SCROLLS] + [True])

    def ready(self):
        if self.controls.deadline and time.monotonic() >= self.controls.deadline:
            self.controls.stopped = True
            self.controls.stop_reason = 'time limit'
        if self.controls.paused or self.controls.stopped:
            return False
        try:
            return self.capture.is_window_active() and self.vision.supported(self.capture.capture())
        except (RuntimeError, OSError):
            return False

    def snapshot(self):
        try:
            image = self.capture.capture()
            supported = self.vision.supported(image)
            value, confidence = self.vision.gems(image) if supported else (None, 0.)
            return image, value, confidence, supported and self.capture.is_window_active()
        except (RuntimeError, OSError):
            return np.zeros((SIZE[1], SIZE[0], 3), dtype=np.uint8), None, 0., False

    def execute(self, action):
        if action == WAIT:
            return True
        if not self.ready():
            return False
        if action < len(POINTS):
            return self.mouse.click(*POINTS[action])
        if action < len(POINTS)*2:
            return self.mouse.hold_at(*POINTS[action-len(POINTS)], duration=1.)
        a, b = SCROLLS[action-len(POINTS)*2]
        return self.mouse.drag(*a, *b)

    def sleep(self, seconds):
        deadline = time.monotonic() + max(0, seconds)
        while time.monotonic() < deadline:
            if self.controls.stopped:
                raise KeyboardInterrupt
            if self.controls.paused:
                break
            time.sleep(min(.05, max(0, deadline-time.monotonic())))


class GameEnv(gym.Env):
    metadata = {'render_modes': []}

    def __init__(self, interface, controls=None, writer=None):
        self.io = interface
        self.controls = controls or Controls(paused=False)
        self.writer = writer
        self.observation_space = gym.spaces.Box(0, 255, (4, 208, 96), np.uint8)
        self.action_space = gym.spaces.Discrete(WAIT+1)
        self.frames, self.gems = Frames(), Gems()
        self.metrics, self.repetition = Metrics(), Repetition()
        self.generation = self.controls.generation
        self.steps = 0
        self.blocked_count = self.suspected_count = 0
        self.last_clock = self.controls.active_time()

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        self.gems.reset()
        self.frames = Frames()
        image, _, _, self.supported = self.io.snapshot()
        self.image = image
        self.last_clock = self.controls.active_time()
        return self.frames.add(image), {}

    def action_masks(self):
        mask = static_mask()
        if hasattr(self.io, 'mask'):
            mask &= self.io.mask()
        if not self.supported or self.controls.paused or self.controls.stopped:
            mask[:-1] = False
        return mask

    def step(self, action):
        self.controls.wait()
        if self.controls.generation != self.generation:
            self.gems.reset()
            self.repetition = Repetition()
            self.frames = Frames()
            self.generation = self.controls.generation
        action = int(action)
        if not self.action_space.contains(action):
            raise ValueError('Invalid action')
        start = time.monotonic()
        blocked = not self.action_masks()[action] or not self.io.execute(action)
        suspect = self.repetition.penalty(action, self.image) if not blocked else 0.
        self.io.sleep(1.5 - (time.monotonic()-start))
        image, value, confidence, self.supported = self.io.snapshot()
        now = self.controls.active_time()
        if self.controls.generation != self.generation:
            self.gems.reset()
        delta, confirmed = self.gems.read(value, confidence, now)
        if self.gems.decreased:
            print('Confirmed Gems decrease: paused for review. Z establishes a new baseline.')
            self.controls.pause()
        seconds, self.last_clock = max(0., now-self.last_clock), now
        components = {'gems': float(delta), 'time': -.01*seconds,
                      'blocked': -.1*blocked, 'suspected_ineffective': suspect}
        info = components | self.metrics.add(seconds, delta, self.gems.last is not None)
        self.blocked_count += int(blocked)
        self.suspected_count += int(suspect < 0)
        info.update(ocr_available=float(confirmed), ocr_confidence=float(confidence),
                    supported=float(self.supported), blocked_actions=self.blocked_count,
                    suspected_ineffective_actions=self.suspected_count)
        observation = self.frames.add(image)
        self.image = image
        self.steps += 1
        if self.writer:
            for key, val in info.items():
                self.writer.add_scalar('session/'+key, val, self.steps)
        return observation, sum(components.values()), False, False, info

    def close(self):
        # Include the last optimizer interval even when no further rollout starts.
        now = self.controls.active_time()
        final_metrics = self.metrics.add(max(0., now-self.last_clock), 0,
                                         self.gems.last_time is not None and now-self.gems.last_time <= 60)
        self.last_clock = now
        self.controls.close()
        if self.writer:
            for key, value in final_metrics.items():
                self.writer.add_scalar('session/'+key, value, self.steps)
            self.writer.flush()


class SimulatedInterface:
    """Deterministic offline interface; never creates a window or sends input."""
    def __init__(self):
        self.step = 0

    def snapshot(self):
        image = np.zeros((SIZE[1], SIZE[0], 3), np.uint8)
        image[:, :, 0] = np.arange(SIZE[0], dtype=np.uint16) % 255
        image[300:400, 100:200, 1] = self.step % 255
        return image, 100 + self.step // 6, .99, True

    def execute(self, action):
        self.step += 1
        return bool(static_mask()[action])

    def sleep(self, seconds):
        pass
