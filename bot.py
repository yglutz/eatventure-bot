import time
import logging
import math
from pathlib import Path

from window_capture import WindowCapture, ForbiddenAreaOverlay
from image_matcher import ImageMatcher
from mouse_controller import MouseController
from state_machine import StateMachine, State
from telegram_notifier import TelegramNotifier
from action_result import ActionResult
from runtime_metrics import metrics
import config

logger = logging.getLogger(__name__)


class EatventureBot:
    def __init__(self):
        logger.info("Initializing Eatventure Bot...")
        
        self.window_capture = WindowCapture(config.WINDOW_TITLE, config.WINDOW_WIDTH, config.WINDOW_HEIGHT)
        self.image_matcher = ImageMatcher(config.MATCH_THRESHOLD)
        self.running = False
        self.mouse_controller = MouseController(
            self.window_capture.hwnd,
            config.CLICK_DELAY,
            cancelled=lambda: not self.running,
        )
        self.state_machine = StateMachine(State.FIND_RED_ICONS)
        
        self.register_states()
        self.templates = self.load_templates()
        self.running = False
        self.red_icon_cycle_count = 0
        self.red_icons = []
        self.current_red_icon_index = 0
        self.red_observed_at = None
        self.attempted_red_positions = set()
        self.pending_transition = None
        self.red_targets_remaining = False
        self.wait_for_unlock_attempts = 0
        self.max_wait_for_unlock_attempts = 4
        
        self.scroll_direction = 'down'
        self.scroll_count = 0
        self.max_scroll_count = 5
        self.work_done = False
        self.cycle_counter = 0
        self.upgrade_station_counter = 0
        self.red_icon_processed_count = 0
        
        self.successful_red_icon_positions = []
        self.upgrade_found_in_cycle = False
        self.consecutive_failed_cycles = 0
        
        self.failed_click_tracker = {}
        self.last_clicked_pos = None
        
        self.total_levels_completed = 0
        self.current_level_start_time = None
        
        self.telegram = TelegramNotifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, config.TELEGRAM_ENABLED)
        
        self.overlay = None
        if config.ShowForbiddenArea:
            forbidden_zones = [
                (config.FORBIDDEN_ZONE_1_X_MIN, config.FORBIDDEN_ZONE_1_X_MAX, 
                 config.FORBIDDEN_ZONE_1_Y_MIN, config.FORBIDDEN_ZONE_1_Y_MAX),
                (config.FORBIDDEN_ZONE_2_X_MIN, config.FORBIDDEN_ZONE_2_X_MAX,
                 config.FORBIDDEN_ZONE_2_Y_MIN, config.FORBIDDEN_ZONE_2_Y_MAX),
                (config.FORBIDDEN_ZONE_3_X_MIN, config.FORBIDDEN_ZONE_3_X_MAX,
                 config.FORBIDDEN_ZONE_3_Y_MIN, config.FORBIDDEN_ZONE_3_Y_MAX),
                (config.FORBIDDEN_ZONE_4_X_MIN, config.FORBIDDEN_ZONE_4_X_MAX,
                 config.FORBIDDEN_ZONE_4_Y_MIN, config.FORBIDDEN_ZONE_4_Y_MAX),
                (config.FORBIDDEN_ZONE_5_X_MIN, config.FORBIDDEN_ZONE_5_X_MAX,
                 config.FORBIDDEN_ZONE_5_Y_MIN, config.FORBIDDEN_ZONE_5_Y_MAX),
            ]
            self.overlay = ForbiddenAreaOverlay(self.window_capture.hwnd, forbidden_zones)
            self.overlay.start()
            logger.info("Forbidden area overlay enabled and started")
        
        logger.info("Bot initialized successfully")
    
    def load_templates(self):
        templates = {}
        templates_path = Path(config.ASSETS_DIR)
        
        if not templates_path.exists():
            logger.error(f"Assets directory not found: {templates_path}")
            return templates
        
        for template_file in templates_path.glob("*.png"):
            try:
                template_name = template_file.stem
                template_img = self.image_matcher.load_template(template_file)
                templates[template_name] = template_img
                logger.info(f"Loaded template: {template_name}")
            except Exception as e:
                logger.error(f"Failed to load template {template_file}: {e}")
        
        return templates
    
    def register_states(self):
        self.state_machine.register_handler(State.FIND_RED_ICONS, self.handle_find_red_icons)
        self.state_machine.register_handler(State.CLICK_RED_ICON, self.handle_click_red_icon)
        self.state_machine.register_handler(State.CHECK_UNLOCK, self.handle_check_unlock)
        self.state_machine.register_handler(State.SEARCH_UPGRADE_STATION, self.handle_search_upgrade_station)
        self.state_machine.register_handler(State.HOLD_UPGRADE_STATION, self.handle_hold_upgrade_station)
        self.state_machine.register_handler(State.OPEN_BOXES, self.handle_open_boxes)
        self.state_machine.register_handler(State.UPGRADE_STATS, self.handle_upgrade_stats)
        self.state_machine.register_handler(State.SCROLL, self.handle_scroll)
        self.state_machine.register_handler(State.CHECK_NEW_LEVEL, self.handle_check_new_level)
        self.state_machine.register_handler(State.TRANSITION_LEVEL, self.handle_transition_level)
        self.state_machine.register_handler(State.WAIT_FOR_UNLOCK, self.handle_wait_for_unlock)
    
    def _is_in_any_forbidden_zone(self, x, y):
        return self.mouse_controller.is_in_forbidden_zone(x, y)
    
    def _invalidate_targets(self):
        self.red_icons = []
        self.red_observed_at = None

    def _click(self, x, y, relative=True):
        if not self.running:
            return ActionResult.CANCELLED
        try:
            result = self.mouse_controller.click(x, y, relative=relative)
        except Exception:
            self._invalidate_targets()
            raise
        if result is not ActionResult.DENIED:
            self._invalidate_targets()
        return result

    def _wait(self, seconds, held=False):
        deadline = time.monotonic() + seconds
        while self.running:
            if held and self.mouse_controller.check_held_input() is not ActionResult.SENT:
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            metrics.wait(min(0.05, remaining))
        return self.running

    def _detect(self, name, screenshot, threshold):
        if name not in self.templates:
            return False, 0, 0, 0
        template, mask = self.templates[name]
        options = {"check_color": config.UPGRADE_STATION_COLOR_CHECK} if name == "upgradeStation" else {}
        return self.image_matcher.find_template(
            screenshot, template, mask=mask, threshold=threshold, template_name=name, **options)

    def handle_find_red_icons(self, current_state):
        if not self.running:
            return current_state
        self._click(*config.IDLE_CLICK_POS)
        
        self.cycle_counter += 1
        logger.info(f"🔄 Cycle {self.cycle_counter}/2")
        
        if self.cycle_counter >= 2:
            logger.info("➡ 2 cycles done → Force scroll")
            self.cycle_counter = 0
            return State.SCROLL
        
        self.work_done = False
        
        screenshot = self.window_capture.capture()
        observed_at = time.monotonic()
        limited_screenshot = screenshot[:config.MAX_SEARCH_Y, :]
        
        if "newLevel" in self.templates:
            template, mask = self.templates["newLevel"]
            found, confidence, x, y = self.image_matcher.find_template(
                limited_screenshot, template, mask=mask,
                threshold=config.NEW_LEVEL_THRESHOLD
            )
            
            if found:
                logger.info(f"newLevel.png found at ({x}, {y}), transitioning to new level")
                return State.TRANSITION_LEVEL
        
        red_icon_templates = ["RedIcon", "RedIcon2","RedIcon3","RedIcon4","RedIcon5","RedIcon6","RedIcon7","RedIcon8","RedIcon9","RedIcon10","RedIcon11", "RedIconNoBG"]
        
        all_detections = {}
        
        for template_name in red_icon_templates:
            if template_name not in self.templates:
                continue
            
            template, mask = self.templates[template_name]
            
            icons = self.image_matcher.find_all_templates(
                limited_screenshot, template, mask=mask,
                threshold=config.RED_ICON_THRESHOLD,
                min_distance=80, template_name=template_name
            )
            
            for conf, x, y in icons:
                found_nearby = False
                for (px, py) in list(all_detections.keys()):
                    if abs(x - px) < 10 and abs(y - py) < 10:
                        all_detections[(px, py)].append((template_name, conf))
                        found_nearby = True
                        break
                
                if not found_nearby:
                    all_detections[(x, y)] = [(template_name, conf)]
        
        min_matches = config.RED_ICON_MIN_MATCHES
        total_detections = len(all_detections)
        rejected_count = 0
        
        self.red_icons = []
        for (x, y), matches in all_detections.items():
            if len(matches) >= min_matches:
                max_conf = max(conf for _, conf in matches)
                self.red_icons.append((max_conf, x, y))
            else:
                rejected_count += 1
        
        logger.info(f"Red Icon Detection: {total_detections} total, {len(self.red_icons)} valid, {rejected_count} rejected")
        
        if screenshot.shape[0] > 640:
            new_level_roi = screenshot[640:min(710, screenshot.shape[0]), :]
            for tname in red_icon_templates[:4]:
                if tname not in self.templates:
                    continue
                t, m = self.templates[tname]
                if t.shape[0] > new_level_roi.shape[0] or t.shape[1] > new_level_roi.shape[1]:
                    continue
                nf, nc, nx, ny = self.image_matcher.find_template(
                    new_level_roi, t, mask=m, threshold=config.RED_ICON_THRESHOLD
                )
                if nf:
                    abs_x, abs_y = nx, 640 + ny
                    if (config.NEW_LEVEL_RED_ICON_X_MIN <= abs_x <= config.NEW_LEVEL_RED_ICON_X_MAX and
                        config.NEW_LEVEL_RED_ICON_Y_MIN <= abs_y <= config.NEW_LEVEL_RED_ICON_Y_MAX):
                        logger.info(f"New level detected! Red icon at ({abs_x}, {abs_y})")
                        return State.CHECK_NEW_LEVEL
        
        if self.red_icons:
            filtered_icons = []
            forbidden_zone_count = 0
            
            for conf, x, y in self.red_icons:
                if self._is_in_any_forbidden_zone(x, y):
                    forbidden_zone_count += 1
                else:
                    filtered_icons.append((conf, x, y))
            
            if forbidden_zone_count > 0:
                logger.info(f"Forbidden Zone Filter: {forbidden_zone_count} icons removed")
            
            if not filtered_icons:
                logger.info("No valid red icons after filtering")
                return State.OPEN_BOXES
            
            stable_icons = []
            for conf, x, y in filtered_icons:
                pos_key = (x // 30, y // 30)
                if self.failed_click_tracker.get(pos_key, 0) < 3:
                    stable_icons.append((conf, x, y))
            
            if not stable_icons and filtered_icons:
                logger.info("All red icons are stuck positions, forcing scroll")
                self.failed_click_tracker.clear()
                return State.SCROLL
            
            if not stable_icons:
                return State.OPEN_BOXES
            
            def get_priority(icon):
                conf, x, y = icon
                for success_y in self.successful_red_icon_positions:
                    if abs(y - success_y) < 50:
                        return (0, y)
                return (1, y)
            
            stable_icons.sort(key=get_priority)
            # History is only a fairness hint, never authority to reuse coordinates.
            unattempted = [icon for icon in stable_icons
                           if (icon[1] // 30, icon[2] // 30) not in self.attempted_red_positions]
            if not unattempted:
                self.attempted_red_positions.clear()
            else:
                stable_icons = unattempted
            self.red_icons = stable_icons
            self.red_observed_at = observed_at
            
            logger.info(f"{len(self.red_icons)} red icons ready to process")
            self.current_red_icon_index = 0
            self.red_icon_cycle_count = 0
            self.work_done = True
            return State.CLICK_RED_ICON
        
        return State.OPEN_BOXES
    
    def handle_click_red_icon(self, current_state):
        if not self.running:
            return current_state
        if self.red_observed_at is None or time.monotonic() - self.red_observed_at > 1.0:
            self._invalidate_targets()
            return State.FIND_RED_ICONS
        if self.current_red_icon_index >= len(self.red_icons):
            logger.info("All red icons processed, continuing cycle")
            return State.OPEN_BOXES
        
        confidence, x, y = self.red_icons[self.current_red_icon_index]
        self.current_red_icon_index += 1
        self.red_targets_remaining = self.current_red_icon_index < len(self.red_icons)
        self.attempted_red_positions.add((x // 30, y // 30))
        self.last_clicked_pos = (x, y)
        click_x = x + config.RED_ICON_OFFSET_X
        click_y = y + config.RED_ICON_OFFSET_Y
        
        if self.mouse_controller.is_in_forbidden_zone(click_x, click_y):
            logger.warning(f"Red icon click blocked - position with offset ({click_x}, {click_y}) is in forbidden zone")
            return State.CLICK_RED_ICON if self.current_red_icon_index < len(self.red_icons) else State.OPEN_BOXES
        
        logger.info(f"Attempting red icon {self.current_red_icon_index}/{len(self.red_icons)} at ({click_x}, {click_y})")
        result = self._click(click_x, click_y)
        if result is not ActionResult.SENT:
            return State.CLICK_RED_ICON if self.current_red_icon_index < len(self.red_icons) else State.OPEN_BOXES
        
        self.red_icon_cycle_count = 0
        return State.CHECK_UNLOCK
    
    def handle_check_unlock(self, current_state):
        if not self.running:
            return current_state
        screenshot = self.window_capture.capture()
        limited_screenshot = screenshot[:config.MAX_SEARCH_Y, :]
        
        if "unlock" in self.templates:
            template, mask = self.templates["unlock"]
            found, confidence, x, y = self.image_matcher.find_template(
                limited_screenshot, template, mask=mask,
                threshold=config.UNLOCK_THRESHOLD, template_name="unlock"
            )
            
            if found:
                if self.mouse_controller.is_in_forbidden_zone(x, y):
                    logger.warning(f"Unlock button in forbidden zone, skipping")
                else:
                    logger.info(f"Unlock found, clicking")
                    self._click(x, y)
        
        return State.SEARCH_UPGRADE_STATION
    
    def handle_search_upgrade_station(self, current_state):
        max_attempts = 5
        relaxed_threshold = config.UPGRADE_STATION_THRESHOLD - 0.05
        
        for attempt in range(max_attempts):
            if not self.running:
                return current_state
            screenshot = self.window_capture.capture()
            limited_screenshot = screenshot[:config.MAX_SEARCH_Y, :]
            
            if "upgradeStation" in self.templates:
                template, mask = self.templates["upgradeStation"]
                
                current_threshold = config.UPGRADE_STATION_THRESHOLD if attempt < 2 else relaxed_threshold
                
                found, confidence, x, y = self.image_matcher.find_template(
                    limited_screenshot, template, mask=mask,
                    threshold=current_threshold, template_name="upgradeStation"
                )
                
                if found:
                    if self.mouse_controller.is_in_forbidden_zone(x, y):
                        logger.info(f"Upgrade station in forbidden zone ({x}, {y}), skipping")
                        if self.last_clicked_pos:
                            pk = (self.last_clicked_pos[0] // 30, self.last_clicked_pos[1] // 30)
                            self.failed_click_tracker[pk] = self.failed_click_tracker.get(pk, 0) + 1
                        self.red_icon_processed_count += 1
                        self.consecutive_failed_cycles += 1
                        return State.OPEN_BOXES
                    
                    logger.info(f"Upgrade station found (attempt {attempt + 1})")
                    self.upgrade_station_pos = (x, y)
                    
                    if self.last_clicked_pos:
                        _, red_y = self.last_clicked_pos
                        if red_y not in self.successful_red_icon_positions:
                            self.successful_red_icon_positions.append(red_y)
                    
                    self.upgrade_found_in_cycle = True
                    self.consecutive_failed_cycles = 0
                    return State.HOLD_UPGRADE_STATION
            
            if attempt < max_attempts - 1:
                self._wait(0.15)
        
        logger.info(f"Upgrade station not found (failed cycles: {self.consecutive_failed_cycles + 1})")
        if self.last_clicked_pos:
            pk = (self.last_clicked_pos[0] // 30, self.last_clicked_pos[1] // 30)
            self.failed_click_tracker[pk] = self.failed_click_tracker.get(pk, 0) + 1
        self.red_icon_processed_count += 1
        self.consecutive_failed_cycles += 1
        return State.OPEN_BOXES
    
    def handle_hold_upgrade_station(self, current_state):
        if not self.running:
            return current_state
        # Reobserve across the state boundary rather than hold a cached coordinate.
        screenshot = self.window_capture.capture()[:config.MAX_SEARCH_Y, :]
        found, _, x, y = self._detect("upgradeStation", screenshot, config.UPGRADE_STATION_THRESHOLD)
        if not found or not self.running:
            return State.OPEN_BOXES
        if not math.isfinite(config.UPGRADE_HOLD_DURATION) or config.UPGRADE_HOLD_DURATION <= 0:
            logger.warning("Unsafe upgrade hold duration; skipping")
            return State.OPEN_BOXES
        deadline = time.monotonic() + config.UPGRADE_HOLD_DURATION
        missing = 0
        interrupted = False
        try:
            with self.mouse_controller.pressed_at(x, y, relative=True) as result:
                if result is not ActionResult.SENT:
                    return State.OPEN_BOXES
                self._invalidate_targets()
                while self.running and time.monotonic() < deadline:
                    if not self._wait(min(0.2, max(0, deadline - time.monotonic())), held=True):
                        interrupted = True
                        break
                    screenshot = self.window_capture.capture()[:config.MAX_SEARCH_Y, :]
                    found, _, current_x, current_y = self._detect("upgradeStation", screenshot, config.UPGRADE_STATION_THRESHOLD)
                    if self.mouse_controller.check_held_input() is not ActionResult.SENT:
                        interrupted = True
                        break
                    # A similarly shaped button elsewhere is not the held target.
                    # Permit only small detector jitter, not movement to another control.
                    if found and (abs(current_x - x) > 3 or abs(current_y - y) > 3):
                        interrupted = True
                        break
                    missing = 0 if found else missing + 1
                    if missing >= 2:
                        break
        finally:
            self._invalidate_targets()
        # Disappearance is a stopping condition, not proof of a purchase. Existing
        # assets cannot verify a changed station level; do not inflate counters.
        if interrupted:
            return State.OPEN_BOXES
        self._click(*config.IDLE_CLICK_POS)
        self.red_icon_processed_count += 1
        return State.OPEN_BOXES
    
    def handle_upgrade_stats(self, current_state):
        if not self.running:
            return current_state
        if self.mouse_controller.is_in_forbidden_zone(*config.STATS_UPGRADE_POS):
            logger.warning("Stats target blocked; panel will not be opened")
            return State.OPEN_BOXES
        # No asset identifies a safe stats purchase or its postcondition. Even a
        # permissive coordinate configuration is not verified purchase authority.
        logger.warning("Stats purchase skipped: safe control identity cannot be verified")
        return State.OPEN_BOXES

    def handle_open_boxes(self, current_state):
        if not self.running:
            return current_state
        self._click(*config.IDLE_CLICK_POS)
        
        screenshot = self.window_capture.capture()
        limited_screenshot = screenshot[:config.MAX_SEARCH_Y, :]
        
        if "newLevel" in self.templates:
            template, mask = self.templates["newLevel"]
            found, confidence, x, y = self.image_matcher.find_template(
                limited_screenshot, template, mask=mask,
                threshold=config.NEW_LEVEL_THRESHOLD
            )
            
            if found:
                logger.info(f"New level found, transitioning")
                return State.TRANSITION_LEVEL
        
        box_names = ["box1", "box2", "box3", "box4", "box5"]
        boxes_found = 0
        
        for box_name in box_names:
            if box_name in self.templates:
                template, mask = self.templates[box_name]
                found, confidence, x, y = self.image_matcher.find_template(
                    limited_screenshot, template, mask=mask,
                    threshold=config.BOX_THRESHOLD, template_name=box_name
                )
                
                if found:
                    if self.mouse_controller.is_in_forbidden_zone(x, y):
                        logger.debug(f"{box_name} in forbidden zone, skipping")
                    else:
                        if self._click(x, y) is ActionResult.SENT:
                            boxes_found += 1
                            # Opening a box changes the scene; never consume other
                            # detections from this frame after that mutation.
                            break
        
        if boxes_found > 0:
            logger.info("Box input sent; opening is unverified")
            self.work_done = True

        if self.red_targets_remaining:
            self.red_targets_remaining = False
            self.cycle_counter = 0
            return State.FIND_RED_ICONS
        
        if self.upgrade_found_in_cycle:
            logger.info("✓ Upgrade found → Staying in area")
            self.upgrade_found_in_cycle = False
            self.cycle_counter = 0
            return State.FIND_RED_ICONS
        
        self.cycle_counter += 1
        
        if self.consecutive_failed_cycles >= 3:
            logger.info(f"⚠ {self.consecutive_failed_cycles} failed → Force scroll")
            self.consecutive_failed_cycles = 0
            self.cycle_counter = 0
            return State.SCROLL
        
        if self.cycle_counter >= 2:
            logger.info(f"➡ Cycle {self.cycle_counter}/2 done → Scrolling")
            self.cycle_counter = 0
            return State.SCROLL
        else:
            return State.FIND_RED_ICONS
    
    def handle_scroll(self, current_state):
        if not self.running:
            return current_state
        self.failed_click_tracker.clear()
        self._click(*config.IDLE_CLICK_POS)
        
        screenshot = self.window_capture.capture()
        limited_screenshot = screenshot[:config.MAX_SEARCH_Y, :]
        
        if "newLevel" in self.templates:
            template, mask = self.templates["newLevel"]
            found, confidence, x, y = self.image_matcher.find_template(
                limited_screenshot, template, mask=mask,
                threshold=config.NEW_LEVEL_THRESHOLD
            )
            
            if found:
                logger.info(f"New level detected before scroll")
                return State.TRANSITION_LEVEL
        
        if not self.running:
            return current_state
        self._invalidate_targets()
        self.attempted_red_positions.clear()
        self.red_targets_remaining = False
        if self.scroll_direction == 'up':
            logger.info(f"⬆ Scroll UP ({self.scroll_count + 1}/{self.max_scroll_count})")
            result = self.mouse_controller.drag(
                config.SCROLL_END_POS[0], config.SCROLL_END_POS[1],
                config.SCROLL_START_POS[0], config.SCROLL_START_POS[1],
                duration=0.3, relative=True
            )
        else:  # down
            logger.info(f"⬇ Scroll DOWN ({self.scroll_count + 1}/{self.max_scroll_count})")
            result = self.mouse_controller.drag(
                config.SCROLL_START_POS[0], config.SCROLL_START_POS[1],
                config.SCROLL_END_POS[0], config.SCROLL_END_POS[1],
                duration=0.3, relative=True
            )
        
        if result is not ActionResult.SENT:
            return State.FIND_RED_ICONS
        
        self.scroll_count += 1
        
        if self.scroll_count >= self.max_scroll_count:
            if self.scroll_direction == 'up':
                self.scroll_direction = 'down'
            else:
                self.scroll_direction = 'up'
            self.scroll_count = 0
        
        return State.FIND_RED_ICONS
    
    def handle_check_new_level(self, current_state):
        if not self.running:
            return current_state
        self._click(*config.IDLE_CLICK_POS)
        if self._click(*config.NEW_LEVEL_BUTTON_POS) is not ActionResult.SENT:
            return State.FIND_RED_ICONS
        deadline = time.monotonic() + 0.9
        for attempt in range(5):
            if not self.running or time.monotonic() >= deadline:
                break
            screenshot = self.window_capture.capture()[:config.MAX_SEARCH_Y, :]
            found, _, x, y = self._detect("upgradeStation", screenshot, config.UPGRADE_STATION_THRESHOLD)
            if found:
                if self._click(x, y) is ActionResult.SENT:
                    return State.TRANSITION_LEVEL
                break
            self._wait(0.15)
        return State.FIND_RED_ICONS
    
    def handle_transition_level(self, current_state):
        if not self.running:
            return current_state
        if self.pending_transition is not None:
            return State.WAIT_FOR_UNLOCK
        # Poll readiness instead of stacking a fixed 2.5s and 1s delay.
        for attempt in range(5):
            if not self.running:
                return current_state
            screenshot = self.window_capture.capture()[:config.MAX_SEARCH_Y, :]
            found, _, x, y = self._detect("newLevel", screenshot, config.NEW_LEVEL_THRESHOLD)
            if found:
                if self._click(x, y) is ActionResult.SENT:
                    self.pending_transition = time.monotonic() + 5.0
                    self.wait_for_unlock_attempts = 0
                    return State.WAIT_FOR_UNLOCK
                return State.FIND_RED_ICONS
            if attempt < 4:
                self._wait(0.6)
        return State.FIND_RED_ICONS
    
    def handle_wait_for_unlock(self, current_state):
        if not self.running:
            self.pending_transition = None
            return State.FIND_RED_ICONS
        if self.pending_transition is None:
            return State.FIND_RED_ICONS
        self.wait_for_unlock_attempts += 1
        if (self.wait_for_unlock_attempts > self.max_wait_for_unlock_attempts
                or time.monotonic() >= self.pending_transition):
            self.pending_transition = None
            self.wait_for_unlock_attempts = 0
            self.scroll_direction = 'down'
            self.scroll_count = 0
            return State.FIND_RED_ICONS
        screenshot = self.window_capture.capture()
        new_level, _, _, _ = self._detect("newLevel", screenshot, config.NEW_LEVEL_THRESHOLD)
        unlocked, _, x, y = self._detect("unlock", screenshot, config.UNLOCK_THRESHOLD)
        if not new_level and unlocked and self.running:
            # These assets are generic Open/Unlock controls, also seen at ordinary
            # stations. This is readiness evidence only, NOT level confirmation.
            # A trustworthy level identity/transition fixture is still required.
            self.pending_transition = None
            logger.info("Transition readiness observed; level completion remains unverified")
            self.wait_for_unlock_attempts = 0
            self.scroll_direction = 'down'
            self.scroll_count = 0
            self.attempted_red_positions.clear()
            self._invalidate_targets()
            self._click(x, y)
            return State.FIND_RED_ICONS
        self._wait(0.3)
        return State.WAIT_FOR_UNLOCK
    
    def run(self):
        self.running = True
        logger.info("Bot started - Press Ctrl+C to stop")
        
        try:
            while self.running:
                if not self.window_capture.is_window_active():
                    logger.error(f"Window '{config.WINDOW_TITLE}' is no longer active!")
                    break
                
                self.state_machine.update()
                
        except KeyboardInterrupt:
            logger.info("Bot stopped by user (Ctrl+C)")
        except Exception as e:
            logger.error(f"Bot error: {e}", exc_info=True)
        finally:
            self.stop()
    
    def stop(self):
        self.running = False
        self.pending_transition = None
        self._invalidate_targets()
        if self.overlay:
            self.overlay.stop()
        logger.info("Bot stopped")
