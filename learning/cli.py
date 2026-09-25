import argparse
import time
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
    parser.add_argument('--start', action='store_true', help='Focus game and start immediately; Z/P remain active')
    parser.add_argument('--max-seconds', type=float, help='Stop after this wall-clock session duration')
    parser.add_argument('--verify-only', action='store_true', help='Check existing calibration against ten live captures')
    args = parser.parse_args(argv)
    if args.steps <= 0 or args.epochs <= 0 or args.budget_gb <= 0:
        parser.error('steps, epochs and budget must be positive')
    if args.max_seconds is not None and args.max_seconds <= 0:
        parser.error('max-seconds must be positive')
    if args.mode == 'evaluate' and not args.resume:
        parser.error('evaluate requires --resume')
    if args.simulate and args.mode in ('calibrate', 'collect'):
        parser.error('calibrate/collect require a real game window')
    from learning.environment import Controls, GameEnv, LiveInterface, SimulatedInterface
    from learning.training import create, load_checkpoint, pretrain, save_checkpoint
    from torch.utils.tensorboard import SummaryWriter
    controls = Controls(paused=not (args.simulate or args.start))
    writer = SummaryWriter(str(Path(args.logs) / args.mode / time.strftime('%Y%m%d-%H%M%S')))
    model = env = bot = None
    try:
        if args.simulate or args.mode == 'pretrain':
            interface = SimulatedInterface()
            controls = Controls(paused=False)
        else:
            import config
            from window_capture import WindowCapture
            from mouse_controller import MouseController
            from learning.vision import Vision, calibrate
            if args.mode == 'collect':
                from bot import EatventureBot
                bot = EatventureBot()
                capture, mouse = bot.window_capture, bot.mouse_controller
            else:
                capture = WindowCapture(config.WINDOW_TITLE, config.WINDOW_WIDTH, config.WINDOW_HEIGHT)
                mouse = MouseController(capture.hwnd)
            if args.mode == 'calibrate':
                if args.verify_only:
                    import cv2
                    vision = Vision(args.calibration)
                    for i in range(10):
                        image = capture.capture()
                        print('Calibration check:', vision.supported(image), vision.gems(image), flush=True)
                        time.sleep(.5)
                    cv2.imwrite(str(Path(args.calibration).parent/'verification.png'), image)
                else:
                    calibrate(capture, args.calibration, args.gems_roi, args.interaction_roi)
                return 0
            interface = LiveInterface(capture, mouse, Vision(args.calibration), controls)
            controls.start()
            if args.start:
                import win32gui
                win32gui.SetForegroundWindow(capture.hwnd)
            print('Ready: focus the game, then Z to start/pause; P to exit.', flush=True)
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
        controls.close()
        if model is not None and args.mode in ('train', 'pretrain'):
            path = save_checkpoint(model, args.checkpoints)
            print('Checkpoint:', path)
        if env:
            env.close()
        if bot:
            bot.stop()
        writer.close()
