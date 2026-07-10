#!/usr/bin/env zsh

set -euo pipefail

readonly TEST_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly REPO_ROOT="$(cd "$TEST_DIR/.." && pwd)"
readonly AGENTS_FILE="$REPO_ROOT/dotfiles/.agent/AGENTS.md"
readonly SKILL_DIR="$REPO_ROOT/dotfiles/.agent/skills/html-preview-review"
readonly SKILL_FILE="$SKILL_DIR/SKILL.md"
readonly RENDERER="$SKILL_DIR/scripts/render_review.py"
readonly PREVIEW_SERVER="$SKILL_DIR/scripts/serve_preview.py"
readonly OPENAI_METADATA="$SKILL_DIR/agents/openai.yaml"
readonly SCHEMA_REFERENCE="$SKILL_DIR/references/schema.md"
readonly CODEX_PRESENTER_REFERENCE="$SKILL_DIR/references/presenters/codex.md"
readonly PLAYWRIGHT_PRESENTER_REFERENCE="$SKILL_DIR/references/presenters/playwright-mcp.md"
readonly CLAUDE_MCP_CONFIG="$REPO_ROOT/dotfiles/.agent/apps/claude/.mcp.json"
readonly IMPLEMENTATION_NOTES_EVAL="$REPO_ROOT/dotfiles/.agent/evals/html-preview-review/tasks/capture-implementation-notes.yaml"
readonly IMPLEMENTATION_NOTES_ARTIFACT_EVAL="$REPO_ROOT/dotfiles/.agent/evals/html-preview-review/tasks/artifact/capture-implementation-notes.yaml"
readonly MODEL_EVAL="$REPO_ROOT/dotfiles/.agent/evals/html-preview-review/model.yaml"

source "$TEST_DIR/lib/assertions.sh"

write_valid_input() {
  local output_path="$1"
  local language="$2"

  cat > "$output_path" <<EOF
{
  "schema_version": 1,
  "language": "$language",
  "title": "Setup review <script>alert(\"title\")</script>",
  "objective": "Improve setup documentation",
  "scope": ["docs/example.md"],
  "changed_files": [
    {
      "path": "docs/example.md",
      "summary": "Added a literal script example",
      "evidence": "+<script>alert(\"review\")</script>"
    }
  ],
  "verification": [
    {
      "command": "python3 -m unittest tests.test_example",
      "status": "passed",
      "summary": "1 test passed"
    }
  ],
  "findings": [
    {
      "severity": "minor",
      "title": "Layout not checked",
      "body": "Open the generated page in a browser.",
      "location": "docs/example.md:1"
    }
  ],
  "implementation_notes": [
    {
      "kind": "decision",
      "title": "Keep the renderer static",
      "body": "Static HTML preserves the review boundary.",
      "location": "scripts/render_review.py"
    },
    {
      "kind": "deviation",
      "title": "Retain one compatibility path",
      "body": "Required by migration plan <script>alert(\"note\")</script>."
    },
    {
      "kind": "tradeoff",
      "title": "Prefer CSS over a chart library",
      "body": "Avoids an external runtime dependency."
    },
    {
      "kind": "open-question",
      "title": "Confirm removal date",
      "body": "The user must confirm when compatibility ends."
    }
  ],
  "visualizations": [
    {
      "type": "bar",
      "title": "Change composition",
      "summary": "Files changed by category",
      "items": [
        {"label": "Documentation <script>alert(\"chart\")</script>", "value": 3},
        {"label": "Tests", "value": 2}
      ]
    },
    {
      "type": "flow",
      "title": "Review flow",
      "summary": "Implementation through presentation",
      "nodes": [
        {"id": "implement", "label": "Implementation"},
        {"id": "review", "label": "Read-only review"},
        {"id": "preview", "label": "HTML preview"}
      ],
      "edges": [
        {"from": "implement", "to": "review", "label": "verify"},
        {"from": "review", "to": "preview", "label": "present"}
      ]
    }
  ],
  "remaining_risks": ["Browser layout has not been checked"],
  "provenance": {
    "repository": "/work/example",
    "revision": "abc123-dirty",
    "generated_at": "2026-07-10T19:00:00+09:00"
  }
}
EOF
}

run_invalid_case() {
  local input_file="$1"
  local output_file="$2"
  local expected_error="$3"
  local output
  local exit_status

  set +e
  output="$(python3 "$RENDERER" --input "$input_file" --output "$output_file" 2>&1)"
  exit_status=$?
  set -e

  [[ "$exit_status" -ne 0 ]] || fail "expected invalid review input to fail"
  assert_contains_text "$output" "$expected_error"
  assert_not_exists "$output_file"
}

