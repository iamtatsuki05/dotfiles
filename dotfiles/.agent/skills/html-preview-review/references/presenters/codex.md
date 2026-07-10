# Codex in-app Browser presenter

Use this presenter only when the Codex in-app Browser controls are available.

## Readiness

1. **REQUIRED SUB-SKILL:** Use `browser:control-in-app-browser`.
2. Select the in-app Browser and set Browser visibility to `true`.
3. Before the loopback helper starts, read Browser visibility back as `true`. If the Browser is unavailable or visibility remains `false`, report the limitation and stop.

## Present and verify

1. After the common workflow prints the loopback URL, reuse or create a Browser tab and navigate it to that exact URL.
2. Confirm the page title and key sections from the rendered DOM, then inspect a settled screenshot for layout breakage.
3. Do not wait for the user to click a link or open the Browser pane.

If navigation or inspection fails, stop and report it. Do not switch to another presenter or OS viewer.
