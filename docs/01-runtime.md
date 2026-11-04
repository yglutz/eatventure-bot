# Runtime and baseline fixes

Owner: `bot-runtime`; coordinate with `bot-actions` and `bot-safety` for shared
files and behavior changes.

## Current leads

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
- Enabled Telegram requests can block gameplay for up to their network timeout.

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
| Single match with color | 12.560 / 13.100 | 12.613 / 13.242 |
| All matches | 12.920 / 13.240 | 12.924 / 13.362 |

Outputs were identical. This measures instrumentation overhead, not a speedup.
Instrumented mean calling-thread CPU was 12.683 ms and 12.904 ms respectively
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
- Asynchronous notifications are implemented: `TelegramNotifier` uses a bounded
  non-blocking queue (default 16) with a daemon background thread. `close()`
  flushes pending messages before shutdown. `main.py` calls `close()` in the
  `finally` block. Offline tests in `tests/test_telegram_notifier.py` cover
  enqueue latency, queue overflow, FIFO ordering, and idempotent shutdown.
- Capture-resource reuse/cropped capture remains deferred absent measured need.
  Existing capture error cleanup and unchecked PrintWindow failure remain risks;
  capture instrumentation does not repair them.
- Windows/device capture latency, overall CPU use, action success, and level
  throughput require separate authorized validation. No live bot, physical input,
  purchases, or real notifications were used for this implementation.