test_skill_contract_exists() {
  assert_file "$SKILL_FILE"
  assert_file "$AGENTS_FILE"
  assert_executable "$RENDERER"
  assert_executable "$PREVIEW_SERVER"
  assert_file "$OPENAI_METADATA"
  assert_file "$SCHEMA_REFERENCE"
  assert_file "$CODEX_PRESENTER_REFERENCE"
  assert_file "$PLAYWRIGHT_PRESENTER_REFERENCE"
  assert_file "$CLAUDE_MCP_CONFIG"
  assert_file "$IMPLEMENTATION_NOTES_EVAL"
  assert_file "$IMPLEMENTATION_NOTES_ARTIFACT_EVAL"
  assert_file "$MODEL_EVAL"
  assert_contains "$SKILL_FILE" "main agent"
  assert_contains "$SKILL_FILE" "read-only reviewer"
  assert_contains "$SKILL_FILE" "presenter"
  assert_contains "$SCHEMA_REFERENCE" "Do not upload"
  assert_contains "$SCHEMA_REFERENCE" "synthetic or placeholder evidence"
  assert_contains "$SKILL_FILE" "references/schema.md"
  assert_contains "$SCHEMA_REFERENCE" '`bar`'
  assert_contains "$SCHEMA_REFERENCE" '`flow`'
  assert_contains "$SCHEMA_REFERENCE" "decisions, deviations, tradeoffs, and open questions"
  assert_contains "$SKILL_FILE" '.agent/work/sessions/<session>/artifacts/html-preview-review/'
  assert_contains "$SKILL_FILE" 'mode `0700`'
  assert_contains "$SKILL_FILE" 'review.json` there with mode `0600`'
  assert_contains "$SKILL_FILE" 'scripts/serve_preview.py --input <artifact-dir>/index.html'
  assert_contains "$SKILL_FILE" 'By default, immediately present every generated review with one supported presenter'
  assert_contains "$SKILL_FILE" 'Select exactly one supported presenter before starting the helper'
  assert_contains "$SKILL_FILE" 'Read exactly one presenter reference completely'
  assert_contains "$SKILL_FILE" 'references/presenters/codex.md'
  assert_contains "$SKILL_FILE" 'references/presenters/playwright-mcp.md'
  assert_contains "$SKILL_FILE" 'Do not start the helper until the selected presenter readiness checks pass'
  assert_contains "$SKILL_FILE" 'Do not switch presenters after a readiness, navigation, or verification failure'
  assert_contains "$SKILL_FILE" 'If no supported presenter is available, report the limitation and local artifact path, then stop before starting the helper'
  assert_contains "$SKILL_FILE" 'navigate the selected presenter to the exact URL'
  assert_contains "$SKILL_FILE" 'wait for the helper to exit and require status `0`'
  assert_contains "$SKILL_FILE" 'If navigation fails or the helper does not exit cleanly, terminate it if needed and report the limitation'
  assert_contains "$CODEX_PRESENTER_REFERENCE" '**REQUIRED SUB-SKILL:** Use `browser:control-in-app-browser`'
  assert_contains "$CODEX_PRESENTER_REFERENCE" 'set Browser visibility to `true`'
  assert_contains "$CODEX_PRESENTER_REFERENCE" 'read Browser visibility back as `true`'
  assert_contains "$CODEX_PRESENTER_REFERENCE" 'reuse or create a Browser tab'
  assert_contains "$CODEX_PRESENTER_REFERENCE" 'Do not wait for the user to click a link or open the Browser pane'
  assert_contains "$PLAYWRIGHT_PRESENTER_REFERENCE" '`browser_navigate`'
  assert_contains "$PLAYWRIGHT_PRESENTER_REFERENCE" '`browser_snapshot`'
  assert_contains "$PLAYWRIGHT_PRESENTER_REFERENCE" '`browser_take_screenshot`'
  assert_contains "$PLAYWRIGHT_PRESENTER_REFERENCE" 'Confirm all three tools are callable before starting the helper'
  assert_contains "$PLAYWRIGHT_PRESENTER_REFERENCE" 'Do not enable unrestricted `file://` access'
  assert_contains "$CLAUDE_MCP_CONFIG" '"playwright"'
  assert_not_contains "$SKILL_FILE" "private OS temporary directory"
  assert_not_contains "$SKILL_FILE" "OS temporary storage"
  assert_contains "$AGENTS_FILE" '.agent/work/sessions/<session>/artifacts/html-preview-review/'
  assert_contains "$AGENTS_FILE" '`html-preview-review` の private artifact はこの原則の例外'
  assert_contains "$SCHEMA_REFERENCE" '"schema_version": 1'
  assert_contains "$SCHEMA_REFERENCE" '"implementation_notes"'
  assert_contains "$SCHEMA_REFERENCE" '`open-question`'
  assert_contains "$SCHEMA_REFERENCE" '"visualizations"'
  assert_contains "$SCHEMA_REFERENCE" 'at least one positive value'
  assert_contains "$SCHEMA_REFERENCE" 'valid node ID'
  assert_contains "$SCHEMA_REFERENCE" 'every node must appear in at least one edge'
  assert_contains "$IMPLEMENTATION_NOTES_EVAL" "uses_dedicated_note_field"
  assert_contains "$IMPLEMENTATION_NOTES_EVAL" "keeps_findings_empty"
  assert_contains "$IMPLEMENTATION_NOTES_EVAL" "keeps_remaining_risks_empty"
  assert_contains "$IMPLEMENTATION_NOTES_EVAL" "reports_rendered_artifact"
  assert_contains "$IMPLEMENTATION_NOTES_ARTIFACT_EVAL" "type: file"
  assert_contains "$IMPLEMENTATION_NOTES_ARTIFACT_EVAL" 'must_exist: ["review.json", "index.html"]'
  assert_contains "$IMPLEMENTATION_NOTES_ARTIFACT_EVAL" "content_patterns:"
  assert_contains "$IMPLEMENTATION_NOTES_ARTIFACT_EVAL" 'note-open-question'
  assert_contains "$IMPLEMENTATION_NOTES_ARTIFACT_EVAL" 'must_not_match:'
  assert_contains "$MODEL_EVAL" 'tasks/artifact/*.yaml'

  python3 - "$SKILL_FILE" <<'PY'
import pathlib
import sys

content = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
ordered_contract = [
    "Select exactly one supported presenter before starting the helper",
    "Read exactly one presenter reference completely",
    "Do not start the helper until the selected presenter readiness checks pass",
    "Start the bundled one-shot loopback helper",
    "navigate the selected presenter to the exact URL",
    "Confirm the title and key sections are visible",
    "wait for the helper to exit and require status `0`",
]
positions = [content.index(item) for item in ordered_contract]
if positions != sorted(positions):
    raise SystemExit("presenter contract is out of order")

routing_contract = [
    "Codex controls plus its sub-skill",
    "Otherwise, configured Playwright MCP tools",
    "If no supported presenter is available",
]
routing_positions = [content.index(item) for item in routing_contract]
if routing_positions != sorted(routing_positions):
    raise SystemExit("presenter routing priority is out of order")
PY
}

