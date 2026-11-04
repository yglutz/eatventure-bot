# Code guidelines

Apply these guidelines to new and changed code; avoid unrelated rewrites.

- Optimize for human readability: use descriptive names, explicit control flow,
  early returns, and comments that explain why rather than repeat what.
- Give each function, class, and module one clear responsibility. Split code
  when it mixes concerns or becomes hard to explain; avoid god functions/classes.
- Keep layers thin: orchestration coordinates work, domain logic makes decisions,
  and adapters handle capture, input, storage, or notifications. Keep side effects
  out of pure decision logic.
- Group related responsibilities into focused submodules/packages with clear
  interfaces. Keep dependencies one-way; avoid circular imports and catch-all
  `utils` modules. Update `AGENTS.md` when module paths change.
- Prefer simple functions and composition. Add abstractions only when they
  clarify a real boundary; avoid needless wrappers, deep inheritance, and
  speculative frameworks. Do not split code solely to meet a line-count limit.
- Keep dependencies and state explicit. Test decision logic offline and mock
  external effects; preserve existing safety checks when extracting code.
