import time
import logging
import sys
from pathlib import Path
from datetime import datetime

from window_capture import WindowCapture, ForbiddenAreaOverlay
from image_matcher import ImageMatcher
from mouse_controller import MouseController
from state_machine import StateMachine, State
from telegram_notifier import TelegramNotifier
import config

logger = logging.getLogger(__name__)


class EatventureBot:
    def __init__(self):
        logger.info("Initializing Eatventure Bot...")
        self.running = False
        self.window_capture = None
        self.mouse_controller = None
        self.overlay = None
        try:
            self._initialize()
        except BaseException:
            # main cannot clean up an instance whose constructor never returned.
            # Preserve the initialization error even if releasing resources fails.
            try:
                self.stop()
            except BaseException:
                logger.exception("Cleanup after bot initialization failure failed")
            raise

    def _initialize(self):
        self.window_capture = WindowCapture(config.WINDOW_TITLE, config.WINDOW_WIDTH, config.WINDOW_HEIGHT)
        self.image_matcher = ImageMatcher(config.MATCH_THRESHOLD)
        self.mouse_controller = MouseController(
            self.window_capture.hwnd,
            config.CLICK_DELAY,
            backend=self.window_capture.backend,
            cancel_check=lambda: not self.running,
        )
        self.state_machine = StateMachine(State.FIND_RED_ICONS)
        
        self.register_states()
        self.templates = self.load_templates()
        self.running = False
        self.red_icon_cycle_count = 0
        self.red_icons = []
        self.current_red_icon_index = 0
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
        if config.ShowForbiddenArea and sys.platform != "win32":
            logger.info("Forbidden-area overlay unsupported on Linux; static input zones remain enforced")
        if config.ShowForbiddenArea and sys.platform == "win32":
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

    def _capture(self):
        if not self.running:
            raise RuntimeError("Bot paused")
        frame = self.window_capture.capture()
        if frame is None or getattr(frame, "size", 0) == 0:
            raise RuntimeError("Capture failed")
        return frame

    def _input(self, operation, *args, **kwargs):
        if not self.running:
            raise RuntimeError("Bot paused")
        # The first idle click also needs verified capture geometry.
        if self.window_capture.backend.last_capture_rect is None:
            self._capture()
        result = operation(*args, **kwargs)
        if not result:
            raise RuntimeError(f"Input failed: {getattr(result, 'reason', 'unknown')}")
        return result

    def _click(self, *args, **kwargs):
        return self._input(self.mouse_controller.click, *args, **kwargs)

    def _drag(self, *args, **kwargs):
        return self._input(self.mouse_controller.drag, *args, **kwargs)

    def pause(self):
        # The in-flight pressed_at context releases on its next validation.
        # Do not send release concurrently from the keyboard listener thread.
        self.running = False
    
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
        zones = [
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
        for x_min, x_max, y_min, y_max in zones:
            if x_min <= x <= x_max and y_min <= y <= y_max:
                return True
        return False
    
    def handle_find_red_icons(self, current_state):
        self._click(config.IDLE_CLICK_POS[0], config.IDLE_CLICK_POS[1], relative=True)
        
        self.cycle_counter += 1
        logger.info(f"🔄 Cycle {self.cycle_counter}/2")
        
        if self.cycle_counter >= 2:
            logger.info("➡ 2 cycles done → Force scroll")
            self.cycle_counter = 0
            return State.SCROLL
        
        self.work_done = False
        
        screenshot = self._capture()
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
            self.red_icons = stable_icons
            
            logger.info(f"{len(self.red_icons)} red icons ready to process")
            self.current_red_icon_index = 0
            self.red_icon_cycle_count = 0
            self.work_done = True
            return State.CLICK_RED_ICON
        
        return State.OPEN_BOXES
    
    def handle_click_red_icon(self, current_state):
        if self.current_red_icon_index >= len(self.red_icons):
            logger.info("All red icons processed, continuing cycle")
            return State.OPEN_BOXES
        
        confidence, x, y = self.red_icons[self.current_red_icon_index]
        self.last_clicked_pos = (x, y)
        click_x = x + config.RED_ICON_OFFSET_X
        click_y = y + config.RED_ICON_OFFSET_Y
        
        if self.mouse_controller.is_in_forbidden_zone(click_x, click_y):
            logger.warning(f"Red icon click blocked - position with offset ({click_x}, {click_y}) is in forbidden zone")
            self.current_red_icon_index += 1
            return State.CLICK_RED_ICON if self.current_red_icon_index < len(self.red_icons) else State.OPEN_BOXES
        
        logger.info(f"Clicking red icon {self.current_red_icon_index + 1}/{len(self.red_icons)} at ({click_x}, {click_y})")
        self._click(click_x, click_y, relative=True)
        
        self.red_icon_cycle_count = 0
        return State.CHECK_UNLOCK
    
    def handle_check_unlock(self, current_state):
        screenshot = self._capture()
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
                    self._click(x, y, relative=True)
        
        return State.SEARCH_UPGRADE_STATION
    
    def handle_search_upgrade_station(self, current_state):
        max_attempts = 5
        relaxed_threshold = config.UPGRADE_STATION_THRESHOLD - 0.05
        
        for attempt in range(max_attempts):
            screenshot = self._capture()
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
                    
                    if self.current_red_icon_index < len(self.red_icons):
                        _, _, red_y = self.red_icons[self.current_red_icon_index]
                        if red_y not in self.successful_red_icon_positions:
                            self.successful_red_icon_positions.append(red_y)
                    
                    self.upgrade_found_in_cycle = True
                    self.consecutive_failed_cycles = 0
                    return State.HOLD_UPGRADE_STATION
            
            if attempt < max_attempts - 1:
                time.sleep(0.15)
        
        logger.info(f"Upgrade station not found (failed cycles: {self.consecutive_failed_cycles + 1})")
        if self.last_clicked_pos:
            pk = (self.last_clicked_pos[0] // 30, self.last_clicked_pos[1] // 30)
            self.failed_click_tracker[pk] = self.failed_click_tracker.get(pk, 0) + 1
        self.red_icon_processed_count += 1
        self.consecutive_failed_cycles += 1
        return State.OPEN_BOXES
    
    def handle_hold_upgrade_station(self, current_state):
        x, y = self.upgrade_station_pos
        logger.info(f"Holding upgrade station...")
        
        max_hold_time = 10.0
        check_interval = 0.2
        started = time.monotonic()
        deadline = started + max_hold_time
        next_check = started + check_interval
        consecutive_not_found = 0
        required_not_found = 2
        
        with self.mouse_controller.pressed_at(x, y, relative=True):
            while time.monotonic() < deadline:
                self.mouse_controller.validate()
                now = time.monotonic()
                if now < next_check:
                    time.sleep(min(0.05, next_check - now, deadline - now))
                    continue
                screenshot = self._capture()
                self.mouse_controller.validate()
                limited_screenshot = screenshot[:config.MAX_SEARCH_Y, :]
                if "upgradeStation" in self.templates:
                    template, mask = self.templates["upgradeStation"]
                    found, confidence, found_x, found_y = self.image_matcher.find_template(
                        limited_screenshot, template, mask=mask,
                        threshold=config.UPGRADE_STATION_THRESHOLD, template_name="upgradeStation",
                        check_color=config.UPGRADE_STATION_COLOR_CHECK
                    )
                    if not found:
                        consecutive_not_found += 1
                        if consecutive_not_found >= required_not_found:
                            logger.info("Hold released: upgradeStation disappeared")
                            break
                    else:
                        consecutive_not_found = 0
                next_check = time.monotonic() + check_interval
        
        self._click(config.IDLE_CLICK_POS[0], config.IDLE_CLICK_POS[1], relative=True)
        time.sleep(0.05)
        
        if self.last_clicked_pos:
            pk = (self.last_clicked_pos[0] // 30, self.last_clicked_pos[1] // 30)
            self.failed_click_tracker.pop(pk, None)
        
        self.red_icon_processed_count += 1
        
        self.upgrade_station_counter += 1
        logger.info(f"Upgrades: {self.upgrade_station_counter}/2")
        
        if self.upgrade_station_counter >= 2:
            logger.info("✓ 2 upgrades done → Stats upgrade")
            self.upgrade_station_counter = 0
            return State.UPGRADE_STATS
        
        return State.OPEN_BOXES
    
    def handle_upgrade_stats(self, current_state):
        logger.info("⬆ Stats upgrade starting")
        self._click(config.IDLE_CLICK_POS[0], config.IDLE_CLICK_POS[1], relative=True)
        
        screenshot = self._capture()
        limited_screenshot = screenshot[:config.MAX_SEARCH_Y, :]
        
        if "newLevel" in self.templates:
            template, mask = self.templates["newLevel"]
            found, confidence, x, y = self.image_matcher.find_template(
                limited_screenshot, template, mask=mask,
                threshold=config.NEW_LEVEL_THRESHOLD
            )
            
            if found:
                logger.info(f"New level detected during stats upgrade")
                return State.TRANSITION_LEVEL
        
        has_upgrade_icon = False
        red_icon_templates = ["RedIcon", "RedIcon2", "RedIcon3", "RedIcon4", "RedIcon5",
                              "RedIcon6", "RedIcon7", "RedIcon8", "RedIcon9", "RedIcon10",
                              "RedIcon11", "RedIconNoBG"]
        
        roi_y_start = max(0, config.UPGRADE_RED_ICON_Y_MIN - 30)
        roi_y_end = min(screenshot.shape[0], config.UPGRADE_RED_ICON_Y_MAX + 30)
        roi_x_start = max(0, config.UPGRADE_RED_ICON_X_MIN - 30)
        roi_x_end = min(screenshot.shape[1], config.UPGRADE_RED_ICON_X_MAX + 30)
        upgrade_roi = screenshot[roi_y_start:roi_y_end, roi_x_start:roi_x_end]
        
        for template_name in red_icon_templates:
            if template_name not in self.templates:
                continue
            template, mask = self.templates[template_name]
            if template.shape[0] > upgrade_roi.shape[0] or template.shape[1] > upgrade_roi.shape[1]:
                continue
            found, conf, rx, ry = self.image_matcher.find_template(
                upgrade_roi, template, mask=mask,
                threshold=config.STATS_RED_ICON_THRESHOLD
            )
            if found:
                abs_x = roi_x_start + rx
                abs_y = roi_y_start + ry
                if (config.UPGRADE_RED_ICON_X_MIN <= abs_x <= config.UPGRADE_RED_ICON_X_MAX and
                    config.UPGRADE_RED_ICON_Y_MIN <= abs_y <= config.UPGRADE_RED_ICON_Y_MAX):
                    has_upgrade_icon = True
                    break
        
        if not has_upgrade_icon:
            logger.info("✗ No stats icon, skipping")
            return State.SCROLL
        
        logger.info("✓ Stats icon found, upgrading")
        self._click(config.STATS_UPGRADE_BUTTON_POS[0], config.STATS_UPGRADE_BUTTON_POS[1], relative=True)
        time.sleep(config.STATE_DELAY)
        
        for i in range(config.STATS_UPGRADE_CLICK_COUNT):
            self._click(config.STATS_UPGRADE_POS[0], config.STATS_UPGRADE_POS[1], relative=True)
            time.sleep(config.STATS_UPGRADE_CLICK_DELAY)
        
        self._click(config.IDLE_CLICK_POS[0], config.IDLE_CLICK_POS[1], relative=True)
        logger.info("========== STAT UPGRADE COMPLETED ==========")
        return State.OPEN_BOXES
    
    def handle_open_boxes(self, current_state):
        self._click(config.IDLE_CLICK_POS[0], config.IDLE_CLICK_POS[1], relative=True)
        
        screenshot = self._capture()
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
                        self._click(x, y, relative=True)
                        boxes_found += 1
        
        if boxes_found > 0:
            logger.info(f"🎁 Opened {boxes_found} boxes")
            self.work_done = True
        
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
        self.failed_click_tracker.clear()
        self._click(config.IDLE_CLICK_POS[0], config.IDLE_CLICK_POS[1], relative=True)
        
        screenshot = self._capture()
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
        
        if self.scroll_direction == 'up':
            logger.info(f"⬆ Scroll UP ({self.scroll_count + 1}/{self.max_scroll_count})")
            self._drag(
                config.SCROLL_END_POS[0], config.SCROLL_END_POS[1],
                config.SCROLL_START_POS[0], config.SCROLL_START_POS[1],
                duration=0.3, relative=True
            )
        else:  # down
            logger.info(f"⬇ Scroll DOWN ({self.scroll_count + 1}/{self.max_scroll_count})")
            self._drag(
                config.SCROLL_START_POS[0], config.SCROLL_START_POS[1],
                config.SCROLL_END_POS[0], config.SCROLL_END_POS[1],
                duration=0.3, relative=True
            )
        
        self._click(config.IDLE_CLICK_POS[0], config.IDLE_CLICK_POS[1], relative=True)
        
        self.scroll_count += 1
        
        if self.scroll_count >= self.max_scroll_count:
            if self.scroll_direction == 'up':
                self.scroll_direction = 'down'
            else:
                self.scroll_direction = 'up'
            self.scroll_count = 0
        
        return State.FIND_RED_ICONS
    
    def handle_check_new_level(self, current_state):
        self._click(config.IDLE_CLICK_POS[0], config.IDLE_CLICK_POS[1], relative=True)
        time.sleep(0.05)
        
        logger.info("Clicking new level button position")
        self._click(config.NEW_LEVEL_BUTTON_POS[0], config.NEW_LEVEL_BUTTON_POS[1], relative=True)
        time.sleep(0.3)
        
        screenshot = self._capture()
        limited_screenshot = screenshot[:config.MAX_SEARCH_Y, :]
        
        if "upgradeStation" in self.templates:
            template, mask = self.templates["upgradeStation"]
            found, confidence, x, y = self.image_matcher.find_template(
                limited_screenshot, template, mask=mask,
                threshold=config.UPGRADE_STATION_THRESHOLD, template_name="upgradeStation",
                check_color=config.UPGRADE_STATION_COLOR_CHECK
            )
            
            if found:
                logger.info(f"Upgrade station found at ({x}, {y}) for level transition")
                self._click(x, y, relative=True)
                time.sleep(2.5)
                return State.TRANSITION_LEVEL
        
        logger.warning("Upgrade station not found for level transition")
        self.scroll_direction = 'down'
        self.scroll_count = 0
        return State.FIND_RED_ICONS
    
    def handle_transition_level(self, current_state):
        self._click(config.IDLE_CLICK_POS[0], config.IDLE_CLICK_POS[1], relative=True)
        
        max_attempts = 5
        
        for attempt in range(max_attempts):
            screenshot = self._capture()
            limited_screenshot = screenshot[:config.MAX_SEARCH_Y, :]
            
            if "newLevel" in self.templates:
                template, mask = self.templates["newLevel"]
                found, confidence, x, y = self.image_matcher.find_template(
                    limited_screenshot, template, mask=mask,
                    threshold=config.NEW_LEVEL_THRESHOLD, template_name="newLevel"
                )
                
                if found:
                    logger.info(f"New level button found at ({x}, {y}) (attempt {attempt + 1})")
                    self._click(x, y, relative=True)
                    time.sleep(1.0)
                    
                    self.total_levels_completed += 1
                    
                    time_spent = 0
                    if self.current_level_start_time:
                        time_spent = (datetime.now() - self.current_level_start_time).total_seconds()
                    
                    self.current_level_start_time = datetime.now()
                    
                    self.telegram.notify_new_level(self.total_levels_completed, time_spent)
                    
                    logger.info(f"Level {self.total_levels_completed} completed. Time spent: {time_spent:.1f}s")
                    logger.info("Waiting for unlock button after level transition")
                    return State.WAIT_FOR_UNLOCK
            
            if attempt < max_attempts - 1:
                time.sleep(0.2)
        
        logger.warning("New level button not found after 5 attempts")
        self.scroll_direction = 'down'
        self.scroll_count = 0
        return State.FIND_RED_ICONS
    
    def handle_wait_for_unlock(self, current_state):
        self._click(config.IDLE_CLICK_POS[0], config.IDLE_CLICK_POS[1], relative=True)
        time.sleep(0.05)
        
        self.wait_for_unlock_attempts += 1
        logger.debug(f"Waiting for unlock button (attempt {self.wait_for_unlock_attempts}/{self.max_wait_for_unlock_attempts})")
        
        if self.wait_for_unlock_attempts > self.max_wait_for_unlock_attempts:
            logger.warning(f"Unlock button not found after {self.max_wait_for_unlock_attempts} attempts, resetting to scroll")
            self.wait_for_unlock_attempts = 0
            self.scroll_direction = 'down'
            self.scroll_count = 0
            return State.FIND_RED_ICONS
        
        screenshot = self._capture()
        
        if "unlock" in self.templates:
            template, mask = self.templates["unlock"]
            found, confidence, x, y = self.image_matcher.find_template(
                screenshot, template, mask=mask,
                threshold=config.UNLOCK_THRESHOLD, template_name="unlock"
            )
            
            if found:
                logger.info(f"Unlock button found at ({x}, {y}) after level transition")
                self._click(x, y, relative=True)
                time.sleep(0.5)
                logger.info("Starting new level")
                self.wait_for_unlock_attempts = 0
                self.scroll_direction = 'down'
                self.scroll_count = 0
                return State.FIND_RED_ICONS
        
        time.sleep(0.3)
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
        try:
            if self.mouse_controller is not None:
                self.mouse_controller.close()
        finally:
            try:
                if self.overlay:
                    self.overlay.stop()
            finally:
                if self.window_capture is not None:
                    self.window_capture.close()
        logger.info("Bot stopped")