test_preview_server_serves_only_the_html_once_on_loopback() {
  make_temp_dir "html-preview-review-server"
  local work_dir="$REPLY"
  local input_file="$work_dir/index.html"

  print -r -- '<!DOCTYPE html><title>Private review</title><p>visible in Codex</p>' > "$input_file"

  python3 - "$PREVIEW_SERVER" "$input_file" <<'PY'
import re
import http.client
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

server_path, input_path = sys.argv[1:]
process = subprocess.Popen(
    [sys.executable, server_path, "--input", input_path],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)

try:
    assert process.stdout is not None
    url = process.stdout.readline().strip()
    if re.fullmatch(r"http://127\.0\.0\.1:\d+/index\.html", url) is None:
        raise SystemExit(f"unexpected preview URL: {url!r}")

    private_json_url = urllib.parse.urljoin(url, "review.json")
    try:
        urllib.request.urlopen(private_json_url, timeout=2)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
    else:
        raise SystemExit("preview server exposed a non-HTML artifact")

    parsed_url = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parsed_url.hostname, parsed_url.port, timeout=2)
    connection.request("GET", parsed_url.path, headers={"Connection": "keep-alive"})
    response = connection.getresponse()
    try:
        body = response.read().decode("utf-8")
        if body != '<!DOCTYPE html><title>Private review</title><p>visible in Codex</p>\n':
            raise SystemExit("preview response did not match the input HTML")
        if response.headers.get("Cache-Control") != "no-store":
            raise SystemExit("preview response must disable caching")
        if response.headers.get("X-Content-Type-Options") != "nosniff":
            raise SystemExit("preview response must disable content sniffing")
        if response.headers.get("Connection") != "close":
            raise SystemExit("preview response must close the browser connection")

        if process.wait(timeout=5) != 0:
            raise SystemExit("preview server did not exit cleanly after the HTML request")
    finally:
        connection.close()

    try:
        urllib.request.urlopen(url, timeout=1)
    except urllib.error.URLError:
        pass
    else:
        raise SystemExit("preview server remained reachable after one HTML request")
