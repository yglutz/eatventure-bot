import logging
import os
from pathlib import Path
import sys
import time
import argparse
import queue

import config
from bot import EatventureBot
from runtime_metrics import metrics

current_match_index = 0
current_matches = []
bot_instance = None
z_pressed = False
should_exit = False
hotkey_controls = None


def on_press(key):
    global current_match_index, bot_instance, z_pressed, current_matches, should_exit
    try:
        if hasattr(key, 'char'):
            if key.char == 'x':
                logger = logging.getLogger(__name__)
                if bot_instance and bot_instance.window_capture.hwnd:
                    backend = bot_instance.window_capture.backend
                    screen_x, screen_y = backend.get_cursor_pos()
                    win_x, win_y = backend.validate()[:2]
                    rel_x = screen_x - win_x
                    rel_y = screen_y - win_y
                    logger.info(f"[X pressed] Window position: ({rel_x}, {rel_y})")
                else:
                    logger.info(f"[X pressed] Bot not initialized yet")
            elif key.char == 'z':
                if hotkey_controls is not None:
                    hotkey_controls.submit('toggle')
            elif key.char == 'p':
                logger = logging.getLogger(__name__)
                logger.info("[P pressed] Exiting program...")
                if hotkey_controls is not None:
                    hotkey_controls.submit('quit')
    except Exception:
        logging.getLogger(__name__).exception("Hotkey handling failed")


def setup_logging():
    logs_dir = Path(config.LOGS_DIR)
    logs_dir.mkdir(exist_ok=True)
    
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    log_level = logging.DEBUG if config.DEBUG else logging.INFO
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(log_format))
    
    file_handler = logging.FileHandler(logs_dir / 'bot.log', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format))
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)


def select_backend(requested):
    if requested != 'auto':
        return requested
    wayland = (os.environ.get('WAYLAND_DISPLAY') or
               os.environ.get('XDG_SESSION_TYPE', '').lower() == 'wayland')
    return 'adb' if sys.platform.startswith('linux') and wayland else 'desktop'


def main(argv=None):
    global bot_instance, hotkey_controls
    
    print("=" * 60)
    print("Eatventure Bot - Screen Automation Tool")
    print("=" * 60)
    print(f"Window Title: {config.WINDOW_TITLE}")
    print(f"Match Threshold: {config.MATCH_THRESHOLD * 100}%")
    print(f"Templates Directory: {config.TEMPLATES_DIR}")
    print("=" * 60)
    
    setup_logging()
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', choices=('auto', 'desktop', 'adb'), default=config.AUTOMATION_BACKEND)
    parser.add_argument('--adb-serial', default=config.ADB_SERIAL)
    parser.add_argument('--enable-adb-input', action='store_true',
                        help='EXPERIMENTAL: opt in to gestures after manual crop calibration')
    args = parser.parse_args(argv)
    selected = select_backend(args.backend)
    
    listener = None
    bot = None
    controls = None
    try:
        if selected == 'desktop' and sys.platform.startswith("linux") and (
            os.environ.get("WAYLAND_DISPLAY")
            or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        ):
            raise RuntimeError(
                "Wayland/XWayland desktop automation is unsupported; use a native X11 "
                "session for desktop gameplay. For capture-only Android calibration, "
                "use adb_calibrate.py, or --backend adb for experimental Android integration."
            )
        if selected == 'adb':
            from adb_backend import ADBBackend
            from window_capture import WindowCapture
            backend = ADBBackend(
                serial=args.adb_serial, package=config.ADB_PACKAGE,
                native_size=config.ADB_NATIVE_SIZE, crop=config.ADB_CROP,
                logical_size=(int(config.WINDOW_WIDTH), int(config.WINDOW_HEIGHT)),
                enable_input=args.enable_adb_input,
                cancel_check=lambda: bot is None or not bot.running)
            capture = WindowCapture(backend=backend)
            try:
                # Startup is capture-only, even when input has been opted in.
                frame = capture.capture()
                if frame is None or frame.size == 0:
                    raise RuntimeError('ADB startup capture failed')
                bot = EatventureBot(window_capture=capture)
            except BaseException:
                capture.close()
                raise
        else:
            bot = EatventureBot()
        bot_instance = bot
        # Validate desktop startup before pynput can connect or start a thread.
        if selected == 'desktop':
            from terminal_controls import CommandQueue
            controls = CommandQueue(bot.pause)
            hotkey_controls = controls
            from pynput import keyboard
            listener = keyboard.Listener(on_press=on_press)
            listener.start()
        else:
            from terminal_controls import TerminalControls
            controls = TerminalControls(sys.stdin, bot.pause)
            controls.start()
            print('ADB PAUSED. Candidate crop is UNAPPROVED; inspect with adb_calibrate.py. '
                  'Experimental gestures require manual calibration and --enable-adb-input. '
                  'Terminal commands: start, pause, status, quit; Ctrl+C exits.')
        
        logger = logging.getLogger(__name__)
        logger.info("Bot initialized and ready")
        if selected == 'desktop':
            logger.info("Press Z to START/STOP the bot")
            logger.info("Press X to see window-relative cursor position")
            logger.info("Press P to EXIT the program")
        
        while not should_exit:
            if controls is not None:
                while True:
                    try:
                        command, token = controls.commands.get_nowait()
                    except queue.Empty:
                        break
                    if command == 'quit':
                        return 0
                    if command == 'start':
                        if selected == 'adb' and not args.enable_adb_input:
                            logger.warning('Start rejected: --enable-adb-input required after manual calibration')
                        elif not bot.running:
                            if controls.try_start(token, bot.resume):
                                bot.telegram.notify_bot_started()
                    elif command == 'pause':
                        bot.pause()
                        bot.telegram.notify_bot_stopped()
                    elif command == 'status':
                        logger.info('ADB %s; input opt-in=%s', 'RUNNING' if bot.running else 'PAUSED', args.enable_adb_input)
            if bot.running:
                cycle_started = metrics.clock()
                try:
                    bot.state_machine.update()
                except Exception:
                    was_running = bot.running
                    bot.pause()
                    # Never resume a partially executed action with old targets.
                    from state_machine import State
                    bot.state_machine.transition(State.FIND_RED_ICONS)
                    if was_running:
                        raise
                metrics.wait_remaining(cycle_started, config.CYCLE_DELAY)
            else:
                metrics.wait(0.05 if controls is not None else 0.5)
        
        logger.info("Program exiting...")
        
    except KeyboardInterrupt:
        logging.info("\nBot stopped by user (Ctrl+C)")
        return 0
    except Exception as e:
        logging.error(f"\nFatal error: {e}", exc_info=True)
        return 1
    finally:
        if controls is not None:
            try:
                controls.close()
            except Exception:
                logging.exception('Terminal controls cleanup failed')
        # A partially started pynput listener may lack backend attributes.
        # Cleanup failures must not replace the original startup diagnostic.
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                logging.exception("Keyboard listener cleanup failed")
        if bot is not None:
            try:
                bot.stop()
            except Exception:
                logging.exception("Bot cleanup failed")
        bot_instance = None
        hotkey_controls = None
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
