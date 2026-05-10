from __future__ import annotations

import json
from pathlib import Path

from agent_job_scheduler.cli import _extract_runtime_root, _normalize_command_tokens, main
from agent_job_scheduler.ledger import Ledger
from agent_job_scheduler.models import Agent
from agent_job_scheduler.runtime import build_runtime_paths


def test_extract_runtime_root_supports_split_and_equals_forms(tmp_path: Path) -> None:
    runtime_root, remaining = _extract_runtime_root(["--runtime-root", str(tmp_path), "status"])
    assert runtime_root == tmp_path
    assert remaining == ["status"]

    runtime_root, remaining = _extract_runtime_root([f"--runtime-root={tmp_path}", "status"])
    assert runtime_root == tmp_path
    assert remaining == ["status"]


def test_normalize_command_tokens_maps_hyphenated_commands() -> None:
    assert _normalize_command_tokens(["run-once"]) == ["run_once"]
    assert _normalize_command_tokens(["set-stale-running-timeout", "60"]) == [
        "set_stale_running_timeout",
        "60",
    ]


def test_fire_cli_show_config_outputs_json(tmp_path: Path, capsys) -> None:
    main(["--runtime-root", str(tmp_path), "show-config"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["stale_running_timeout_seconds"] == 1800
    assert payload["rate_limit_profiles"]["codex"]["default_backoff_seconds"] == 900
    assert set(payload["rate_limit_profiles"]) == {agent.value for agent in Agent}


def test_fire_cli_enqueue_accepts_existing_flag_style(tmp_path: Path, capsys) -> None:
    runtime_root = tmp_path / "runtime"
    workdir = tmp_path / "workspace"
    workdir.mkdir()

    main(
        [
            "--runtime-root",
            str(runtime_root),
            "enqueue",
            "--agent",
            "codex",
            "--workdir",
            str(workdir),
            "--prompt",
            "hello from fire",
        ]
    )

    captured = capsys.readouterr()
    assert "enqueued job_id=" in captured.out

    jobs = Ledger(build_runtime_paths(runtime_root)).list_jobs()
    assert len(jobs) == 1
    assert jobs[0].prompt == "hello from fire"


def test_fire_cli_hyphenated_command_invokes_scheduler_method(tmp_path: Path, capsys) -> None:
    main(["--runtime-root", str(tmp_path), "set-stale-running-timeout", "75"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["stale_running_timeout_seconds"] == 75