finally:
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)
    stderr = process.stderr.read() if process.stderr is not None else ""
    if stderr:
        raise SystemExit(f"unexpected preview server stderr: {stderr}")
PY

  rm -rf "$work_dir"
}

test_preview_server_deadline_applies_to_an_accepted_stalled_connection() {
  make_temp_dir "html-preview-review-server-stall"
  local work_dir="$REPLY"
  local input_file="$work_dir/index.html"

  print -r -- '<!DOCTYPE html><title>Private review</title>' > "$input_file"

  python3 - "$PREVIEW_SERVER" "$input_file" <<'PY'
import subprocess
import socket
import sys
import urllib.parse

server_path, input_path = sys.argv[1:]
wrapper = """
import importlib.util
import sys

server_path, input_path = sys.argv[1:]
spec = importlib.util.spec_from_file_location("preview_server_under_test", server_path)
if spec is None or spec.loader is None:
    raise SystemExit("failed to load preview server")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.REQUEST_TIMEOUT_SECONDS = 0.2
sys.argv = [server_path, "--input", input_path]
raise SystemExit(module.main())
"""
process = subprocess.Popen(
    [sys.executable, "-B", "-c", wrapper, server_path, input_path],
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
connection = None

try:
    assert process.stdout is not None
    url = process.stdout.readline().strip()
    parsed_url = urllib.parse.urlsplit(url)
    if parsed_url.hostname != "127.0.0.1" or parsed_url.port is None:
        raise SystemExit(f"unexpected preview URL: {url!r}")

    connection = socket.create_connection((parsed_url.hostname, parsed_url.port), timeout=1)
    connection.sendall(b"GET /index.html HTTP/1.1\r\nHost: 127.0.0.1\r\n")

    try:
        exit_status = process.wait(timeout=1)
    except subprocess.TimeoutExpired as error:
        raise SystemExit("preview server exceeded its deadline after accepting a stalled connection") from error
    if exit_status == 0:
        raise SystemExit("stalled preview request must not count as a successful HTML response")
    assert process.stderr is not None
    if "preview request timed out" not in process.stderr.read():
        raise SystemExit("preview server did not report its deadline expiry")
finally:
    if connection is not None:
        connection.close()
    if process.poll() is None:
        process.kill()
        process.wait(timeout=5)
PY

  rm -rf "$work_dir"
}

test_renderer_escapes_untrusted_content_and_is_self_contained() {
  make_temp_dir "html-preview-review"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"
  local mode

  write_valid_input "$input_file" "ja"
  python3 "$RENDERER" --input "$input_file" --output "$output_file" >/dev/null

  assert_file "$output_file"
  assert_contains "$output_file" '<!DOCTYPE html>'
  assert_contains "$output_file" '<html lang="ja">'
  assert_contains "$output_file" 'Content-Security-Policy'
  assert_contains "$output_file" "connect-src 'none'"
  assert_contains "$output_file" '&lt;script&gt;alert(&quot;review&quot;)&lt;/script&gt;'
  assert_contains "$output_file" '<pre tabindex="0"><code>'
  assert_contains "$output_file" '<details>'
  assert_contains "$output_file" '<section class="panel wide visualizations">'
  assert_contains "$output_file" '<section class="panel wide implementation-notes">'
  assert_contains "$output_file" '>実装ノート</h2>'
  assert_contains "$output_file" '<article class="implementation-note note-open-question">'
  assert_contains "$output_file" '>1</strong><span>未解決事項</span>'
  assert_contains "$output_file" 'Required by migration plan &lt;script&gt;alert(&quot;note&quot;)&lt;/script&gt;.'
  assert_contains "$output_file" '<figure class="visualization visualization-bar">'
  assert_contains "$output_file" '<figure class="visualization visualization-flow">'
  assert_contains "$output_file" 'role="img"'
  assert_contains "$output_file" '<table class="chart-data">'
  assert_contains "$output_file" 'Documentation &lt;script&gt;alert(&quot;chart&quot;)&lt;/script&gt;'
  assert_contains "$output_file" 'Read-only review'
  assert_not_contains "$output_file" '<script'
  assert_not_contains "$output_file" 'http://'
  assert_not_contains "$output_file" 'https://'

  mode="$(python3 - "$output_file" <<'PY'
import pathlib
import stat
import sys

print(oct(stat.S_IMODE(pathlib.Path(sys.argv[1]).stat().st_mode))[2:])
PY
)"
  [[ "$mode" == "600" ]] || fail "expected generated HTML mode 600, got $mode"

  rm -rf "$work_dir"
}

