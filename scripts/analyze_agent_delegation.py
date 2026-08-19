#!/usr/bin/env python3
"""Aggregate privacy-preserving delegation metrics from Codex JSONL logs."""

from __future__ import annotations

import argparse
import heapq
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def rounded(value: float) -> float:
    return round(value, 3)


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * quantile)]


def stats(values: Iterable[float]) -> dict[str, float | int | None]:
    data = list(values)
    return {
        "n": len(data),
        "median": rounded(statistics.median(data)) if data else None,
        "p90": rounded(percentile(data, 0.9)) if data else None,
        "max": rounded(max(data)) if data else None,
    }


def load_object(line: bytes) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def read_session_meta(path: Path) -> tuple[dict[str, Any], datetime | None]:
    with path.open("rb") as handle:
        for index, line in enumerate(handle):
            item = load_object(line)
            if item and item.get("type") == "session_meta":
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                timestamp = parse_time(payload.get("timestamp")) or parse_time(item.get("timestamp"))
                return payload, timestamp
            if index >= 99:
                break
    return {}, None


def subagent_parent(payload: dict[str, Any]) -> str | None:
    if payload.get("thread_source") != "subagent":
        return None
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    subagent = source.get("subagent") if isinstance(source.get("subagent"), dict) else {}
    spawn = subagent.get("thread_spawn") if isinstance(subagent.get("thread_spawn"), dict) else {}
    parent = spawn.get("parent_thread_id")
    return str(parent) if parent else None


def tool_calls(path: Path) -> list[str]:
    calls: list[str] = []
    with path.open("rb") as handle:
        for line in handle:
            if b"spawn_agent" not in line and b"wait_agent" not in line:
                continue
            item = load_object(line)
            if not item or item.get("type") != "response_item":
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            if payload.get("type") != "function_call":
                continue
            name = payload.get("name")
            if name in {"spawn_agent", "wait_agent"}:
                calls.append(str(name))
    return calls


def task_intervals(
    path: Path,
    session_started_at: datetime | None,
) -> tuple[list[tuple[str, datetime, datetime]], set[str]]:
    if session_started_at is None:
        return [], set()

    intervals: list[tuple[str, datetime, datetime]] = []
    started_turns: set[str] = set()
    completed_turns: set[str] = set()
    starts_by_turn: dict[str, float] = {}
    replay_cutoff = session_started_at.timestamp() - 5

    with path.open("rb") as handle:
        for line in handle:
            if b"task_started" not in line and b"task_complete" not in line:
                continue
            item = load_object(line)
            if not item or item.get("type") != "event_msg":
                continue
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            event_type = payload.get("type")
            if event_type not in {"task_started", "task_complete"}:
                continue
            turn_id = payload.get("turn_id")
            if not isinstance(turn_id, str):
                continue
            if event_type == "task_started":
                started_at = payload.get("started_at")
                if not isinstance(started_at, (int, float)) or float(started_at) < replay_cutoff:
                    continue
                starts_by_turn[turn_id] = float(started_at)
                started_turns.add(turn_id)
                continue
            duration_ms = payload.get("duration_ms")
            if not isinstance(duration_ms, (int, float)) or duration_ms < 0:
                continue
            started_at = payload.get("started_at")
            if not isinstance(started_at, (int, float)):
                started_at = starts_by_turn.get(turn_id)
            if not isinstance(started_at, (int, float)):
                completed_at = payload.get("completed_at")
                if isinstance(completed_at, (int, float)):
                    started_at = float(completed_at) - float(duration_ms) / 1000
            if not isinstance(started_at, (int, float)) or float(started_at) < replay_cutoff:
                continue
            start = datetime.fromtimestamp(float(started_at), timezone.utc)
            end = start + timedelta(milliseconds=float(duration_ms))
            intervals.append((turn_id, start, end))
            completed_turns.add(turn_id)

    return intervals, started_turns - completed_turns


def dispatch_summary(paths: list[Path], metadata: dict[Path, dict[str, Any]]) -> dict[str, Any]:
    batch_sizes: list[float] = []
    spawn_calls = 0
    wait_calls = 0
    root_logs_with_calls = 0

    for path in paths:
        if subagent_parent(metadata[path]) is not None:
            continue
        calls = tool_calls(path)
        if calls:
            root_logs_with_calls += 1
        batch_size = 0
        for name in calls:
            if name == "spawn_agent":
                spawn_calls += 1
                batch_size += 1
            else:
                wait_calls += 1
                if batch_size:
                    batch_sizes.append(float(batch_size))
                    batch_size = 0
        if batch_size:
            batch_sizes.append(float(batch_size))

    return {
        "root_logs_with_calls": root_logs_with_calls,
        "spawn_calls": spawn_calls,
        "wait_calls": wait_calls,
        "batches": len(batch_sizes),
        "single_spawn_batches": sum(size == 1 for size in batch_sizes),
        "multi_spawn_batches": sum(size >= 2 for size in batch_sizes),
        "spawns_in_multi_batches": int(sum(size for size in batch_sizes if size >= 2)),
        "batch_size": stats(batch_sizes),
    }


