# Central input safety

Owner: `bot-safety`. Prerequisite: baseline mocked input tests.

## Scope

`mouse_controller.py`, input call sites in `bot.py`, protection settings in
`config.py`, and overlay rendering in `window_capture.py`. Agree on action-result
and observation interfaces with `bot-actions` before shared edits.

## Stages

1. Inventory every click, hold, drag, raw Win32 call, coordinate system, and
   forbidden-zone definition. Test existing behavior before consolidating it.
2. Introduce one safety policy shared by final input execution, candidate
   filtering, and overlays. Normalize coordinates and validate window bounds,
   context, and foreground focus before input; window existence alone is not focus.
3. Return explicit allowed/blocked/stale/cancelled results with reasons. Route
   station holds through this boundary; cover absolute input and define drag-path
   protection explicitly, not just endpoint checks.
4. Guarantee release using `try/finally`. Check cancellation and focus during
   long operations, and ensure shutdown releases any held button. Use monotonic
   deadlines and bounded retries.
5. Resolve stats-target protection narrowly: require a positively identified stats
   screen/control before allowing an intended action. Do not create a blanket
   coordinate exception or remove the existing zone as a convenience.

## Acceptance and validation

- Mocked tests cover zone edges, offsets, coordinate conversion, out-of-window
  input, stale context, lost focus, protected drag paths, and denied holds.
- Injected exceptions and stop requests always release held input promptly.
- All production input passes through the policy; blocked inputs do not increment
  success counters. The overlay displays the same static protections as execution.
- Static protections remain effective when dynamic recognition is unavailable.
- Live Windows testing is a separate, explicitly authorized step. No test should
  send physical input or buy anything by default.
