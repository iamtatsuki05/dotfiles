#!/usr/bin/env python3
"""Render a strict agent-review JSON document as self-contained static HTML."""

from __future__ import annotations

import argparse
import html
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Any


SCHEMA_VERSION = 1
VALID_LANGUAGES = ("ja", "en")
VALID_STATUSES = ("passed", "failed", "not-run")
VALID_SEVERITIES = ("critical", "major", "minor", "info")
VALID_NOTE_KINDS = ("decision", "deviation", "tradeoff", "open-question")
VALID_VISUALIZATION_TYPES = ("bar", "flow")

TOP_LEVEL_KEYS = {
    "schema_version",
    "language",
    "title",
    "objective",
    "scope",
    "changed_files",
    "verification",
    "findings",
    "implementation_notes",
    "visualizations",
    "remaining_risks",
    "provenance",
}

LABELS = {
    "ja": {
        "eyebrow": "Agent実行結果レビュー",
        "objective": "目的",
        "scope": "対象範囲",
        "changes": "変更内容",
        "evidence": "根拠となる差分・抜粋",
        "verification": "検証結果",
        "findings": "レビュー指摘",
        "no_findings": "指摘は記録されていません。",
        "implementation_notes": "実装ノート",
        "open_questions": "未解決事項",
        "note_decision": "設計判断",
        "note_deviation": "仕様からの逸脱",
        "note_tradeoff": "トレードオフ",
        "note_open_question": "未解決事項",
        "visualizations": "可視化",
        "category": "項目",
        "value": "値",
        "from": "接続元",
        "relation": "関係",
        "to": "接続先",
        "risks": "残るリスク",
        "no_risks": "残るリスクは明示的にありません。",
        "provenance": "出典",
        "repository": "Repository",
        "revision": "Revision",
        "generated_at": "生成日時",
        "files": "変更ファイル",
        "checks": "検証項目",
    },
    "en": {
        "eyebrow": "Agent Result Review",
        "objective": "Objective",
        "scope": "Scope",
        "changes": "Changes",
        "evidence": "Diff or evidence excerpt",
        "verification": "Verification",
        "findings": "Review findings",
        "no_findings": "No findings were recorded.",
        "implementation_notes": "Implementation notes",
        "open_questions": "Open questions",
        "note_decision": "Decision",
        "note_deviation": "Deviation",
        "note_tradeoff": "Tradeoff",
        "note_open_question": "Open question",
        "visualizations": "Visualizations",
        "category": "Category",
        "value": "Value",
        "from": "From",
        "relation": "Relationship",
        "to": "To",
        "risks": "Remaining risks",
        "no_risks": "No remaining risks were explicitly recorded.",
        "provenance": "Provenance",
        "repository": "Repository",
        "revision": "Revision",
        "generated_at": "Generated at",
        "files": "Changed files",
        "checks": "Verification checks",
    },
}

