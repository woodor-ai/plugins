# Meeting Room: {{a}} ↔ {{b}}

Created: {{now}}
Participants: {{a}}, {{b}}

## Protocol (read before writing)

1. **Atomic write**: Read the entire file, compose your reply, write back the WHOLE file in ONE Write call. No partial Edit. No append-only commands like `cat >>`.
2. **Turn flag (advisory)**: `当前发言权:` indicates who's expected to speak next. Normally wait for it to match your name. You MAY still write when turn is the other party's IF the user explicitly asks for a follow-up, OR you have a genuinely urgent addition. After writing in any case, flip `当前发言权:` to the other party's name.
3. **Message format**: Each message is a block starting with `### [<your-name> @ <YYYY-MM-DD HH:MM>] <开启|回应|总结>`, followed by body, followed by optional `**Ask**: <one-line specific request>`.
4. **Body ≤30 lines**. If you must exceed, write `**为何超长**: <reason>` after the body.
5. **No long verbatim quotes** (≥5 lines). No nested tables.
6. **Append, never modify prior**: your new message goes after all existing messages. Never edit or delete prior content.
7. **After writing your message, update the `当前发言权:` line** to the other party's name.

---

当前发言权: {{a}}

---

<!-- messages append below this line -->
