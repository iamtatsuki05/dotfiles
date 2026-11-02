# Pi extensions

Run from the dotfiles checkout after installing the mise-managed Pi CLI and
running `agent-sync` to create Pi's `mcp.json`:

```sh
bash scripts/setup_pi_extensions.sh
```

`packages.txt` pins user-level packages shared by Pi CLI and pi-gui. The script
uses `pi install`, preserving personal packages and model settings. It can be
run again to reconcile the pins. It stops on the first failed install; earlier
successful installs remain installed. Remove a package with `pi remove <source>`
and remove its declaration here if it is no longer managed.

| Package | Pin | Purpose |
| --- | --- | --- |
| [pi-web-access](https://github.com/nicobailon/pi-web-access) | 0.46.0 | Search and page extraction |
| [@tintinweb/pi-subagents](https://github.com/tintinweb/pi-subagents) | 0.19.0 | Delegation and independent reviews |
| [pi-lsp-adapter](https://github.com/nikmmd/pi-lsp-adapter) | 0.1.3 | Read-only definitions, references and diagnostics |
| [pi-memory](https://github.com/jayzeng/pi-memory) | 0.4.2 | Persistent Markdown memory and scratchpad |
| [@juicesharp/rpiv-ask-user-question](https://github.com/juicesharp/rpiv-mono/tree/main/packages/rpiv-ask-user-question) | 2.15.0 | Structured questions in terminal and RPC hosts |
| [billion-context](https://github.com/ranxianglei/billion-context) | 0.1.212 | Local model proxy and conversation compression |
| [pi-mcp-adapter](https://github.com/nicobailon/pi-mcp-adapter) | 7.0.0 | Lazy MCP connections and tool discovery |
| [@juanibiapina/pi-extension-settings](https://github.com/juanibiapina/pi-extension-settings) | 0.10.0 | Settings UI required by Powerbar |
| [@juanibiapina/pi-powerbar](https://github.com/juanibiapina/pi-powerbar) | 0.16.0 | Terminal status bar |

The subagent pin supports Pi >=0.84.0, including the installed pi-gui 1.0.1
runtime (Pi 0.87.1) and CLI (Pi 1.1.0). Newer subagent releases require Pi
>=1.1.0. Check both hosts before changing pins; a peer dependency range alone
is not a runtime compatibility test. The LSP adapter requires language servers
for the languages being inspected; installing the extension does not install
all language servers.

Pi Memory stores global, cross-project memory under `~/.pi/agent/memory/`
(`PI_MEMORY_DIR` overrides it) and injects stored content into later turns.
Keep secrets out of this directory: injected content is sent to the selected
model with the conversation. By default, quitting a session can call that model
to generate and persist an exit summary, consuming model quota. Set
`PI_MEMORY_EXIT_SUMMARY=0` in the Pi host environment to disable that behavior.
This setup does not change those upstream defaults or import existing memories.
The optional `qmd` search engine is not installed by this script; core memory
tools work without it. If qmd is already available, Pi Memory automatically
creates its collection and can download an embedding model on first use.

The question extension needs Node.js >=22 and interactive UI support. RPC hosts
use their native dialogs; non-interactive sessions hide the tool. Its optional
translation package is not installed, so dialog labels use English. The
extension itself makes no model calls. Restart either host after installation.

Billion Context starts a local proxy when the Pi extension loads and routes
model traffic through it. Conversation compression state is persisted under
`$XDG_DATA_HOME/billion-context/sessions` (default
`~/.local/share/billion-context/sessions`). Treat this data as conversation
history. This setup links `billion-context.json` at `$BILI_CONFIG_FILE`, or
`$XDG_CONFIG_HOME/billion-context/billion-context.json` (default
`~/.config/billion-context/billion-context.json`). A conflicting personal file
causes setup to stop before installing packages. The proxy binds to loopback;
automatic updates and advisory-triggered forced updates are disabled so the
repository pins remain authoritative. Update these pins manually when fixes
are needed. Other upstream settings retain their defaults. Do not place
credentials in the linked repository file.

MCP Adapter uses the existing Pi `mcp.json`. Setup links `mcp-adapter.json` to
that same user file (under `$PI_CODING_AGENT_DIR`, or `~/.pi/agent`). This keeps
the GUI's older Pi 0.87.1 on the same definitions as CLI Pi 1.1.0, including
personal servers already in that file. Setup refuses a missing `mcp.json` or
a conflicting personal `mcp-adapter.json`. This compatibility link can be
removed once every host reads Pi's MCP configuration natively (Pi >=0.99).
Only the shared transport fields (`type`, `command`, `args`, `env`, `cwd`,
`url`, `headers`, `description`) are supported by this link. Setup rejects
Pi-only controls such as `enabled`, `exposure`, `toolExposure`, `timeout` and
`oauth`, rather than silently changing their meaning on the older GUI. Do not
add those controls to the shared file while the link is active; upgrade all
hosts and remove the link, or explicitly migrate to an adapter-owned config.
On Pi versions with built-in MCP,
its first session startup adds `-builtin:mcp` to user settings to avoid running
both implementations. It does not import other agents' configs or sign-ins
unless requested. If removing the adapter, re-enable built-in MCP in
`pi config` before relying on Pi's MCP tools again. The existing remote Figma
definition remains unchanged; remote Figma may reject unapproved clients, and
this setup does not replace it with the desktop server or authorize OAuth.

Powerbar needs `pi-extension-settings` loaded first, so preserve their order in
`packages.txt` and Pi's package list. Its bundled usage extension can query a
provider's subscription-usage endpoint using existing credentials. Powerbar
uses a custom terminal widget; loading it in an SDK/GUI host does not guarantee
that host can display the bar. No alternate GUI widget is installed.

`web-search.json` is linked to the extension's effective config directory:
`$PI_CODING_AGENT_DIR`, otherwise `$XDG_CONFIG_HOME/pi` when set, otherwise
`~/.pi/agent`. An existing legacy `~/.pi/web-search.json` must be reconciled
before creating a new config. A conflicting
personal file causes an error before installation; reconcile it explicitly.
Search is restricted to Exa, with no provider fallback or current-model routing.
Search queries go to Exa when the tool is used. Page fetches use direct HTTP;
hosted extraction and answer mode are disabled. Summary workflows use the
current model, so invoking them can consume model quota. No credentials belong
in this repository. Remove the symlink to stop managing this configuration.

Packages execute with the user's permissions. The published package manifests
and entry points were inspected before installation; this is not an exhaustive
security audit. Reload an existing Pi session or restart pi-gui after setup.
