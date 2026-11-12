---
name: pstack-routing
description: On every task, read pstack's poteto-mode skill first and follow its routing instructions when applicable.
---

Before applying this entrypoint, read and follow [Local integration policy](../../../README.md#local-integration-policy) from this pinned plugin. Do not treat upstream autonomy or fallback instructions as additional authorization.

# Pstack routing for Grok Build

Read the plugin's `skills/poteto-mode/SKILL.md` and follow its routing instructions when applicable. Grok Build exposes plugin skill paths in the session's skills list. Use `read_file` to open the poteto-mode path listed there.