test_renderer_surfaces_open_questions_before_other_implementation_notes() {
  make_temp_dir "html-preview-review-note-order"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "ja"
  python3 "$RENDERER" --input "$input_file" --output "$output_file" >/dev/null

  python3 - "$output_file" <<'PY'
import pathlib
import sys

content = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
open_question = content.index("Confirm removal date")
decision = content.index("Keep the renderer static")
if open_question >= decision:
    raise SystemExit("open question must be rendered before other implementation notes")
PY
  rm -rf "$work_dir"
}

test_renderer_omits_empty_implementation_notes_section() {
  make_temp_dir "html-preview-review-no-notes"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "ja"
  python3 - "$input_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
data["implementation_notes"] = []
path.write_text(json.dumps(data), encoding="utf-8")
PY
  python3 "$RENDERER" --input "$input_file" --output "$output_file" >/dev/null

  assert_not_contains "$output_file" 'class="implementation-note'
  assert_contains "$output_file" '>0</strong><span>未解決事項</span>'
  rm -rf "$work_dir"
}

test_renderer_rejects_unknown_implementation_note_kind() {
  make_temp_dir "html-preview-review-note-kind"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "ja"
  python3 - "$input_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
data["implementation_notes"][0]["kind"] = "assumption"
path.write_text(json.dumps(data), encoding="utf-8")
PY
  run_invalid_case "$input_file" "$output_file" "implementation_notes[0].kind must be one of: decision, deviation, tradeoff, open-question"
  rm -rf "$work_dir"
}

test_renderer_omits_visualization_section_for_explicit_empty_list() {
  make_temp_dir "html-preview-review-no-visualization"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "ja"
  python3 - "$input_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
data["visualizations"] = []
path.write_text(json.dumps(data), encoding="utf-8")
PY
  python3 "$RENDERER" --input "$input_file" --output "$output_file" >/dev/null

  assert_not_contains "$output_file" 'class="visualization'
  rm -rf "$work_dir"
}

test_renderer_rejects_negative_bar_values() {
  make_temp_dir "html-preview-review-negative-bar"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "ja"
  python3 - "$input_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
data["visualizations"][0]["items"][0]["value"] = -1
path.write_text(json.dumps(data), encoding="utf-8")
PY
  run_invalid_case "$input_file" "$output_file" "visualizations[0].items[0].value must be a finite non-negative number"
  rm -rf "$work_dir"
}

test_renderer_rejects_all_zero_bar_values() {
  make_temp_dir "html-preview-review-zero-bar"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "ja"
  python3 - "$input_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
for item in data["visualizations"][0]["items"]:
    item["value"] = 0
path.write_text(json.dumps(data), encoding="utf-8")
PY
  run_invalid_case "$input_file" "$output_file" "visualizations[0] must contain at least one positive bar value"
  rm -rf "$work_dir"
}

test_renderer_rejects_flow_edges_with_unknown_nodes() {
  make_temp_dir "html-preview-review-flow-reference"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "ja"
  python3 - "$input_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
data["visualizations"][1]["edges"][0]["to"] = "missing"
path.write_text(json.dumps(data), encoding="utf-8")
PY
  run_invalid_case "$input_file" "$output_file" "visualizations[1].edges[0].to references unknown node: missing"
  rm -rf "$work_dir"
}

test_renderer_rejects_duplicate_flow_node_ids() {
  make_temp_dir "html-preview-review-flow-duplicate"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "ja"
  python3 - "$input_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
data["visualizations"][1]["nodes"][1]["id"] = "implement"
path.write_text(json.dumps(data), encoding="utf-8")
PY
  run_invalid_case "$input_file" "$output_file" "visualizations[1] contains duplicate node id: implement"
  rm -rf "$work_dir"
}

