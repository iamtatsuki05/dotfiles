---
name: brainstorming
description: "Use when a software request may need a feasibility probe, a bounded change to an existing flow, or an architectural design before implementation. Not for pure questions, non-software writing, or work whose approach and acceptance criteria are already fixed."
---

# Brainstorming Router

Classify a software request into one of three paths before deciding how much design work to do. Follow `AGENTS.md`; a label neither grants authority nor adds an approval gate to work already authorized.

## Choose a path

- **Spike** — Feasibility, performance, or behavior is unknown. State the question and the cheapest valid probe, keep artifacts throwaway, and report evidence plus a recommendation. Ask first if the probe adds dependencies, cost, external transmission, destructive effects, or material scope.
- **Bounded** — The requested behavior and the existing flow are identifiable. Inspect the flow, state the short approach, success condition, and verification, then implement within the user's authorization. Ask only about a material ambiguity. Do not create a design document for a routine change.
- **Architectural** — The work adds a subsystem, changes public boundaries, fixes a data model, or requires migration. Inspect the architecture, surface unresolved decisions, compare viable approaches, and obtain direction before a consequential choice. Record a specification or architecture decision only when the repository or later work needs it.

If hidden complexity appears, stop and move to the heavier path. Never use a lighter label to skip an unknown. Keep every path scoped by YAGNI.

## Output

For each request give the label, a one-line approach, the next user decision (or "none"), and the durable artifact the path produces: spike, an evidence report; bounded, the change plus its verification; architectural, a decision record only when needed. Keep the routing note to a few lines; the design work itself happens on the chosen path.