CSS = """
:root {
  color-scheme: light;
  --canvas: #f4f7fb;
  --surface: #ffffff;
  --surface-soft: #f8fafc;
  --ink: #17202a;
  --muted: #5f6b7a;
  --line: #d9e1ea;
  --accent: #155eef;
  --accent-soft: #eef4ff;
  --passed: #067647;
  --passed-soft: #ecfdf3;
  --failed: #b42318;
  --failed-soft: #fef3f2;
  --warning: #b54708;
  --warning-soft: #fffaeb;
  --code: #101828;
  --code-ink: #f2f4f7;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--canvas);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  line-height: 1.6;
}

main {
  width: min(1040px, calc(100% - 32px));
  margin: 40px auto;
}

.hero,
.panel {
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--surface);
  box-shadow: 0 8px 24px rgb(16 24 40 / 6%);
}

.hero { padding: 32px; }

.eyebrow {
  margin: 0 0 8px;
  color: var(--accent);
  font-size: 0.78rem;
  font-weight: 750;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

h1 {
  max-width: 24ch;
  margin: 0;
  font-size: clamp(1.8rem, 5vw, 3rem);
  line-height: 1.15;
  letter-spacing: -0.03em;
}

.summary-grid,
.content-grid {
  display: grid;
  gap: 16px;
}

.summary-grid {
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin-top: 24px;
}

.summary-card {
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface-soft);
}

.summary-card strong {
  display: block;
  font-size: 1.45rem;
}

.summary-card span { color: var(--muted); }

.content-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin-top: 16px;
}

.panel { padding: 24px; }
.panel.wide { grid-column: 1 / -1; }

h2 {
  margin: 0 0 14px;
  font-size: 1.08rem;
}

h3 {
  margin: 0;
  font-size: 1rem;
}

p { margin: 0; }
p + p { margin-top: 10px; }

ul {
  margin: 0;
  padding-left: 1.25rem;
}

li + li { margin-top: 8px; }

.change,
.verification-item,
.finding,
.implementation-note {
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
}

.change + .change,
.verification-item + .verification-item,
.finding + .finding { margin-top: 12px; }

.change > p,
.verification-item > p,
.finding > p,
.implementation-note > p { margin-top: 8px; }

details { margin-top: 12px; }

summary {
  color: var(--accent);
  cursor: pointer;
  font-weight: 650;
}

pre {
  overflow-x: auto;
  margin: 12px 0 0;
  padding: 16px;
  border-radius: 10px;
  background: var(--code);
  color: var(--code-ink);
  white-space: pre;
}

code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.9em;
}

.badge {
  display: inline-block;
  padding: 3px 8px;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.04em;
}

.status-passed { border-color: #abefc6; }
.status-passed .badge { color: var(--passed); background: var(--passed-soft); }
.status-failed { border-color: #fecdca; }
.status-failed .badge { color: var(--failed); background: var(--failed-soft); }
.status-not-run .badge { color: var(--warning); background: var(--warning-soft); }

.severity-critical,
.severity-major { border-color: #fecdca; }
.severity-critical .badge,
.severity-major .badge { color: var(--failed); background: var(--failed-soft); }
.severity-minor .badge { color: var(--warning); background: var(--warning-soft); }
.severity-info .badge { color: var(--accent); background: var(--accent-soft); }

.note-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.note-decision .badge { color: var(--accent); background: var(--accent-soft); }
.note-deviation,
.note-open-question { border-color: #fedf89; }
.note-deviation .badge,
.note-open-question .badge { color: var(--warning); background: var(--warning-soft); }
.note-tradeoff .badge { color: var(--muted); background: var(--surface-soft); }

.visualizations {
  display: grid;
  gap: 16px;
}

.visualization {
  margin: 0;
  padding: 20px;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: var(--surface-soft);
}

.visualization figcaption { margin-bottom: 16px; }
.visualization figcaption strong { display: block; }
.visualization figcaption span { color: var(--muted); }

.bar-plot,
.flow-plot {
  padding: 16px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--surface);
}

.bar-row {
  display: grid;
  grid-template-columns: minmax(10rem, 1fr) minmax(12rem, 3fr) max-content;
  gap: 12px;
  align-items: center;
}

.bar-row + .bar-row { margin-top: 12px; }

.bar-track {
  height: 0.8rem;
  overflow: hidden;
  border-radius: 999px;
  background: var(--accent-soft);
}

.bar-fill {
  display: block;
  width: var(--bar-size);
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
}

.flow-edge {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(7rem, auto) minmax(0, 1fr);
  gap: 12px;
  align-items: center;
}

.flow-edge + .flow-edge { margin-top: 12px; }

.flow-node {
  padding: 10px 12px;
  border: 1px solid #b2ccff;
  border-radius: 10px;
  background: var(--accent-soft);
  font-weight: 700;
  text-align: center;
}

.flow-relation {
  color: var(--muted);
  font-size: 0.82rem;
  font-weight: 700;
  text-align: center;
}

.flow-relation::after {
  display: block;
  color: var(--accent);
  font-size: 1.4rem;
  line-height: 1;
  content: "→";
}

.chart-data {
  width: 100%;
  margin-top: 16px;
  border-collapse: collapse;
  background: var(--surface);
}

.chart-data th,
.chart-data td {
  padding: 8px 10px;
  border: 1px solid var(--line);
  text-align: start;
}

.chart-data th { background: var(--accent-soft); }

.visually-hidden {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}

.location,
.muted { color: var(--muted); }

dl {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 8px 16px;
  margin: 0;
}

dt { color: var(--muted); font-weight: 700; }
dd { min-width: 0; margin: 0; overflow-wrap: anywhere; }

@media (max-width: 720px) {
  main { margin: 20px auto; }
  .hero,
  .panel { border-radius: 12px; }
  .summary-grid,
  .content-grid,
  .note-grid { grid-template-columns: 1fr; }
  .panel.wide { grid-column: auto; }
  .bar-row,
  .flow-edge { grid-template-columns: 1fr; }
  .flow-relation::after { transform: rotate(90deg); }
  dl { grid-template-columns: 1fr; gap: 2px; }
  dd + dt { margin-top: 10px; }
}

@media print {
  body { background: #fff; }
  main { width: 100%; margin: 0; }
  .hero,
  .panel { box-shadow: none; break-inside: avoid; }
}
""".strip()


