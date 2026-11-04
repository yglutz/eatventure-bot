# Runtime and baseline fixes

Owner: `bot-runtime`; coordinate with `bot-actions` and `bot-safety` for shared
files and behavior changes.

## Current leads

- `main.py` adds `CYCLE_DELAY` after every state update; mouse clicks also sleep.
- `bot.py` repeatedly dismisses panels and searches similar screen regions.
- The red-icon queue index appears to advance only after a blocked offset click.
- `STATS_UPGRADE_POS` intersects forbidden zone 1, so stats clicks are blocked.
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