test_renderer_rejects_unreferenced_flow_nodes() {
  make_temp_dir "html-preview-review-flow-unreferenced"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "ja"
  python3 - "$input_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
data["visualizations"][1]["edges"] = data["visualizations"][1]["edges"][:1]
path.write_text(json.dumps(data), encoding="utf-8")
PY
  run_invalid_case "$input_file" "$output_file" "visualizations[1] contains unreferenced node id: preview"
  rm -rf "$work_dir"
}

test_renderer_uses_explicit_language_labels() {
  make_temp_dir "html-preview-review-language"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "en"
  python3 "$RENDERER" --input "$input_file" --output "$output_file" >/dev/null

  assert_contains "$output_file" '<html lang="en">'
  assert_contains "$output_file" '>Objective</h2>'
  assert_contains "$output_file" '>Verification</h2>'

  rm -rf "$work_dir"
}

test_renderer_rejects_missing_required_fields() {
  make_temp_dir "html-preview-review-missing"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "ja"
  python3 - "$input_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
del data["objective"]
path.write_text(json.dumps(data), encoding="utf-8")
PY

  run_invalid_case "$input_file" "$output_file" "missing top-level keys: objective"
  rm -rf "$work_dir"
}

test_renderer_rejects_unknown_status_and_language() {
  make_temp_dir "html-preview-review-enum"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "ja"
  python3 - "$input_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
data["verification"][0]["status"] = "success"
path.write_text(json.dumps(data), encoding="utf-8")
PY
  run_invalid_case "$input_file" "$output_file" "verification[0].status must be one of: passed, failed, not-run"

  write_valid_input "$input_file" "fr"
  run_invalid_case "$input_file" "$output_file" "language must be one of: ja, en"
  rm -rf "$work_dir"
}

test_renderer_requires_an_existing_output_directory() {
  make_temp_dir "html-preview-review-output"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/missing/index.html"

  write_valid_input "$input_file" "ja"
  run_invalid_case "$input_file" "$output_file" "output directory does not exist"
  rm -rf "$work_dir"
}

test_renderer_rejects_overwriting_its_input() {
  make_temp_dir "html-preview-review-same-path"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output
  local exit_status

  write_valid_input "$input_file" "ja"
  set +e
  output="$(python3 "$RENDERER" --input "$input_file" --output "$input_file" 2>&1)"
  exit_status=$?
  set -e

  [[ "$exit_status" -ne 0 ]] || fail "expected identical input and output paths to fail"
  assert_contains_text "$output" "input and output paths must differ"
  assert_contains "$input_file" '"schema_version": 1'
  rm -rf "$work_dir"
}

test_renderer_rejects_invalid_unicode_surrogates() {
  make_temp_dir "html-preview-review-unicode"
  local work_dir="$REPLY"
  local input_file="$work_dir/review.json"
  local output_file="$work_dir/index.html"

  write_valid_input "$input_file" "ja"
  python3 - "$input_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
data = json.loads(path.read_text())
data["title"] = "invalid-" + chr(0xD800)
path.write_text(json.dumps(data), encoding="utf-8")
PY

  run_invalid_case "$input_file" "$output_file" "title contains an invalid Unicode surrogate"
  rm -rf "$work_dir"
}

main() {
  test_skill_contract_exists
  test_preview_server_serves_only_the_html_once_on_loopback
  test_preview_server_deadline_applies_to_an_accepted_stalled_connection
  test_renderer_escapes_untrusted_content_and_is_self_contained
  test_renderer_surfaces_open_questions_before_other_implementation_notes
  test_renderer_omits_empty_implementation_notes_section
  test_renderer_rejects_unknown_implementation_note_kind
  test_renderer_omits_visualization_section_for_explicit_empty_list
  test_renderer_rejects_negative_bar_values
  test_renderer_rejects_all_zero_bar_values
  test_renderer_rejects_flow_edges_with_unknown_nodes
  test_renderer_rejects_duplicate_flow_node_ids
  test_renderer_rejects_unreferenced_flow_nodes
  test_renderer_uses_explicit_language_labels
  test_renderer_rejects_missing_required_fields
  test_renderer_rejects_unknown_status_and_language
  test_renderer_requires_an_existing_output_directory
  test_renderer_rejects_overwriting_its_input
  test_renderer_rejects_invalid_unicode_surrogates
  echo "agent HTML preview review tests passed"
}

main "$@"
