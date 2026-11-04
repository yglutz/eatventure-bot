# OpenCode V2 permission matrix

Source of project rules: root `opencode.json`. Agent Markdown files contain no
permission rules. Shared rules are explicit at the top level; agent overrides
are appended afterward. Last matching rule wins. This matrix describes project
configuration, not an audit of global/managed policies or saved approvals.

Legend: **Allow** = no prompt, **Ask** = approval required, **Deny** = blocked.

| Action / resource | improvements | runtime | actions | safety | strategy | review |
| --- | --- | --- | --- | --- | --- | --- |
| `read` ordinary paths | Allow | Allow | Allow | Allow | Allow | Allow |
| `read` `*.env`, `*.env.*` | Ask | Ask | Ask | Ask | Ask | Ask |
| `read` `*.env.example` | Allow | Allow | Allow | Allow | Allow | Allow |
| `edit` ordinary paths | Allow | Allow | Allow | Allow | Allow | Deny |
| `edit` `*.env`, `*.env.*` | Ask | Ask | Ask | Ask | Ask | Deny |
| `edit` `*.env.example` | Allow | Allow | Allow | Allow | Allow | Deny |
| `glob` | Allow | Allow | Allow | Allow | Allow | Allow |
| `grep` | Allow | Allow | Allow | Allow | Allow | Allow |
| `shell` | Ask | Ask | Ask | Ask | Ask | Deny |
| `subagent` five specialist IDs below | Allow | Deny | Deny | Deny | Deny | Deny |
| `subagent` any other ID | Deny | Deny | Deny | Deny | Deny | Deny |
| `skill` | Allow | Allow | Allow | Allow | Allow | Allow |
| `question` | Allow | Allow | Allow | Allow | Allow | Allow |
| `webfetch` | Allow | Allow | Allow | Allow | Allow | Allow |
| `websearch` | Allow | Allow | Allow | Allow | Allow | Allow |
| `experimental_image_generate` | Ask | Ask | Ask | Ask | Ask | Ask |
| `external_directory` | Ask | Ask | Ask | Ask | Ask | Ask |
| `execute` | Allow | Allow | Allow | Allow | Allow | Allow |
| `opencode_list_mcp_resources` | Ask | Ask | Ask | Ask | Ask | Ask |
| `opencode_read_mcp_resource` | Ask | Ask | Ask | Ask | Ask | Ask |
| `<server>_<tool>` MCP tools | Ask | Ask | Ask | Ask | Ask | Ask |
| Other/plugin actions (`*` fallback) | Ask | Ask | Ask | Ask | Ask | Ask |

Column names omit the `bot-` prefix. The coordinator may launch only
`bot-runtime`, `bot-actions`, `bot-safety`, `bot-strategy`, and `bot-review`.
Running a coordinator as a child may additionally be limited by OpenCode's
subagent-depth setting; no experimental depth setting is enabled here.

## Interpretation and limits

- Shared defaults also apply to other agents used in this project; their own
  rules can refine them. No model or default-agent selection is imposed.
- `edit` covers edit/write/patch tools. `shell` covers tests as well as other
  commands; the reviewer cannot run tests. Approval before shell execution is
  deliberate, rather than trying to enumerate every dangerous command.
- `execute` permits the Code Mode wrapper, not unrestricted nested tools.
  Nested calls still enforce their own permissions. Keeping it enabled avoids
  hiding read-only tools that may be available only through Code Mode.
- MCP action names vary by installed server/tool; the catch-all Ask rule covers
  them without inventing server IDs. MCP tools can have side effects: inspect
  requests, especially for the reviewer. Read-only here is enforced for native
  edit/shell tools, not a universal sandbox for arbitrary integrations.
- External access needs approval in addition to applicable read/edit checks.
  This does not create a process sandbox; shell commands run with host authority.
- Environment-file exceptions protect matching read/edit paths, not secrets in
  every possible filename or content. Grep/shell/MCP can access data through other
  routes; review their use accordingly. Do not treat these rules as secret isolation.
- Project instructions still prohibit unapproved live automation, purchases,
  notifications, commits, and pushes. A tool approval is not a reason to ignore
  those instructions. Do not approve a broad shell pattern for those operations.
- Saved Allow-always approvals can remove future Ask prompts, but cannot override
  Deny. Managed hard-deny policies can further restrict these project defaults.

When changing permissions, update this matrix in the same change. Keep broad
rules before narrower exceptions. Reference:
https://opencode.ai/v2/docs/permissions
