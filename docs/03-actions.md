# Shared observations and composite actions

Owner: `bot-actions`. Depends on the central safety execution boundary.

## Design

Reuse a screenshot for detection and planning, not as indefinite authority to
execute coordinates. Keep the existing state machine while extracting small,
testable observation and action interfaces. Proposed modules are not current
paths; choose actual names during implementation and update `AGENTS.md` then.

## Stages

1. Define an immutable observation with frame ID, monotonic timestamp, screen
   context, coordinate transform, and detections. Cache detector results within
   that observation; share only the detectors needed for the current context.
2. Define action preconditions, stable target identity, deadline, cancellation,
   expected postcondition, and explicit result. Separate input emission from
   verified gameplay success.
3. Implement one composite operation: locate/open station -> observe panel ->
   unlock if permitted -> upgrade to a bounded stopping condition -> close ->
   verify. Reuse state handlers where sensible; avoid a full rewrite.
4. Invalidate affected observations after scrolling, opening/closing panels,
   purchases, boxes, and level transitions. Reidentify targets before continuing
   queued work if layout or context may have changed.
5. Schedule multiple candidate actions from one observation, but batch execution
   only when earlier actions demonstrably preserve later targets. Serialize all
   physical input. Start conservatively with reobservation at mutation boundaries.
6. Integrate stop handling between sub-actions and within holds/waits. Reset or
   cancel outstanding plans on context changes and unexpected screens.

## Acceptance and validation

- Offline tests demonstrate shared detection on an unchanged frame and mandatory
  reobservation after mutations. A stale queued coordinate never produces input.
- Composite sequencing, queue advancement, bounded recovery, cancellation, and
  postcondition failure are covered with synthetic observations and mocked input.
- Every sub-action passes safety checks immediately before execution.
- Compare captures/matches per successful operation and actual completion time;
  fewer captures alone are not a success criterion.
