# Playwright MCP presenter

Use this presenter when a configured Playwright MCP server is the available browser surface, including Claude Code and other MCP clients.

## Readiness

1. Require `browser_navigate`, `browser_snapshot`, and `browser_take_screenshot`. Confirm all three tools are callable before starting the helper. If any tool is missing or the MCP server is not connected, report the limitation and stop.
2. Use the existing MCP configuration. Do not install a server or change persistent configuration. Do not enable unrestricted `file://` access for this review.

## Present and verify

1. After the common workflow prints the loopback URL, call `browser_navigate` with that exact URL right away; the helper exits non-zero after 30 seconds without a request.
2. Use `browser_snapshot` to confirm the page title and key sections.
3. Use `browser_take_screenshot` to inspect the settled layout. Omit `filename` unless the client requires one; if a file is required, keep it inside the private artifact directory.

If a required tool, navigation, snapshot, or screenshot fails, retry that one step at most once, then stop and report it with the local artifact path. Do not switch to another presenter or OS viewer (`open -a "Google Chrome"` included).
