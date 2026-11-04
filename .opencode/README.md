# Eatventure improvement agents (OpenCode V2)

Project agents live in `.opencode/agents/`. They inherit the calling session's model; no provider or model subscription is assumed.

Edit project settings and all per-agent permission rules in the root
`opencode.json`. Agent Markdown files retain descriptions, modes, and prompts;
keep permissions centralized rather than duplicating them in frontmatter.
See `docs/opencode-permissions.md` for the complete per-agent permission matrix
and the limits of these defaults.

| Agent | Responsibility |
| --- | --- |
| `bot-improvements` | Coordinator, usable as a primary agent or subagent |
| `bot-runtime` | Profiling, delay/capture/matching optimizations |
| `bot-actions` | Shared observations, composite actions, verification |
| `bot-safety` | Central input protection and identified no-click buttons |
| `bot-strategy` | Rule-based gameplay and selective upgrade exclusions |
| `bot-review` | Read-only correctness/safety review; no shell execution |

## Example requests

Ask your main agent:

```text
Use the bot-improvements agent to assess the current code and propose the first
small implementation stage. Do not implement yet.
```

```text
Use the bot-improvements agent to implement only the first stage: investigate
blocked stats clicks and red-icon queue progression, add offline regression
tests, and have bot-review review the result. Do not run live automation.
```

Or delegate a focused task:

```text
Use the bot-runtime subagent to identify capture and matching hotspots and add
offline timing instrumentation. Do not change gameplay policy or run the bot.
```

The coordinator sequences shared-file edits and supplies child agents with scope
and acceptance criteria. Parallel work should be read-only or own disjoint files.
Implementation agents cannot launch further child agents. Review is read-only.

The agent definitions do not start work automatically. Live Windows/device input,
purchases, and notifications require explicit authorization. Offline tests should
mock capture/input; report Windows-only validation separately. No agent should
commit or push unless asked.

Reference: https://opencode.ai/v2/docs/agents