def peak_concurrency(intervals: list[tuple[datetime, datetime]]) -> int:
    active_ends: list[datetime] = []
    peak = 0
    for start, end in sorted(intervals):
        while active_ends and active_ends[0] <= start:
            heapq.heappop(active_ends)
        heapq.heappush(active_ends, end)
        peak = max(peak, len(active_ends))
    return peak


def overlapping_waves(
    intervals: list[tuple[datetime, datetime]],
) -> list[list[tuple[datetime, datetime]]]:
    waves: list[list[tuple[datetime, datetime]]] = []
    current: list[tuple[datetime, datetime]] = []
    wave_end: datetime | None = None

    for interval in sorted(intervals):
        start, end = interval
        if current and wave_end is not None and start >= wave_end:
            waves.append(current)
            current = []
            wave_end = None
        current.append(interval)
        wave_end = end if wave_end is None else max(wave_end, end)
    if current:
        waves.append(current)
    return waves


def task_run_summary(
    paths: list[Path],
    metadata: dict[Path, dict[str, Any]],
    session_starts: dict[Path, datetime | None],
) -> dict[str, Any]:
    by_parent: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    durations: list[float] = []
    completed_turn_ids: set[str] = set()
    incomplete_turn_ids: set[str] = set()

    for path in paths:
        parent = subagent_parent(metadata[path])
        if parent is None:
            continue
        intervals, path_incomplete = task_intervals(path, session_starts[path])
        incomplete_turn_ids.update(path_incomplete)
        for turn_id, start, end in intervals:
            if turn_id in completed_turn_ids:
                continue
            completed_turn_ids.add(turn_id)
            by_parent[parent].append((start, end))
            durations.append((end - start).total_seconds())

    peaks = [float(peak_concurrency(intervals)) for intervals in by_parent.values()]
    multi_agent_waves = 0
    sequential_seconds = 0.0
    wall_seconds = 0.0
    overlap_seconds = 0.0
    for intervals in by_parent.values():
        for wave in overlapping_waves(intervals):
            if len(wave) < 2:
                continue
            multi_agent_waves += 1
            sequential = sum((end - start).total_seconds() for start, end in wave)
            wall = (max(end for _, end in wave) - min(start for start, _ in wave)).total_seconds()
            sequential_seconds += sequential
            wall_seconds += wall
            overlap_seconds += max(0.0, sequential - wall)

    return {
        "count": len(durations),
        "parent_threads": len(by_parent),
        "incomplete": len(incomplete_turn_ids - completed_turn_ids),
        "duration_seconds": stats(durations),
        "peak_concurrency_per_parent": stats(peaks),
        "overlap_waves": {
            "multi_agent_waves": multi_agent_waves,
            "sequential_equivalent_seconds": rounded(sequential_seconds),
            "wall_clock_span_seconds": rounded(wall_seconds),
            "observed_overlap_seconds": rounded(overlap_seconds),
        },
    }


def analyze(root: Path) -> dict[str, Any]:
    paths = sorted(root.rglob("*.jsonl"))
    metadata: dict[Path, dict[str, Any]] = {}
    session_starts: dict[Path, datetime | None] = {}
    for path in paths:
        payload, session_start = read_session_meta(path)
        metadata[path] = payload
        session_starts[path] = session_start

    return {
        "schema_version": 1,
        "privacy": {
            "emits_prompt_content": False,
            "emits_response_content": False,
            "emits_tool_arguments": False,
            "emits_tool_outputs": False,
        },
        "files_scanned": len(paths),
        "dispatch": dispatch_summary(paths, metadata),
        "task_runs": task_run_summary(paths, metadata, session_starts),
        "limitations": [
            "A dispatch batch is consecutive spawn_agent calls before the next wait_agent call.",
            "Observed overlap is reconstructed from task_started/task_complete timestamps; it is not a causal speedup estimate.",
            "Task independence and output quality are not inferred from log contents.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate delegation counts and observed overlap without emitting log content."
    )
    parser.add_argument(
        "--sessions-root",
        type=Path,
        default=Path.home() / ".codex" / "sessions",
        help="Codex sessions directory (default: ~/.codex/sessions)",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args()

    root = args.sessions_root.expanduser()
    if not root.is_dir():
        parser.error(f"sessions root does not exist or is not a directory: {root}")

    json.dump(analyze(root), sys.stdout, ensure_ascii=False, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
