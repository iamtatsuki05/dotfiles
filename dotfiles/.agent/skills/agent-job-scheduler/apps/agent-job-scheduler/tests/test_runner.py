from __future__ import annotations

import sys
from pathlib import Path

from agent_job_scheduler.models import CommandSpec
from agent_job_scheduler.runner import SubprocessRunner


def test_subprocess_runner_merges_command_env(tmp_path: Path) -> None:
    spec = CommandSpec(
        argv=(
            sys.executable,
            "-c",
            "import os; print(os.environ['AGENT_JOB_SCHEDULER_TEST_ENV'])",
        ),
        cwd=tmp_path,
        display_command="print env",
        env={"AGENT_JOB_SCHEDULER_TEST_ENV": "ok"},
    )

    result = SubprocessRunner().run(spec)

    assert result.exit_code == 0
    assert result.stdout.strip() == "ok"
