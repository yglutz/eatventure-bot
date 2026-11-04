# Selective upgrade exclusions

Owner: `bot-strategy`; coordinate recognition/safety with `bot-safety`.
Depends on the policy interface and verified action execution.

## Problem

The generic `upgradeStation` template identifies an action button, not the offered
upgrade. Stats currently click a fixed position repeatedly. Neither mechanism
alone can enforce an individual-upgrade blocklist.

## Stages

1. Clarify requested exclusions: entire stations, specific named upgrades, or
   stop-at-level milestones. Ask for actual upgrade identities/screenshots when
   ambiguous; these are distinct policies.
2. Add stable upgrade IDs and recognition from icons, text, or both. Record the
   owning station/screen and confidence. Use OCR only if necessary and authorized;
   ambiguous and unknown identities must remain explicit.
3. Define block/allow preferences and an unknown-target policy. Mandatory safety
   denials always win; explicit upgrade blocks win over upgrade allowances.
   Default uncertain purchases to skip in safety-sensitive operation.
4. Replace blind stats click bursts with observe -> identify -> decide -> buy ->
   verify -> reobserve, because purchases may move the next item into the same
   position. Check identity immediately before each purchase.
5. Audit station holds for overshoot. If a hold can cross a blocked milestone or
   upgrade without a reliable observation boundary, use bounded verified inputs
   or refuse it. Do not promise exclusions that the available input model cannot
   enforce.
6. Log skipped IDs/reasons and distinguish unavailable, unaffordable, blocked,
   unknown, and purchased outcomes. Keep exclusion state scoped appropriately
   across station/level transitions.

## Acceptance and validation

- Fixture tests distinguish target upgrades, lookalikes, changed row positions,
  unknown identities, and missing recognition assets.
- Mocked action tests prove zero purchase input for blocked/uncertain upgrades,
  correct precedence, reidentification after list shifts, and hold stop behavior.
- Allowed actions still require safety approval and observed success.
- Document exactly which upgrade types can be recognized/enforced. Missing data
  is a limitation, not evidence that all exclusions work.
