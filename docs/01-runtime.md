# Runtime and baseline fixes

Owner: `bot-runtime`; coordinate with `bot-actions` and `bot-safety` for shared
files and behavior changes.

## Baseline leads (not all still current)

- `main.py` adds `CYCLE_DELAY` after every state update; mouse clicks also sleep.
- `bot.py` repeatedly dismisses panels and searches similar screen regions.
- The red-icon queue index appears to advance only after a blocked offset click.
- Historical lead (not reproduced with current defaults): `STATS_UPGRADE_POS`
  `(270, 304)` does not intersect current zone 1 (`290..350`, `93..270`).
  Protected stats targets still require regression coverage using synthetic config;
  no protection exception is justified by this lead.
- `image_matcher.py` collects all qualifying pixels before suppression and
  recalculates template histograms during color validation.
- `window_capture.py` allocates Win32 capture resources per capture.
- Synchronous Telegram requests formerly blocked gameplay; the queue described
  below now isolates network requests from the action thread.

## Stages

1. Add offline tests in `tests/` for blocked stats input and red-icon progression.
   Establish explicit action results before counting success. Fix queue flow with
   observation invalidation, not unconditional reuse of old coordinates. Resolve
   stats interaction through the safety plan, never by disabling protections.
2. Instrument capture, matching by template/region, input, state execution, and
   deliberate waiting with monotonic timers. Separate work from waiting and keep
   overhead low. Record level completion only after observed confirmation.
3. Replace redundant dismissals and stacked delays with bounded readiness waits.
   Preserve idle backoff, retries, cancellation, and required input durations.
4. Optimize measured matching hotspots: target-local regions with broad fallback,
   cached template preprocessing, and local-peak candidate extraction before
   suppression. Evaluate color-based candidate regions against screenshot fixtures.
   Preserve three-variant agreement until evidence supports changing it.
5. Only if warranted, reuse capture resources with resize/error cleanup, validate
   cropped capture, or move notifications to a bounded non-blocking queue.

## Acceptance and validation

- Offline fake-clock/input tests cover delays, retries, denied actions, and queue
  progression. No denied click is reported as a purchase.
- Detection precision/recall is compared on the same annotated fixtures; record
  missing fixtures as a blocker rather than inventing detection evidence.
- Compare latency distributions, CPU use, action success, and time per level on
  comparable workloads. Windows capture and device latency require separate,
  authorized validation; Linux-only tests cannot establish those improvements.
- Retain a configurable fallback for experimental detector/capture paths until
  validated. Stop or revert only the new optimization if correctness deteriorates.

## Implementation and validation status

Runtime instrumentation now records bounded rolling wall/work/thread-CPU latency
distributions for capture, matching (template and region shape), input, state
execution, and deliberate waits. Nested scopes are not additive; work subtracts
instrumented waiting, not arbitrary blocking I/O. `metrics.snapshot()` exposes
the data. The main loop waits only for the remaining minimum cycle interval,
rather than adding the full cycle delay after work; idle backoff remains.

Merge reconciliation restored full-frame backend call compatibility, connected
input and deliberate waits to metrics, and added main-loop timing regressions.
Bot settling waits remain bounded and pause-aware; they have not been replaced
by visual readiness detection. Red detections are observation-scoped: input
attempts (including failures) invalidate remaining coordinates; blocked offsets
can skip within an unchanged fresh observation. Remembered Y positions no longer
pretend to identify stations. Holds invalidate queued detections, and box opening
reobserves rather than clicking a batch of pre-mutation targets.
`RED_ICON_OBSERVATION_MAX_AGE` defaults to five seconds, measured from before
capture, so normal ADB transfer/detection latency does not exhaust a one-second
budget. Input mutations still invalidate the observation immediately.

Stats purchasing is disabled until panel/control identity evidence exists.
Generic Open/Unlock interactions do not increment completed levels or emit
completion notifications. Successful input transport is not proof of a purchase.
These are conservative baseline repairs, not verified composite gameplay actions.

The merge-repair offline suite passed **150 tests and 88 subtests** on Linux.
Coverage includes synthetic protected stats configuration, red queue invalidation
and progression, denied/raising input, cycle timing, controller wait accounting,
and notification lifecycle races. No device actions or real network sends were
used. This does not complete every stage below.

Offline regression tests live in `tests/`. Run them without starting the bot:

```sh
python -m unittest discover -s tests -v
python tools/benchmark_runtime.py --iterations 100
```

The matcher benchmark compares the unchanged matching body with its instrumented
wrapper on identical seeded synthetic inputs, not annotated game screenshots.
One Linux run (Python 3.14.8, OpenCV 4.14.0, NumPy 2.5.4, one OpenCV thread,
seed 1701, 660x360 image, 100 samples after five warmups) measured:

| Workload | Baseline median / p95 ms | Instrumented median / p95 ms |
| --- | --- | --- |
| Single match with color | 13.076 / 13.478 | 13.095 / 13.508 |
| All matches | 13.501 / 13.903 | 13.499 / 13.892 |

Outputs were identical. This measures instrumentation overhead, not a speedup.
These merge-repair rerun figures use nearest-rank p95, consistent with runtime
metrics, replacing the earlier benchmark's percentile convention.
Instrumented mean calling-thread CPU was 13.122 ms and 13.455 ms respectively
(including warmups); baseline CPU was not separately measured. These dependency
versions differ from the pinned Windows requirements.

### Evidence gates and remaining work

- No annotated screenshot corpus exists. `Assets/demo.mp4` is a possible future
  fixture source, not detection precision/recall evidence.
- Stats panel/purchase identity assets are absent. No coordinate exception is
  authorized; unidentified stats purchase bursts must fail closed.
- `newLevel.png` is a generic Open button and `unlock.png` also appears for
  ordinary stations. They are not reliable completed-level evidence. Unverified
  transitions cannot support completed-level counts or time-per-level claims.
- Histogram caching, local-peak extraction, target-local/broad-fallback detection,
  and color candidate regions remain deferred pending representative hotspot and
  accuracy evidence. The existing detector and three-variant agreement remain.
- Visual readiness waits and verified composite purchase results remain deferred.
  Existing settling delays and bounded retries are retained rather than removed
  without representative UI evidence.
- Asynchronous notifications are implemented: `TelegramNotifier` uses a bounded
  non-blocking queue (default 16) with a daemon background thread. Shutdown is
  bounded and best-effort, not guaranteed delivery or a guaranteed complete flush.
  `main.py` calls `bot.stop()` in its `finally` block, which closes the notifier.
  Offline tests in `tests/test_telegram_notifier.py` cover enqueue latency, queue
  overflow, FIFO ordering, and shutdown.
- Capture-resource reuse/cropped capture remains deferred absent measured need.
  Existing Windows capture code checks PrintWindow failure, uses scoped resource
  cleanup, and rejects changed geometry before publishing capture authority.
  These protections have mocked coverage, not live Windows validation.
- Windows/device capture latency, overall CPU use, action success, and level
  throughput require separate authorized validation. No live bot, physical input,
  purchases, or real notifications were used for this implementation.
