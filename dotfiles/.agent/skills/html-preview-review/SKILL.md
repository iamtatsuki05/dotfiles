---
name: html-preview-review
description: "Use only when the user explicitly asks for an HTML preview, browser preview, or visual review board of a completed, verified result. DO NOT USE FOR: unfinished implementation, replacing tests or raw diffs, publishing artifacts, or deliverables that are themselves HTML or slides (this skill builds a separate review board)."
---

# HTML Preview Review

Render completed, verified evidence as a private static HTML review board and show it in exactly one supported presenter. The main agent runs this skill after the required read-only reviewer findings are in; the reviewer never runs it or presents.

## Workflow

0. Confirm the user asked for a preview, then check presenter readiness in one call (Codex in-app Browser controls, or configured Playwright MCP tools). If no supported presenter is ready, report that and stop without generating anything.
1. Finish work and verification; get required fresh read-only reviewer findings. The main agent decides what the board contains.
2. Read [the JSON schema and evidence boundaries](references/schema.md) completely, then create `review.json` from session evidence only.
3. In the active session, create `.agent/work/sessions/<session>/artifacts/html-preview-review/` with mode `0700`; write `review.json` there with mode `0600`. Create the session first if absent.
4. Render:

   ```bash
   python3 <skill-dir>/scripts/render_review.py --input <artifact-dir>/review.json --output <artifact-dir>/index.html
   ```

   The renderer rejects unknown fields and invalid evidence. Fix `review.json`; never patch the renderer.
5. By default, immediately present every generated review with one supported presenter. Select exactly one supported presenter before starting the helper:

   - Codex controls plus its sub-skill: [Codex presenter](references/presenters/codex.md).
   - Otherwise, configured Playwright MCP tools: [Playwright MCP presenter](references/presenters/playwright-mcp.md).
   - If the presenter confirmed in step 0 is no longer available, report the limitation and local artifact path, then stop before starting the helper.

   Read exactly one presenter reference completely. Do not start the helper until the selected presenter readiness checks pass. Do not switch presenters after a readiness, navigation, or verification failure.
6. Start the bundled one-shot loopback helper in the background and read the URL it prints on stdout:

   ```bash
   python3 <skill-dir>/scripts/serve_preview.py --input <artifact-dir>/index.html
   ```

   The helper serves `index.html` once on `127.0.0.1` and exits `0`; it exits non-zero if no request arrives within 30 seconds, so start it only when the presenter is ready to navigate. Follow the reference to navigate the selected presenter to the exact URL. Confirm the title and key sections are visible, then wait for the helper to exit and require status `0`. If navigation fails or the helper does not exit cleanly, terminate it if needed and report the limitation.

## Never

- Open the artifact with an OS viewer or an unrelated browser (`open`, `open -a "Google Chrome"`, `xdg-open`, a `file://` tab), a general-purpose file server, or a persistent or network-visible server, even after a presenter failure.
- Retry a failed presenter attach or navigation more than once, or restart the helper in a loop.
- Upload, publish, or commit the artifact, or invent evidence to fill the board.

## Report

State the artifact path, the presenter used, and the helper exit status. If the board was not displayed, list it as an unmet item with the observed failure, not as a remaining risk; the diffs, tests, and reviewer findings stay the correctness evidence.