class ReviewInputError(ValueError):
    """Raised when the review input violates the explicit schema."""


def _require_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewInputError(f"{context} must be an object")
    return value


def _require_keys(
    value: dict[str, Any],
    *,
    required: set[str],
    allowed: set[str],
    context: str,
) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - allowed)
    if missing:
        raise ReviewInputError(f"missing {context} keys: {', '.join(missing)}")
    if unknown:
        raise ReviewInputError(f"unknown {context} keys: {', '.join(unknown)}")


def _require_string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewInputError(f"{context} must be a non-empty string")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ReviewInputError(f"{context} contains an invalid Unicode surrogate")
    return value


def _require_string_list(value: Any, context: str, *, non_empty: bool) -> list[str]:
    if not isinstance(value, list):
        raise ReviewInputError(f"{context} must be a list")
    if non_empty and not value:
        raise ReviewInputError(f"{context} must not be empty")
    return [_require_string(item, f"{context}[{index}]") for index, item in enumerate(value)]


def _require_enum(value: Any, context: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise ReviewInputError(f"{context} must be one of: {', '.join(choices)}")
    return value


def _require_number(value: Any, context: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and not math.isfinite(value))
        or value < 0
    ):
        raise ReviewInputError(f"{context} must be a finite non-negative number")
    return value


