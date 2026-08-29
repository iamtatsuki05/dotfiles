# HTML Report Format

The architectural review is rendered as a single self-contained offline HTML file in the OS temp directory. Use inline CSS, semantic HTML, and inline SVG only. Do not load scripts, fonts, styles, or other resources from a network.

## Scaffold

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Architecture review for {{repo name}}</title>
    <!-- Offline-only: no external scripts or styles. -->
    <style>
      body { margin: 0; background: #fafaf9; color: #0f172a; font-family: system-ui, sans-serif; }
      .report { max-width: 64rem; margin: 0 auto; padding: 3rem 1.5rem; }
      article { margin: 2.5rem 0; padding: 1.5rem; border: 1px solid #e2e8f0; background: white; }
      .diagram-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
      .files { font-family: ui-monospace, monospace; font-size: 0.875rem; }
      .layer { min-height: 3rem; border-left: 4px solid #475569; }
      .seam { stroke-dasharray: 4 4; }
      .leak { stroke: #dc2626; }
      .deep { background: linear-gradient(135deg, #0f172a, #1e293b); }
    </style>
  </head>
  <body>
    <main class="report">
      <header>...</header>
      <section id="candidates">...</section>
      <section id="top-recommendation">...</section>
    </main>
  </body>
</html>
```

## Header

Repo name, date, and a compact legend: solid box = module, dashed line = seam, red arrow = leakage, thick dark box = deep module. No introduction paragraph. Straight into the candidates.

## Candidate card

The diagrams carry the weight. Prose is sparse, plain, and uses the glossary terms (from the `/codebase-design` skill) without ceremony.

Each candidate is one `<article>`:

- **Title**: short, names the deepening (e.g. "Collapse the Order intake pipeline").
- **Badge row**: recommendation strength (`Strong` = emerald, `Worth exploring` = amber, `Speculative` = slate), plus a tag for the dependency category (`in-process`, `local-substitutable`, `ports & adapters`, `mock`).
- **Files**: monospaced list using the locally defined `.files` class.
- **Before / After diagram**: the centrepiece. Two columns, side by side. See patterns below.
- **Problem**: one sentence. What hurts.
- **Solution**: one sentence. What changes.
- **Wins**: bullets, ≤6 words each. e.g. "Tests hit one interface", "Pricing logic stops leaking", "Delete 4 shallow wrappers".
- **ADR callout** (if applicable): one line in an amber-tinted box.

No paragraphs of explanation. If the diagram needs a paragraph to be understood, redraw the diagram.

## Diagram patterns

Pick the pattern that fits the candidate. Mix them. Don't make every diagram look the same. Variety is part of the point.

### Inline SVG graph (the workhorse for dependencies / call flow)

Use an inline `<svg>` with labelled boxes and explicit `<line>` or `<path>` connections when the point is "X calls Y calls Z." Keep repository-derived text in `<text>` nodes after HTML escaping. Use red strokes for leakage and a dark fill for the proposed deep module. Never emit script, event-handler, or external-resource attributes.

### Hand-built boxes-and-arrows (for editorial layouts)

Modules are `<div>` elements with borders and labels. Arrows are inline SVG `<line>` or `<path>` elements positioned over a relative container. Use this when the after diagram should feel like one thick-bordered deep module with greyed-out internals.

### Cross-section (good for layered shallowness)

Stack horizontal bands using the locally defined `.layer` class to show modules a call passes through. Before: 6 thin bands each doing nothing. After: 1 thick band labelled with the consolidated responsibility.

### Mass diagram (good for "interface as wide as implementation")

Two rectangles per module: one for interface surface area, one for implementation. Before: interface rectangle is nearly as tall as the implementation rectangle (shallow). After: interface rectangle is short, implementation rectangle is tall (deep).

### Call-graph collapse

Before: a tree of function calls rendered as nested boxes. After: the same tree collapsed into one box, with the now-internal calls shown faded inside it.

## Style guidance

- Lean editorial, not corporate-dashboard. Use generous whitespace and system fonts defined in the inline stylesheet.
- Colour sparingly: one accent (emerald or indigo) plus red for leakage and amber for warnings.
- Keep diagrams ~320px tall so before/after sits comfortably side by side without scrolling.
- Define a small uppercase label class in the inline stylesheet so module labels read as schematic, not as UI.
- Use no scripts or external resources. Escape repository-derived text before inserting it into HTML or SVG.

## Top recommendation section

One larger card. Candidate name, one sentence on why, anchor link to its card. That's it.

## Tone

Plain English, concise, but the architectural nouns and verbs come straight from the `/codebase-design` skill. Concision is not an excuse to drift.

**Use exactly:** module, interface, implementation, depth, deep, shallow, seam, adapter, leverage, locality.

**Never substitute:** component, service, unit (for module) · API, signature (for interface) · boundary (for seam) · layer, wrapper (for module, when you mean module).

**Phrasings that fit the style:**

- "Order intake module is shallow: interface nearly matches the implementation."
- "Pricing leaks across the seam."
- "Deepen: one interface, one place to test."
- "Two adapters justify the seam: HTTP in prod, in-memory in tests."

**Wins bullets** name the gain in glossary terms: *"locality: bugs concentrate in one module"*, *"leverage: one interface, N call sites"*, *"interface shrinks; implementation absorbs the wrappers"*. Don't write *"easier to maintain"* or *"cleaner code"*, because those terms aren't in the glossary and don't earn their place.

No hedging, no throat-clearing, no "it's worth noting that…". If a sentence could be a bullet, make it a bullet. If a bullet could be cut, cut it. If a term isn't in the `/codebase-design` glossary, reach for one that is before inventing a new one.
