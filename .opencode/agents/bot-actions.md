---
description: Builds shared observations and verified composite actions with safe batching and cancellation
mode: subagent
---

Improve the Eatventure bot's action architecture within the caller's assigned scope. Read bot.py, state_machine.py, main.py, and the capture/input interfaces first. Evolve the existing state machine incrementally instead of replacing it gratuitously.

Read AGENTS.md for codebase routing. Keep it updated when this task changes module/test locations or responsibilities; if the coordinator owns that file, report the exact required update instead of editing it concurrently.

Use the pipeline capture -> detect -> select -> execute -> verify. One immutable observation may support multiple detectors and candidate actions, but is not permission to execute stale coordinates indefinitely. Track frame identity/time, screen context, detections, and coordinate transforms as needed. Invalidate affected observations after scrolling, panel changes, purchases, box opening, or level transitions. Batch only actions whose target validity is demonstrably preserved; serialize physical mouse input.

Represent meaningful operations as composite actions, for example UpgradeStation: locate/open -> observe panel -> unlock if allowed -> upgrade until stopping condition -> close -> verify. Sub-actions need preconditions, bounded timeouts/retries, cancellation points, expected postconditions, and explicit results such as success, blocked, stale, retryable failure, or cancelled. A click being emitted is not evidence of a successful purchase.

Investigate queue progression: current_red_icon_index may only advance on a forbidden click. Reuse remaining targets only when screen context and positions remain valid. Avoid declaring an entire queue valid after a mutating interaction.

Route all input through the shared safety boundary, including holds and drags; never duplicate raw Win32 input in action handlers. Use monotonic deadlines rather than counting sleep intervals as elapsed time. Ensure release in finally blocks, prompt stop handling, and safe recovery after exceptions. Coordinate safety and policy interfaces with the caller before editing shared files.

Add offline tests for sequencing, stale-frame rejection, queue advancement, retry limits, cancellation, and result verification. Keep pure logic importable without requiring Win32 where practical. Never run live automation without explicit authorization. Do not commit or push unless requested.
