from __future__ import annotations

import plistlib
from pathlib import Path

from agent_job_scheduler.launchd import render_launch_agent_plist


def test_render_launch_agent_plist_contains_expected_program_arguments(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    bin_path = tmp_path / "bin" / "agent-job-scheduler"
    bin_path.parent.mkdir(parents=True)
    bin_path.write_text("#!/bin/sh\n", encoding="utf-8")

    rendered = render_launch_agent_plist(
        runtime_root=runtime_root,
        label="example.scheduler",
        interval_seconds=90,
        bin_path=bin_path,
    )
    payload = plistlib.loads(rendered.encode("utf-8"))

    assert payload["Label"] == "example.scheduler"
    assert payload["StartInterval"] == 90
    assert payload["RunAtLoad"] is True
    assert payload["ProgramArguments"] == [
        str(bin_path.resolve()),
        "--runtime-root",
        str(runtime_root),
        "run-once",
    ]
    assert payload["StandardOutPath"].endswith("/logs/launchd.stdout.log")
    assert payload["StandardErrorPath"].endswith("/logs/launchd.stderr.log")
