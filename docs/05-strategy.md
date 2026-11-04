# First gameplay strategy

Owner: `bot-strategy`. Depends on verified actions and identifiable targets.

## Goal

Add a deterministic decision layer, not machine learning. Keep policy selection
separate from execution mechanics and mandatory safety rules.

## Stages

1. Define minimal observed state: restaurant/level context, identifiable stations,
   selected focus, observed upgrade availability, and recent verified outcomes.
   Represent unavailable facts as unknown, not guessed values.
2. Establish stable station identity from available icon/text evidence. Do not use
   screen coordinates or remembered Y positions as identity after scrolling.
   Request screenshots or labels if current assets cannot distinguish stations.
3. Define a pure policy interface that ranks allowed candidate actions and emits
   a reason. Start with one user-configurable rule: prioritize a chosen station
   toward a milestone, then use a bounded fallback when progress stalls.
4. Use a milestone only if its level/progress is observable. Otherwise offer a
   clearly labeled bounded-action policy rather than claiming milestone tracking.
5. Scope/reset memory at level transitions. Apply upgrade preferences before
   execution, but never let policy override safety denials.
6. Log decisions and verified outcomes. Add costs/income-based scoring only after
   those values and effects can actually be recognized and validated.

## Acceptance and validation

- Pure tests cover deterministic ranking, focus selection, unknown state, stalled
  progress, fallback, and reset on level/context changes.
- Policy outputs contain intents/IDs, not unverified coordinate sequences.
- Existing behavior can remain an explicit comparison mode during evaluation.
- Evaluate verified purchases, wasted actions, and time per comparable level;
  do not claim the proposed policy is an optimal Eatventure strategy.
- Recognition dependencies and any need for OCR are explicit; no new dependency
  or live device experiment is assumed authorized by this plan.
