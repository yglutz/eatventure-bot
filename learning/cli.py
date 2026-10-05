import argparse
import time
import signal
import math
from pathlib import Path

from learning.core import Frames, Gems, Recording, map_demo


def collect(interface, bot, controls, args, writer):
    recording = Recording(args.data, args.budget_gb)
    frames, gems = Frames(), Gems()
    generation = controls.generation
    count = 0
    last_report = 0.

    def observe(phase, kind, start, end, duration, result):
        nonlocal count, generation
        if controls.generation != generation:
            frames.frames.clear()
            gems.reset()
            generation = controls.generation
        if phase == 'before':
            image, value, confidence, supported = interface.snapshot()
            delta, confirmed = gems.read(value, confidence, controls.active_time())
            if gems.decreased:
                controls.pause()
            return (frames.add(image), map_demo(kind, start, end, duration, interface.mask()),
                    {'timestamp': time.time(), 'kind': kind, 'start': start, 'end': end,
                     'duration': duration, 'gems': value, 'confidence': confidence,
                     'confirmed': confirmed, 'delta': delta, 'supported': supported})
        token, completed, executed = result
        observation, action, metadata = token
        if executed and metadata['supported']:
            # Keep all executed actions for audit, including unmappable demonstrations.
            ok = recording.write(observation, -1 if action is None or not completed else action,
                                 metadata | {'completed': completed})
            count += int(ok)
            print(f'Collected {count}: {kind}, mapped={action}, completed={completed}', flush=True)
            writer.add_scalar('collection/examples', count, count)
            if not ok:
                print('Recording budget reached. Recording paused; existing data retained.')
                controls.pause()

    bot.mouse_controller.observer = observe
    original_hold_limit = bot.mouse_controller.max_hold_duration
    bot.mouse_controller.max_hold_duration = 1.
    try:
        while count < args.steps and not controls.stopped:
            controls.wait()
            if recording.full:
                controls.pause()
                continue
            if interface.ready():
                try:
                    bot.state_machine.update()
                except (RuntimeError, OSError) as error:
                    controls.pause()
                    print(f'Capture/input interrupted; paused for review: {error}', flush=True)
            else:
                if time.monotonic() - last_report > 10:
                    image, value, confidence, supported = interface.snapshot()
                    import cv2
                    Path('screenshots').mkdir(exist_ok=True)
                    cv2.imwrite('screenshots/last-unsupported.png', image)
                    print(f'Waiting: focused={interface.capture.is_window_active()}, '
                          f'supported={supported}, gems={value}, confidence={confidence:.3f}', flush=True)
                    last_report = time.monotonic()
                interface.sleep(.2)
    finally:
        bot.mouse_controller.max_hold_duration = original_hold_limit
        bot.mouse_controller.observer = None
        bot.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Screenshot learning agent; Z start/pause, P exit')
    parser.add_argument('mode', choices=['calibrate', 'collect', 'pretrain', 'train', 'evaluate'])
    parser.add_argument('--calibration', default='models/calibration.json')
    parser.add_argument('--data', default='data/demonstrations')
    parser.add_argument('--checkpoints', default='models/checkpoints')
    parser.add_argument('--logs', default='logs/learning')
    parser.add_argument('--resume')
    parser.add_argument('--steps', type=int, default=256)
    parser.add_argument('--epochs', type=int, default=10)
    parser.add_argument('--budget-gb', type=float, default=10.)
    parser.add_argument('--simulate', action='store_true', help='Offline training/evaluation smoke test')
    parser.add_argument('--gems-roi', nargs=4, type=int, metavar=('X','Y','W','H'))
    parser.add_argument('--interaction-roi', nargs=4, type=int, action='append', metavar=('X','Y','W','H'))
    parser.add_argument('--start', action='store_true', help='Start immediately (Windows focuses game; ADB requires it already focused)')
    parser.add_argument('--max-seconds', type=float, help='Stop after this wall-clock session duration')
    parser.add_argument('--verify-only', action='store_true', help='Read-only live calibration verification; defaults to 20 captures')
    parser.add_argument('--backend', choices=['window', 'adb'], default='window')
    parser.add_argument('--adb-serial', help='Explicit device serial or WiFi host:port')
    parser.add_argument('--adb-package', help='Expected foreground Android package')
    parser.add_argument('--adb-connect', action='store_true', help='Connect the explicit WiFi serial')
    parser.add_argument('--adb-path', default='adb')
    parser.add_argument('--adb-timeout', type=float, default=10.)
    parser.add_argument('--animation-reference', action='append', help='Operator-reviewed normal PNG; repeat 8..32 times, never use holdout/negative frames')
    parser.add_argument('--animation-roi', nargs=4, type=int, action='append', metavar=('X','Y','W','H'))
    parser.add_argument('--cash-roi', nargs=4, type=int, metavar=('X','Y','W','H'))
    parser.add_argument('--negative-reference', action='append', default=[], help='Known unsafe PNG for read-only verification')
    parser.add_argument('--verification-checks', type=int, default=20)
    parser.add_argument('--approve-calibration', action='store_true', help='Explicitly approve animation candidate only after live and negative verification')
    args = parser.parse_args(argv)
    if args.steps <= 0 or args.epochs <= 0 or args.budget_gb <= 0:
        parser.error('steps, epochs and budget must be positive')
    if args.max_seconds is not None and (not math.isfinite(args.max_seconds) or args.max_seconds <= 0):
        parser.error('max-seconds must be positive')
    if args.mode == 'evaluate' and not args.resume:
        parser.error('evaluate requires --resume')
    if args.simulate and args.mode in ('calibrate', 'collect'):
        parser.error('calibrate/collect require a real game window')
    live_adb = args.backend == 'adb' and not args.simulate and args.mode != 'pretrain'
    if live_adb and (not args.adb_serial or not args.adb_package):
        parser.error('ADB requires --adb-serial and --adb-package')
    if not math.isfinite(args.adb_timeout) or args.adb_timeout <= 0:
        parser.error('adb-timeout must be finite and positive')
    if live_adb and args.mode == 'calibrate' and not args.verify_only and (not args.gems_roi or not args.interaction_roi):
        parser.error('Headless ADB calibration requires --gems-roi and --interaction-roi')
    if args.approve_calibration and (args.mode != 'calibrate' or not args.verify_only or not args.negative_reference):
        parser.error('Approval requires calibrate --verify-only and --negative-reference')
    if args.animation_reference and (args.mode != 'calibrate' or args.verify_only or not args.animation_roi or not args.gems_roi or not args.interaction_roi):
        parser.error('Animation creation requires calibrate, animation/gems/interaction ROIs, and no --verify-only')
    if args.animation_reference and not 8 <= len(args.animation_reference) <= 32:
        parser.error('Animation calibration requires 8..32 operator-approved references')
    if not 10 <= args.verification_checks <= 1000:
        parser.error('verification-checks must be 10..1000')
    from learning.environment import Controls, GameEnv, LiveInterface, SimulatedInterface
    from learning.training import create, load_checkpoint, pretrain, save_checkpoint
    from torch.utils.tensorboard import SummaryWriter
    controls = Controls(paused=not (args.simulate or args.start))
    writer = SummaryWriter(str(Path(args.logs) / args.mode / time.strftime('%Y%m%d-%H%M%S')))
    model = env = bot = None
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    def terminate(signum, frame):
        controls.stop_reason = 'SIGTERM'
        controls.stopped = True
        controls.release()
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        if args.simulate or args.mode == 'pretrain':
            interface = SimulatedInterface()
            controls = Controls(paused=False)
        else:
            import config
            from mouse_controller import MouseController
            from learning.vision import Vision, calibrate
            if live_adb:
                from adb_capture import ADBCapture
                capture = ADBCapture(args.adb_serial, args.adb_package, adb_path=args.adb_path,
                                     timeout=args.adb_timeout, connect=args.adb_connect)
                if args.mode == 'calibrate':
                    # A new connection can briefly display an Android banner.
                    time.sleep(12)
                capture.capture()
                mouse = MouseController(capture=capture)
                if args.mode == 'collect':
                    from bot import EatventureBot
                    bot = EatventureBot(capture=capture, mouse=mouse)
            elif args.mode == 'collect':
                from bot import EatventureBot
                bot = EatventureBot()
                capture, mouse = bot.window_capture, bot.mouse_controller
            else:
                from window_capture import WindowCapture
                capture = WindowCapture(config.WINDOW_TITLE, config.WINDOW_WIDTH, config.WINDOW_HEIGHT)
                mouse = MouseController(capture.hwnd)
            def load_vision():
                vision = Vision(args.calibration)
                source = capture.calibration_identity() if live_adb else {'backend': 'window'}
                if vision.data.get('source', {'backend': 'window'}) != source:
                    raise ValueError('Calibration capture source mismatch; recalibrate in a separate directory')
                return vision
            if args.mode == 'calibrate':
                if args.verify_only:
                    from learning.calibration import verify_calibration
                    passed = verify_calibration(capture, args.calibration,
                                                checks=args.verification_checks,
                                                negative_paths=args.negative_reference,
                                                approve=args.approve_calibration)
                    return 0 if passed else 1
                elif args.animation_reference:
                    from learning.calibration import create_animation_calibration
                    create_animation_calibration(capture, args.calibration, args.animation_reference,
                                                 args.gems_roi, args.interaction_roi,
                                                 args.animation_roi, args.cash_roi)
                else:
                    calibrate(capture, args.calibration, args.gems_roi, args.interaction_roi)
                return 0
            interface = LiveInterface(capture, mouse, load_vision(), controls)
            if live_adb:
                controls.start_console()
            else:
                controls.start()
            if args.start and not live_adb:
                import win32gui
                win32gui.SetForegroundWindow(capture.hwnd)
            print('Ready: keep the game focused; Z to start/pause, P to exit.' +
                  (' Enter commands followed by Enter; Ctrl+C also stops. ADB: taps only.' if live_adb else ''), flush=True)
        if args.max_seconds:
            controls.deadline = time.monotonic() + args.max_seconds
        if args.mode == 'collect':
            collect(interface, bot, controls, args, writer)
            return 0
        env = GameEnv(interface, controls, writer)
        model = load_checkpoint(args.resume, env) if args.resume else create(env, args.logs)
        model.checkpoint_directory = args.checkpoints
        if args.mode == 'pretrain':
            pretrain(model, args.data, args.epochs, writer)
        elif args.mode == 'train':
            model.tensorboard_log = args.logs
            model.learn(total_timesteps=args.steps, reset_num_timesteps=False)
        else:
            observation, _ = env.reset()
            model.policy.set_training_mode(False)
            for _ in range(args.steps):
                action, _ = model.predict(observation, deterministic=True, action_masks=env.action_masks())
                observation, reward, _, _, info = env.step(action)
            print('Simulated evaluation metrics:' if args.simulate else 'Evaluation metrics:', info)
        return 0
    except KeyboardInterrupt:
        print('Stop reason:', controls.stop_reason or 'interrupt')
        print('Stopped; saving training state.' if model is not None and args.mode in ('train','pretrain')
              else 'Stopped; completed recording files are retained.')
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        controls.close()
        if model is not None and args.mode in ('train', 'pretrain'):
            path = save_checkpoint(model, args.checkpoints)
            print('Checkpoint:', path)
        if env:
            env.close()
        if bot:
            bot.stop()
        writer.close()
