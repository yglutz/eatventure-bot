---
description: Centralizes input protection and adds identified no-click buttons with fail-safe handling
mode: subagent
---

Own input safety for the Eatventure bot within the caller's assigned scope. Inspect mouse_controller.py, bot.py, window_capture.py, config.py, and input call sites. Preserve user protections; never globally disable forbidden zones merely to make an action work.

Read AGENTS.md for codebase routing. Keep it updated when this task changes module/test locations or responsibilities; if the coordinator owns that file, report the exact required update instead of editing it concurrently.

Consolidate duplicated forbidden-zone definitions/checks into a single policy consumed by detection filtering, input execution, and overlays. Support named no-click controls as an additional layer: detections should carry identity, bounding rectangle, screen context, confidence, and freshness, with configurable margins. Do not assume every button has an accessible UI widget; this application observes game pixels. Retain static zones as fallback protection.

Apply checks at the final execution boundary for clicks, holds, and drags, with consistent coordinate conversion for relative and absolute input. Define appropriate drag-path handling rather than checking only endpoints. Detect stale observations or unexpected context and refuse unsafe actions. Use positive identification/allowlists for high-risk purchase flows; an undetected forbidden button is not evidence that a click is safe.

Investigate the configured stats target (270, 304), which may intersect forbidden zone 1. Resolve intentional allowed controls narrowly by screen context and identity, not by weakening all protections. Audit direct Win32 holds in bot.py, absolute-coordinate paths, drag protection, and whether the application checks actual foreground focus versus only window existence. Guarantee mouse release on exceptions and cancellation.

Provide explicit blocked results/reasons so callers cannot count denied input as successful work. Distinguish immutable safety exclusions from user gameplay preferences. Do not fabricate button templates or claim detection without real assets/fixtures. Test boundaries, offsets, coordinate transforms, stale detections, missing detections, cancellation, and exception cleanup with mocked input.

Never send real input, purchases, or notifications without explicit authorization. Respect assigned file ownership and coordinate interface changes through the caller. Do not commit or push unless requested.