def validate_review(data: Any) -> dict[str, Any]:
    review = _require_object(data, "top-level value")
    _require_keys(
        review,
        required=TOP_LEVEL_KEYS,
        allowed=TOP_LEVEL_KEYS,
        context="top-level",
    )

    if type(review["schema_version"]) is not int or review["schema_version"] != SCHEMA_VERSION:
        raise ReviewInputError(f"schema_version must be {SCHEMA_VERSION}")

    _require_enum(review["language"], "language", VALID_LANGUAGES)
    _require_string(review["title"], "title")
    _require_string(review["objective"], "objective")
    _require_string_list(review["scope"], "scope", non_empty=True)

    changed_files = review["changed_files"]
    if not isinstance(changed_files, list) or not changed_files:
        raise ReviewInputError("changed_files must be a non-empty list")
    for index, raw_change in enumerate(changed_files):
        context = f"changed_files[{index}]"
        change = _require_object(raw_change, context)
        keys = {"path", "summary", "evidence"}
        _require_keys(change, required=keys, allowed=keys, context=context)
        for key in keys:
            _require_string(change[key], f"{context}.{key}")

    verification = review["verification"]
    if not isinstance(verification, list) or not verification:
        raise ReviewInputError("verification must be a non-empty list")
    for index, raw_check in enumerate(verification):
        context = f"verification[{index}]"
        check = _require_object(raw_check, context)
        keys = {"command", "status", "summary"}
        _require_keys(check, required=keys, allowed=keys, context=context)
        _require_string(check["command"], f"{context}.command")
        _require_enum(check["status"], f"{context}.status", VALID_STATUSES)
        _require_string(check["summary"], f"{context}.summary")

    findings = review["findings"]
    if not isinstance(findings, list):
        raise ReviewInputError("findings must be a list")
    for index, raw_finding in enumerate(findings):
        context = f"findings[{index}]"
        finding = _require_object(raw_finding, context)
        required = {"severity", "title", "body"}
        allowed = required | {"location"}
        _require_keys(finding, required=required, allowed=allowed, context=context)
        _require_enum(finding["severity"], f"{context}.severity", VALID_SEVERITIES)
        _require_string(finding["title"], f"{context}.title")
        _require_string(finding["body"], f"{context}.body")
        if "location" in finding:
            _require_string(finding["location"], f"{context}.location")

    implementation_notes = review["implementation_notes"]
    if not isinstance(implementation_notes, list):
        raise ReviewInputError("implementation_notes must be a list")
    for index, raw_note in enumerate(implementation_notes):
        context = f"implementation_notes[{index}]"
        note = _require_object(raw_note, context)
        required = {"kind", "title", "body"}
        allowed = required | {"location"}
        _require_keys(note, required=required, allowed=allowed, context=context)
        _require_enum(note["kind"], f"{context}.kind", VALID_NOTE_KINDS)
        _require_string(note["title"], f"{context}.title")
        _require_string(note["body"], f"{context}.body")
        if "location" in note:
            _require_string(note["location"], f"{context}.location")

    visualizations = review["visualizations"]
    if not isinstance(visualizations, list):
        raise ReviewInputError("visualizations must be a list")
    for index, raw_visualization in enumerate(visualizations):
        context = f"visualizations[{index}]"
        visualization = _require_object(raw_visualization, context)
        required = {"type", "title", "summary"}
        _require_keys(
            visualization,
            required=required,
            allowed=required | {"items", "nodes", "edges"},
            context=context,
        )
        visualization_type = _require_enum(
            visualization["type"],
            f"{context}.type",
            VALID_VISUALIZATION_TYPES,
        )
        _require_string(visualization["title"], f"{context}.title")
        _require_string(visualization["summary"], f"{context}.summary")

        if visualization_type == "bar":
            keys = required | {"items"}
            _require_keys(visualization, required=keys, allowed=keys, context=context)
            items = visualization["items"]
            if not isinstance(items, list) or not items:
                raise ReviewInputError(f"{context}.items must be a non-empty list")
            has_positive_value = False
            for item_index, raw_item in enumerate(items):
                item_context = f"{context}.items[{item_index}]"
                item = _require_object(raw_item, item_context)
                item_keys = {"label", "value"}
                _require_keys(item, required=item_keys, allowed=item_keys, context=item_context)
                _require_string(item["label"], f"{item_context}.label")
                value = _require_number(item["value"], f"{item_context}.value")
                has_positive_value = has_positive_value or value > 0
            if not has_positive_value:
                raise ReviewInputError(
                    f"{context} must contain at least one positive bar value"
                )
            continue

        keys = required | {"nodes", "edges"}
        _require_keys(visualization, required=keys, allowed=keys, context=context)
        nodes = visualization["nodes"]
        if not isinstance(nodes, list) or not nodes:
            raise ReviewInputError(f"{context}.nodes must be a non-empty list")
        node_ids: set[str] = set()
        for node_index, raw_node in enumerate(nodes):
            node_context = f"{context}.nodes[{node_index}]"
            node = _require_object(raw_node, node_context)
            node_keys = {"id", "label"}
            _require_keys(node, required=node_keys, allowed=node_keys, context=node_context)
            node_id = _require_string(node["id"], f"{node_context}.id")
            _require_string(node["label"], f"{node_context}.label")
            if node_id in node_ids:
                raise ReviewInputError(f"{context} contains duplicate node id: {node_id}")
            node_ids.add(node_id)

        edges = visualization["edges"]
        if not isinstance(edges, list) or not edges:
            raise ReviewInputError(f"{context}.edges must be a non-empty list")
        referenced_node_ids: set[str] = set()
        for edge_index, raw_edge in enumerate(edges):
            edge_context = f"{context}.edges[{edge_index}]"
            edge = _require_object(raw_edge, edge_context)
            edge_keys = {"from", "to", "label"}
            _require_keys(edge, required=edge_keys, allowed=edge_keys, context=edge_context)
            source = _require_string(edge["from"], f"{edge_context}.from")
            target = _require_string(edge["to"], f"{edge_context}.to")
            _require_string(edge["label"], f"{edge_context}.label")
            if source not in node_ids:
                raise ReviewInputError(
                    f"{edge_context}.from references unknown node: {source}"
                )
            if target not in node_ids:
                raise ReviewInputError(
                    f"{edge_context}.to references unknown node: {target}"
                )
            referenced_node_ids.update((source, target))
        for node in nodes:
            if node["id"] not in referenced_node_ids:
                raise ReviewInputError(
                    f"{context} contains unreferenced node id: {node['id']}"
                )

    _require_string_list(review["remaining_risks"], "remaining_risks", non_empty=False)

    provenance = _require_object(review["provenance"], "provenance")
    provenance_keys = {"repository", "revision", "generated_at"}
    _require_keys(
        provenance,
        required=provenance_keys,
        allowed=provenance_keys,
        context="provenance",
    )
    for key in provenance_keys:
        _require_string(provenance[key], f"provenance.{key}")

    return review


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def _render_string_list(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{_escape(item)}</li>" for item in items) + "</ul>"


def _render_changes(review: dict[str, Any], labels: dict[str, str]) -> str:
    rendered = []
    for change in review["changed_files"]:
        rendered.append(
            "".join(
                [
                    '<article class="change">',
                    f"<h3><code>{_escape(change['path'])}</code></h3>",
                    f"<p>{_escape(change['summary'])}</p>",
                    "<details>",
                    f"<summary>{_escape(labels['evidence'])}</summary>",
                    f"<pre tabindex=\"0\"><code>{_escape(change['evidence'])}</code></pre>",
                    "</details>",
                    "</article>",
                ]
            )
        )
    return "".join(rendered)


def _render_verification(review: dict[str, Any]) -> str:
    rendered = []
    for check in review["verification"]:
        status = check["status"]
        rendered.append(
            "".join(
                [
                    f'<article class="verification-item status-{status}">',
                    f'<span class="badge">{_escape(status.upper())}</span>',
                    f"<p><code>{_escape(check['command'])}</code></p>",
                    f"<p>{_escape(check['summary'])}</p>",
                    "</article>",
                ]
            )
        )
    return "".join(rendered)


def _render_findings(review: dict[str, Any], labels: dict[str, str]) -> str:
    if not review["findings"]:
        return f'<p class="muted">{_escape(labels["no_findings"])}</p>'

    rendered = []
    for finding in review["findings"]:
        severity = finding["severity"]
        location = ""
        if "location" in finding:
            location = f'<p class="location"><code>{_escape(finding["location"])}</code></p>'
        rendered.append(
            "".join(
                [
                    f'<article class="finding severity-{severity}">',
                    f'<span class="badge">{_escape(severity.upper())}</span>',
                    f"<h3>{_escape(finding['title'])}</h3>",
                    location,
                    f"<p>{_escape(finding['body'])}</p>",
                    "</article>",
                ]
            )
        )
    return "".join(rendered)


def _render_implementation_notes(
    review: dict[str, Any], labels: dict[str, str]
) -> str:
    priority = {
        "open-question": 0,
        "deviation": 1,
        "tradeoff": 2,
        "decision": 3,
    }
    rendered = []
    for note in sorted(
        review["implementation_notes"], key=lambda item: priority[item["kind"]]
    ):
        kind = note["kind"]
        label_key = f"note_{kind.replace('-', '_')}"
        location = ""
        if "location" in note:
            location = f'<p class="location"><code>{_escape(note["location"])}</code></p>'
        rendered.append(
            "".join(
                [
                    f'<article class="implementation-note note-{kind}">',
                    f'<span class="badge">{_escape(labels[label_key])}</span>',
                    f"<h3>{_escape(note['title'])}</h3>",
                    location,
                    f"<p>{_escape(note['body'])}</p>",
                    "</article>",
                ]
            )
        )
    return "".join(rendered)


def _format_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    return format(value, "g")


def _render_bar_visualization(
    visualization: dict[str, Any], labels: dict[str, str]
) -> str:
    maximum = max(item["value"] for item in visualization["items"])
    rows = []
    table_rows = []
    for item in visualization["items"]:
        value = item["value"]
        width = 100.0 if value == maximum else (value / maximum) * 100
        formatted_value = _format_number(value)
        rows.append(
            "".join(
                [
                    '<div class="bar-row">',
                    f'<span>{_escape(item["label"])}</span>',
                    '<span class="bar-track" aria-hidden="true">',
                    f'<span class="bar-fill" style="--bar-size: {width:.2f}%"></span>',
                    "</span>",
                    f"<strong>{_escape(formatted_value)}</strong>",
                    "</div>",
                ]
            )
        )
        table_rows.append(
            f"<tr><td>{_escape(item['label'])}</td><td>{_escape(formatted_value)}</td></tr>"
        )

    title = _escape(visualization["title"])
    summary = _escape(visualization["summary"])
    return "".join(
        [
            '<figure class="visualization visualization-bar">',
            f"<figcaption><strong>{title}</strong><span>{summary}</span></figcaption>",
            f'<div class="bar-plot" role="img" aria-label="{title}. {summary}">',
            "".join(rows),
            "</div>",
            '<table class="chart-data">',
            f'<caption class="visually-hidden">{title}</caption>',
            f"<thead><tr><th scope=\"col\">{_escape(labels['category'])}</th>"
            f"<th scope=\"col\">{_escape(labels['value'])}</th></tr></thead>",
            f"<tbody>{''.join(table_rows)}</tbody>",
            "</table>",
            "</figure>",
        ]
    )


def _render_flow_visualization(
    visualization: dict[str, Any], labels: dict[str, str]
) -> str:
    node_labels = {node["id"]: node["label"] for node in visualization["nodes"]}
    edges = []
    table_rows = []
    for edge in visualization["edges"]:
        source = node_labels[edge["from"]]
        target = node_labels[edge["to"]]
        edges.append(
            "".join(
                [
                    '<div class="flow-edge">',
                    f'<div class="flow-node">{_escape(source)}</div>',
                    f'<div class="flow-relation">{_escape(edge["label"])}</div>',
                    f'<div class="flow-node">{_escape(target)}</div>',
                    "</div>",
                ]
            )
        )
        table_rows.append(
            "<tr>"
            f"<td>{_escape(source)}</td>"
            f"<td>{_escape(edge['label'])}</td>"
            f"<td>{_escape(target)}</td>"
            "</tr>"
        )

    title = _escape(visualization["title"])
    summary = _escape(visualization["summary"])
    return "".join(
        [
            '<figure class="visualization visualization-flow">',
            f"<figcaption><strong>{title}</strong><span>{summary}</span></figcaption>",
            f'<div class="flow-plot" role="img" aria-label="{title}. {summary}">',
            "".join(edges),
            "</div>",
            '<table class="chart-data">',
            f'<caption class="visually-hidden">{title}</caption>',
            f"<thead><tr><th scope=\"col\">{_escape(labels['from'])}</th>"
            f"<th scope=\"col\">{_escape(labels['relation'])}</th>"
            f"<th scope=\"col\">{_escape(labels['to'])}</th></tr></thead>",
            f"<tbody>{''.join(table_rows)}</tbody>",
            "</table>",
            "</figure>",
        ]
    )


def _render_visualizations(review: dict[str, Any], labels: dict[str, str]) -> str:
    rendered = []
    for visualization in review["visualizations"]:
        if visualization["type"] == "bar":
            rendered.append(_render_bar_visualization(visualization, labels))
        else:
            rendered.append(_render_flow_visualization(visualization, labels))
    return "".join(rendered)


def render_review(review: dict[str, Any]) -> str:
    language = review["language"]
    labels = LABELS[language]
    risks = (
        _render_string_list(review["remaining_risks"])
        if review["remaining_risks"]
        else f'<p class="muted">{_escape(labels["no_risks"])}</p>'
    )
    provenance = review["provenance"]
    open_question_count = sum(
        note["kind"] == "open-question" for note in review["implementation_notes"]
    )
    implementation_notes_section = ""
    if review["implementation_notes"]:
        implementation_notes_section = (
            '<section class="panel wide implementation-notes">'
            f"<h2>{_escape(labels['implementation_notes'])}</h2>"
            f'<div class="note-grid">{_render_implementation_notes(review, labels)}</div>'
            "</section>"
        )
    visualization_section = ""
    if review["visualizations"]:
        visualization_section = (
            '<section class="panel wide visualizations">'
            f"<h2>{_escape(labels['visualizations'])}</h2>"
            f"{_render_visualizations(review, labels)}"
            "</section>"
        )

    return f"""<!DOCTYPE html>
<html lang="{language}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; font-src 'none'; connect-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'">
  <title>{_escape(review['title'])}</title>
  <style>{CSS}</style>
</head>
<body>
  <main>
    <header class="hero">
      <p class="eyebrow">{_escape(labels['eyebrow'])}</p>
      <h1>{_escape(review['title'])}</h1>
      <div class="summary-grid">
        <div class="summary-card"><strong>{len(review['changed_files'])}</strong><span>{_escape(labels['files'])}</span></div>
        <div class="summary-card"><strong>{len(review['verification'])}</strong><span>{_escape(labels['checks'])}</span></div>
        <div class="summary-card"><strong>{open_question_count}</strong><span>{_escape(labels['open_questions'])}</span></div>
      </div>
    </header>

    <div class="content-grid">
      <section class="panel">
        <h2>{_escape(labels['objective'])}</h2>
        <p>{_escape(review['objective'])}</p>
      </section>
      <section class="panel">
        <h2>{_escape(labels['scope'])}</h2>
        {_render_string_list(review['scope'])}
      </section>
      {visualization_section}
      {implementation_notes_section}
      <section class="panel wide">
        <h2>{_escape(labels['changes'])}</h2>
        {_render_changes(review, labels)}
      </section>
      <section class="panel wide">
        <h2>{_escape(labels['verification'])}</h2>
        {_render_verification(review)}
      </section>
      <section class="panel wide">
        <h2>{_escape(labels['findings'])}</h2>
        {_render_findings(review, labels)}
      </section>
      <section class="panel">
        <h2>{_escape(labels['risks'])}</h2>
        {risks}
      </section>
      <section class="panel">
        <h2>{_escape(labels['provenance'])}</h2>
        <dl>
          <dt>{_escape(labels['repository'])}</dt><dd><code>{_escape(provenance['repository'])}</code></dd>
          <dt>{_escape(labels['revision'])}</dt><dd><code>{_escape(provenance['revision'])}</code></dd>
          <dt>{_escape(labels['generated_at'])}</dt><dd>{_escape(provenance['generated_at'])}</dd>
        </dl>
      </section>
    </div>
  </main>
</body>
</html>
"""


def load_review(input_path: Path) -> dict[str, Any]:
    try:
        raw = input_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ReviewInputError(f"cannot read input: {error}") from error

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ReviewInputError(
            f"invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}"
        ) from error
    return validate_review(data)


def write_secure_html(output_path: Path, content: str) -> None:
    parent = output_path.parent
    if not parent.is_dir():
        raise ReviewInputError(f"output directory does not exist: {parent}")

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        dir=parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary_path, output_path)
        output_path.chmod(0o600)
    finally:
        temporary_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render strict agent-review JSON as self-contained static HTML."
    )
    parser.add_argument("--input", required=True, type=Path, help="Path to review JSON")
    parser.add_argument("--output", required=True, type=Path, help="Path to output HTML")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.input.resolve() == args.output.resolve():
            raise ReviewInputError("input and output paths must differ")
        review = load_review(args.input)
        write_secure_html(args.output, render_review(review))
    except ReviewInputError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"error: cannot write output: {error}", file=sys.stderr)
        return 2

    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
