---
name: brainstorming
description: Use when a software request may need a feasibility probe, a bounded change to an existing flow, or an architectural design before implementation
---

# Brainstorming Router

Classify the request before choosing the amount of design work. Follow `AGENTS.md`; a label neither grants authority nor adds an approval gate to work already authorized.

## Choose a path

- **Spike** — Feasibility, performance, or behavior is unknown. State the question and cheapest valid probe, keep artifacts throwaway, and report evidence plus a recommendation. Ask first if the probe adds dependencies, cost, external transmission, destructive effects, or material scope.
- **Bounded** — The requested behavior and existing flow are identifiable. Inspect the flow, state the short approach, success condition, and verification, then implement within the user's authorization. Ask only about a material ambiguity. Do not create a design document for a routine change.
- **Architectural** — The work adds a subsystem, changes public boundaries, fixes a data model, or requires migration. Inspect the architecture, surface unresolved decisions, compare viable approaches, and obtain direction before a consequential choice. Record a specification or architecture decision only when the repository or later work needs it.

If hidden complexity appears, stop and move to the heavier path. Never use a lighter label to skip an unknown. Keep every path scoped by YAGNI.
