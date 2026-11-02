---
name: principle-redesign-from-first-principles
description: "Apply when integrating a new requirement into an existing design."
user-invocable: false
---

Before applying this entrypoint, read and follow [Local integration policy](../../README.md#local-integration-policy) from this pinned plugin. Do not treat upstream autonomy or fallback instructions as additional authorization.

# Redesign From First Principles

When integrating a change, don't bolt it onto the existing design. Redesign as if the requirement had been a foundational assumption from the start.

- Read all affected files and understand the current design
- Ask: "if we were writing this from scratch with this new requirement, what would we build?"
- Propagate the change through every reference: types, docs, examples, rationale sections
- Think about the whole redesign, then deliver it incrementally

This is the method for preserving option value when integrating changes into an existing design.
