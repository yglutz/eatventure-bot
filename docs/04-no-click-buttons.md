# Named no-click buttons

Owner: `bot-safety`. Depends on shared observations and central input safety.

## Goal

Protect named controls wherever they appear, while retaining static forbidden
zones as fallback. The bot sees pixels, not semantic game widgets.

## Stages

1. Obtain representative screenshots and real templates for the user-selected
   controls to protect, such as premium purchases or advertisements. Define
   stable IDs and screen-specific appearance variants. Missing assets block
   reliable recognition; do not fabricate them.
2. Add configurable blocked IDs, recognition thresholds, and bounding-box margins.
   Return all relevant control detections with identity, rectangle, confidence,
   context, and frame freshness rather than only a best-match center.
3. Associate small icons with their full actionable control bounds where needed;
   protecting only an icon may leave the rest of its button clickable.
4. Apply dynamic exclusions in the shared safety policy immediately before input.
   Reobserve when evidence is stale or context changes. Display/log the identity
   and reason for a block without exposing secrets.
5. In high-risk screens, require positively identified safe actions. A missed
   blocked-button detection must not be interpreted as permission to purchase.

## Acceptance and validation

- Annotated fixtures cover movement, animation, scaling, appearance variants,
  multiple matches, absent controls, and lookalike permitted controls.
- Mocked input tests reject intersections with protected control bounds/margins,
  stale detections, and unrecognized high-risk actions.
- Detection costs are measured and cached per observation, not recomputed for
  each planned click on the same frame.
- Static zones remain enabled; recognition uncertainty is visible in results.
- Document supported appearances and false-negative limitations before live use.
