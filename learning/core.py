import hashlib
import json
import time
import uuid
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import config
from safety import allowed, path_allowed

ACTION_VERSION = 1
SIZE = (int(config.WINDOW_WIDTH), int(config.WINDOW_HEIGHT))
POINTS = [(round((x + .5) * SIZE[0] / 18), round((y + .5) * SIZE[1] / 36))
          for y in range(36) for x in range(18)]
SCROLLS = [(config.SCROLL_START_POS, config.SCROLL_END_POS),
           (config.SCROLL_END_POS, config.SCROLL_START_POS)]
WAIT = len(POINTS) * 2 + 2


def static_mask():
    points = [allowed(p, SIZE) for p in POINTS]
    return np.array(points * 2 + [path_allowed(a, b, SIZE) for a, b in SCROLLS] + [True])


def fingerprint():
    values = {k: v for k, v in vars(config).items() if k.startswith('FORBIDDEN_')}
    values.update(size=SIZE, actions=ACTION_VERSION, observation=[4, 208, 96], window=1.5,
                  scrolls=SCROLLS, rewards=[1., -.01, -.1, -.02], ocr=[3, .95, 60])
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def map_demo(kind, start, end, duration, valid_mask=None):
    valid_mask = static_mask() if valid_mask is None else (static_mask() & valid_mask)
    if kind == 'drag':
        for i, (a, b) in enumerate(SCROLLS):
            if tuple(start) == a and tuple(end) == b and valid_mask[len(POINTS)*2+i]:
                return len(POINTS) * 2 + i
        return None
    if kind not in ('click', 'hold'):
        return None
    # Long legacy holds do not represent the one-second learning action.
    if kind == 'hold' and abs(duration - 1.) > .25:
        return None
    offset = len(POINTS) if kind == 'hold' else 0
    candidates = [(np.linalg.norm(np.subtract(p, start)), i)
                  for i, p in enumerate(POINTS) if valid_mask[i+offset]]
    if not candidates:
        return None
    distance, index = min(candidates)
    return index + offset if distance <= 16 else None


class Frames:
    def __init__(self):
        self.frames = deque(maxlen=4)

    def add(self, image):
        gray = cv2.resize(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), (96, 208))
        self.frames.append(gray)
        while len(self.frames) < 4:
            self.frames.append(gray.copy())
        return np.stack(self.frames)


class Gems:
    """Only three consecutive high-confidence readings can change the baseline."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.last = self.last_time = self.candidate = None
        self.count = 0
        self.decreased = False

    def read(self, value, confidence, now):
        if self.last_time is not None and now - self.last_time > 60:
            self.reset()
        if value is None or confidence < .95 or value < 0:
            self.candidate, self.count = None, 0
            return 0, False
        self.count = self.count + 1 if value == self.candidate else 1
        self.candidate = value
        if self.count < 3:
            return 0, False
        if self.last is not None and value < self.last:
            self.decreased = True
            return 0, True
        delta = 0 if self.last is None else value - self.last
        self.last, self.last_time = value, now
        return delta, True


class Repetition:
    def __init__(self):
        self.key, self.count = None, 0

    def penalty(self, action, image):
        if action >= len(POINTS) * 2:
            self.key, self.count = None, 0
            return 0.
        x, y = POINTS[action % len(POINTS)]
        patch = image[max(0,y-12):y+13, max(0,x-12):x+13]
        key = (x, y, hashlib.sha256(patch.tobytes()).digest())
        self.count = self.count + 1 if key == self.key else 1
        self.key = key
        return -.02 if self.count >= 3 else 0.


class Metrics:
    def __init__(self):
        self.events = deque()
        self.elapsed = self.unknown = 0.

    def add(self, seconds, gems, reliable):
        self.elapsed += seconds
        self.unknown += 0 if reliable else seconds
        self.events.append((self.elapsed, gems))
        while self.events and self.events[0][0] < self.elapsed - 1800:
            self.events.popleft()
        return {f'gems_per_min_{minutes}m': sum(g for t, g in self.events
                if t > self.elapsed - minutes * 60) * 60 / max(.001, min(self.elapsed, minutes * 60))
                for minutes in (5, 30)} | {'unknown_seconds': self.unknown}


class Recording:
    def __init__(self, root, budget_gb=10):
        self.root = Path(root)
        self.session = self.root / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        self.session.mkdir(parents=True, exist_ok=False)
        self.budget = int(budget_gb * 1024**3)
        self.index = 0
        self.full = False

    def write(self, observation, action, metadata):
        import io
        buffer = io.BytesIO()
        np.savez_compressed(buffer, observation=observation, action=action,
                            metadata=json.dumps(metadata | {'version': ACTION_VERSION,
                                                          'config': fingerprint()}))
        data = buffer.getvalue()
        used = sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file())
        if used + len(data) > self.budget:
            self.full = True
            return False
        path = self.session / f'{self.index:08d}.npz'
        path.write_bytes(data)
        self.index += 1
        return True
