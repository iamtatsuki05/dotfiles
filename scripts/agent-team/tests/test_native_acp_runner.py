from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agent_team import adapters, cli, native_backend, tmux_backend
from agent_team.adapters import ExecutionError, ProcessResult
from agent_team.native_acp_dependencies import NativeAcpExecutables, adapter_snapshot
from agent_team.runtime import read_state
from agent_team.runtime_mcp import RuntimeMcpSession
from agent_team.scoped_acp import (
    SCOPED_AGENT,
    SCOPED_CLIENT,
    SCOPED_POLICY,
    SCOPED_QUESTIONS,
    checked_digest,
    create_write_policy,
)


class NativeAcpRunnerTest(unittest.TestCase):
    def test_native_signal_records_cancellation_without_interrupting_cleanup(
        self,
    ) -> None:
        state, _executables, prompt = self._state()
        observed = []

        def turn(**kwargs):
            signal.raise_signal(signal.SIGTERM)
            signal.raise_signal(signal.SIGINT)
            observed.append(kwargs.get("cancellation"))
            return 1

        with (
            mock.patch.object(cli, "read_state", return_value=state),
            mock.patch.object(cli, "_acp_run_turn", side_effect=turn),
            mock.patch.object(native_backend, "publish_completion") as publish,
        ):
            result = cli.acp_run(
                role="planner",
                state_path=self.state_dir / "state.json",
                task_id="task-1",
                dispatch_id="dispatch-1",
                terminal_handle="terminal-1",
                prompt_path=prompt,
                launch_nonce="planner1234",
            )
        self.assertEqual(result, 1)
        self.assertEqual(
            len(observed), 1, "SIGTERM interrupted the trusted cleanup frame"
        )
        self.assertTrue(observed[0].is_set())
        publish.assert_not_called()

    def test_real_signal_during_client_cleanup_does_not_abort_the_cleanup(self) -> None:
        state, _executables, prompt = self._state()
        exits = []
        group_exited = adapters._process_group_exited
        first = True

        def inspect_group(group):
            nonlocal first
            if first:
                first = False
                return False
            return group_exited(group)

        def turn(**kwargs):
            runner = cli._NativeAcpClientRunner(cancellation=kwargs["cancellation"])
            stop = runner._stop

            def signal_before_cleanup(process, group):
                signal.raise_signal(signal.SIGTERM)
                stop(process, group)

            with mock.patch.object(
                runner, "_stop", side_effect=signal_before_cleanup
            ) as stopped:
                result = runner.run(
                    [sys.executable, "-c", "raise SystemExit(1)"],
                    cwd=self.workspace,
                    env={},
                    timeout_seconds=5,
                )
            stopped.assert_called_once()
            self.assertTrue(kwargs["cancellation"].is_set())
            exits.append((result.returncode, runner.completed_returncode))
            return 1

        with (
            mock.patch.object(cli, "read_state", return_value=state),
            mock.patch.object(cli, "_acp_run_turn", side_effect=turn),
            mock.patch.object(adapters, "_process_group_exited", inspect_group),
        ):
            result = cli.acp_run(
                role="planner",
                state_path=self.state_dir / "state.json",
                task_id="task-1",
                dispatch_id="dispatch-1",
                terminal_handle="terminal-1",
                prompt_path=prompt,
                launch_nonce="planner1234",
            )
        self.assertEqual(result, 1)
        self.assertEqual(exits, [(1, 1)])

    def test_cancellation_before_client_launch_publishes_known_no_process_failure(
        self,
    ) -> None:
        state, _executables, prompt = self._state()
        cancellation = threading.Event()
        cancellation.set()
        with (
            mock.patch.object(native_backend, "_assert_publisher"),
            mock.patch.object(cli.ProcessRunner, "run") as run,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = cli._acp_run_turn(
                state=state,
                role="planner",
                state_path=self.state_dir / "state.json",
                task_id="task-1",
                dispatch_id="dispatch-1",
                terminal_handle="terminal-1",
                prompt_path=prompt,
                launch_nonce="planner1234",
                cancellation=cancellation,
            )
        self.assertEqual(result, 1)
        run.assert_not_called()
        result = read_state(self.state_dir / "state.json")["native_result"]
        self.assertEqual(result["outcome"], "failed")
        self.assertTrue(result["cleanup_confirmed"])

    def test_cancellation_after_question_context_setup_still_avoids_provider_start(
        self,
    ) -> None:
        state, _executables, prompt = self._state()
        cancellation = threading.Event()

        @contextlib.contextmanager
        def question_context(*_args, **_kwargs):
            cancellation.set()
            yield

        with (
            mock.patch.object(native_backend, "_assert_publisher"),
            mock.patch.object(cli, "_native_question_context", question_context),
            mock.patch.object(adapters.subprocess, "Popen") as popen,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = cli._acp_run_turn(
                state=state,
                role="planner",
                state_path=self.state_dir / "state.json",
                task_id="task-1",
                dispatch_id="dispatch-1",
                terminal_handle="terminal-1",
                prompt_path=prompt,
                launch_nonce="planner1234",
                cancellation=cancellation,
            )
        self.assertEqual(result, 1)
        popen.assert_not_called()
        self.assertTrue(
            read_state(self.state_dir / "state.json")["native_result"][
                "cleanup_confirmed"
            ]
        )

    def test_cancellation_event_after_client_result_prevents_success(self) -> None:
        state, _executables, prompt = self._state()
        receipt = {
            "output": "fixture complete",
            "session_id": "fixture-session",
            "model": "fable",
            "effort": "high",
            "cleanup_confirmed": True,
        }
        self.write_client_result(receipt)
        command = [
            sys.executable,
            "-c",
            "import sys;sys.stdin.read();print(" + repr(json.dumps(receipt)) + ")",
        ]
        run = cli._NativeAcpClientRunner.run

        def request_after_return(runner, *args, **kwargs):
            result = run(runner, *args, **kwargs)
            runner._cancellation.set()
            return result

        with (
            mock.patch.object(cli, "client_argv", return_value=tuple(command)),
            mock.patch.object(cli._NativeAcpClientRunner, "run", request_after_return),
            mock.patch.object(native_backend, "_assert_publisher"),
            contextlib.redirect_stderr(io.StringIO()),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = self._run(state, self.state_dir / "state.json", prompt)
        self.assertEqual(result, 1)
        published = read_state(self.state_dir / "state.json")["native_result"]
        self.assertEqual(published["outcome"], "failed")
        self.assertTrue(published["cleanup_confirmed"])

    def test_orca_runner_does_not_install_native_signal_handlers(self) -> None:
        state, _executables, prompt = self._state()
        state["runtime"] = "orca"
        with (
            mock.patch.object(cli, "read_state", return_value=state),
            mock.patch.object(cli, "_acp_run_turn", return_value=0) as turn,
            mock.patch.object(cli.signal, "signal") as install,
        ):
            result = cli.acp_run(
                role="planner",
                state_path=self.state_dir / "state.json",
                task_id="task-1",
                dispatch_id="dispatch-1",
                terminal_handle="terminal-1",
                prompt_path=prompt,
                launch_nonce="planner1234",
            )
        self.assertEqual(result, 0)
        self.assertIsNone(turn.call_args.kwargs["cancellation"])
        install.assert_not_called()

    def test_cancellation_during_final_group_probe_retains_client_exit(self) -> None:
        runner = cli._NativeAcpClientRunner(cancellation=threading.Event())
        group_exited = adapters._process_group_exited
        interrupted = False

        def interrupt_probe(group):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise cli.NativeAcpCancelled("cancel during the final group probe")
            return group_exited(group)

        with (
            mock.patch.object(adapters, "_process_group_exited", interrupt_probe),
            self.assertRaises(ExecutionError) as raised,
        ):
            runner.run(
                [sys.executable, "-c", "raise SystemExit(1)"],
                cwd=self.workspace,
                env={},
                timeout_seconds=5,
            )
        self.assertTrue(raised.exception.cleanup_confirmed)
        self.assertEqual(runner.completed_returncode, 1)

    def test_cancellation_after_run_return_keeps_typed_cleanup_proof(self) -> None:
        state, _executables, prompt = self._state()
        state_path = self.state_dir / "state.json"
        receipt = {
            "error": "fixture cancellation finished",
            "session_id": "fixture-session",
            "model": "fable",
            "effort": "high",
            "cleanup_confirmed": True,
        }
        self.write_client_result(receipt)
        command = [
            sys.executable,
            "-c",
            "import sys;sys.stdin.read();print("
            + repr(json.dumps(receipt))
            + ");raise SystemExit(1)",
        ]
        run = cli._NativeAcpClientRunner.run

        def interrupt_return(runner, *args, **kwargs):
            run(runner, *args, **kwargs)
            raise cli.NativeAcpCancelled("cancel after the client result returned")

        with (
            mock.patch.object(cli, "client_argv", return_value=tuple(command)),
            mock.patch.object(cli._NativeAcpClientRunner, "run", interrupt_return),
            mock.patch.object(native_backend, "_assert_publisher"),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = self._run(state, state_path, prompt)
        self.assertEqual(result, 1)
        saved = read_state(state_path)["native_result"]
        self.assertEqual(saved["outcome"], "failed")
        self.assertTrue(saved["cleanup_confirmed"], saved["body"])

    def test_failed_final_group_cleanup_is_not_retried_or_promoted(self) -> None:
        failure = ExecutionError(
            "fixture group cleanup unknown", cleanup_confirmed=False
        )
        with (
            mock.patch.object(adapters, "_process_group_exited", return_value=False),
            mock.patch.object(
                adapters.ProcessRunner, "_stop", side_effect=failure
            ) as stop,
            self.assertRaises(ExecutionError) as raised,
        ):
            adapters.ProcessRunner().run(
                [sys.executable, "-c", "raise SystemExit(1)"],
                cwd=self.workspace,
                env={},
                timeout_seconds=5,
            )
        self.assertIs(raised.exception, failure)
        self.assertFalse(raised.exception.cleanup_confirmed)
        stop.assert_called_once()

    def test_cancellation_at_stop_return_uses_only_finished_cleanup_proof(self) -> None:
        for finished in (False, True):
            with self.subTest(finished=finished):
                runner = cli._NativeAcpClientRunner(cancellation=threading.Event())
                stop = runner._stop
                group_exited = adapters._process_group_exited
                initial = True

                def inspect_group(group, group_exited=group_exited):
                    nonlocal initial
                    if initial:
                        initial = False
                        return False
                    return group_exited(group)

                def interrupt_stop(process, group, finished=finished, stop=stop):
                    if finished:
                        stop(process, group)
                    raise cli.NativeAcpCancelled("cancel at stop return")

                with (
                    mock.patch.object(adapters, "_process_group_exited", inspect_group),
                    mock.patch.object(
                        runner, "_stop", side_effect=interrupt_stop
                    ) as stopped,
                    self.assertRaises(ExecutionError) as raised,
                ):
                    runner.run(
                        [sys.executable, "-c", "raise SystemExit(1)"],
                        cwd=self.workspace,
                        env={},
                        timeout_seconds=5,
                    )
                self.assertIs(raised.exception.cleanup_confirmed, finished)
                stopped.assert_called_once()

    def test_completed_returncode_is_cleared_before_the_next_run(self) -> None:
        runner = cli._NativeAcpClientRunner(cancellation=threading.Event())
        runner.run(
            [sys.executable, "-c", "raise SystemExit(0)"],
            cwd=self.workspace,
            env={},
            timeout_seconds=5,
        )
        self.assertEqual(runner.completed_returncode, 0)
        with self.assertRaises(ExecutionError):
            runner.run([], cwd=self.workspace, env={})
        self.assertIsNone(runner.completed_returncode)

    def _question_state(
        self,
    ) -> tuple[dict[str, object], NativeAcpExecutables, Path, RuntimeMcpSession]:
        state, executables, prompt_path = self._state()
        sdk = Path(os.environ["AGENT_TEAM_SDK_ENTRY"]).resolve(strict=True)
        dummy_sdk = executables.sdk.parent.parent
        shutil.rmtree(dummy_sdk)
        dummy_sdk.symlink_to(sdk.parent.parent, target_is_directory=True)
        source_node = Path(shutil.which("node")).resolve(strict=True)
        node = executables.node
        shutil.copyfile(source_node, node)
        node.chmod(0o700)
        library_source = (
            Path(__file__).with_name("fixtures") / "native_question_agent.mjs"
        )
        executables.library.write_bytes(library_source.read_bytes())
        executables = replace(
            executables,
            node=node,
            sdk=sdk,
            node_sha256=checked_digest(node),
            sdk_sha256=checked_digest(sdk),
            library_sha256=checked_digest(executables.library),
        )
        executables.verify()
        assignment = state["roles"]["planner"]
        state["role_specs"]["planner"]["acp_executables"] = executables.as_dict()
        assignment["adapter_snapshot"] = adapter_snapshot(executables)
        assignment["agent_command"] = cli.acp_agent_command(
            "agent-team-test",
            "planner",
            "planner1234",
            executables=executables,
            write_policy=Path(assignment["write_policy_path"]),
            questions=True,
        )
        state_path = self.state_dir / "state.json"
        cli.write_state(state_path, state)
        backend = tmux_backend.TmuxBackend(launcher_path=Path(state["launcher_path"]))
        backend._state = state
        session = RuntimeMcpSession.__new__(RuntimeMcpSession)
        session.path = state_path
        session.run_id = state["run_id"]
        session.runtime = "tmux"
        session.backend = backend
        return state, executables, prompt_path, session

    @unittest.skipUnless(
        shutil.which("node") and os.environ.get("AGENT_TEAM_SDK_ENTRY"),
        "selected Node and ACP SDK required",
    )
    def test_question_connects_real_sdk_wrapper_channel_and_durable_mcp(self) -> None:
        state, executables, prompt_path, session = self._question_state()
        state_path = self.state_dir / "state.json"
        assignment = state["roles"]["planner"]
        node = executables.node
        with (
            mock.patch.object(native_backend, "_assert_publisher"),
            mock.patch.object(cli, "ACP_TIMEOUT_SECONDS", 20),
            mock.patch.object(
                cli,
                "acp_env",
                return_value={
                    "HOME": str(self.root),
                    "PATH": os.pathsep.join((str(node.parent), "/usr/bin", "/bin")),
                },
            ),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            worker = executor.submit(self._run, state, state_path, prompt_path)
            try:
                wait = session.execute(
                    "role_wait", {"role": "planner", "timeout_ms": 10_000}
                )
                self.assertIsNotNone(wait["delivery_id"])
                self.assertEqual(wait["events"][0]["kind"], "question")
                self.assertFalse(worker.done())
                question = read_state(state_path)["native_question"]
                message_id = wait["events"][0]["message_id"]
                self.assertEqual(question["dispatch_id"], assignment["dispatch_id"])
                session.execute(
                    "message_reply",
                    {
                        "message_id": message_id,
                        "body": "設定ファイルを根拠にしてください",
                    },
                )
                self.assertEqual(
                    read_state(state_path)["native_question"]["phase"], "observed"
                )
                self.assertNotIn("native_result", read_state(state_path))
                session.execute("delivery_ack", {"delivery_id": wait["delivery_id"]})
            finally:
                result = worker.result(timeout=45)
        self.assertEqual(result, 0)
        final = read_state(state_path)
        self.assertNotIn("native_question", final)
        self.assertFalse(Path(assignment["question_socket"]).exists())
        self.assertEqual(final["native_result"]["outcome"], "succeeded")
        self.assertIn(
            "設定ファイルを根拠にしてください", final["native_result"]["body"]
        )
        receipt = final["native_result"]["question_receipts"][0]
        self.assertEqual(receipt["delivery_id"], wait["delivery_id"])
        messages = [
            json.loads(line)
            for line in executables.library.with_name("wire.jsonl")
            .read_text()
            .splitlines()
        ]
        for method in ("initialize", "session/new", "session/prompt", "session/close"):
            self.assertEqual(
                sum(message.get("method") == method for message in messages), 1
            )
        self.assertEqual(
            [
                message["elicitation"]
                for message in messages
                if message.get("event") == "parsed-initialize"
            ],
            [{"form": {}}],
        )
        self.assertEqual(receipt["session_id"], "native-question-fixture-session")

    @unittest.skipUnless(
        shutil.which("node") and os.environ.get("AGENT_TEAM_SDK_ENTRY"),
        "selected Node and ACP SDK required",
    )
    def test_question_interrupt_reads_session_cleanup_after_stdout_is_discarded(
        self,
    ) -> None:
        state, executables, prompt_path, session = self._question_state()
        state_path = self.state_dir / "state.json"
        cancelled = self.cancellation
        stopped = cli._NativeAcpClientRunner._stop

        with (
            mock.patch.object(native_backend, "_assert_publisher"),
            mock.patch.object(cli, "ACP_TIMEOUT_SECONDS", 20),
            mock.patch.object(
                cli,
                "acp_env",
                return_value={
                    "HOME": str(self.root),
                    "PATH": os.pathsep.join(
                        (str(executables.node.parent), "/usr/bin", "/bin")
                    ),
                },
            ),
            mock.patch.object(
                cli._NativeAcpClientRunner, "_stop", autospec=True, side_effect=stopped
            ) as stop,
            contextlib.redirect_stderr(io.StringIO()),
            ThreadPoolExecutor(max_workers=1) as executor,
        ):
            worker = executor.submit(self._run, state, state_path, prompt_path)
            try:
                wait = session.execute(
                    "role_wait", {"role": "planner", "timeout_ms": 10_000}
                )
                self.assertEqual(wait["events"][0]["kind"], "question")
                cancelled.set()
            finally:
                result = worker.result(timeout=45)
        self.assertEqual(result, 1)
        stop.assert_called_once()
        client_process = stop.call_args.args[1]
        self.assertEqual(client_process.returncode, 1)
        self.assertTrue(adapters._process_group_exited(client_process.pid))
        final = read_state(state_path)
        self.assertEqual(final["native_result"]["outcome"], "failed")
        self.assertTrue(
            (self.root / "provider" / "client-result.json").is_file(),
            final["native_result"]["body"],
        )
        artifact = json.loads(
            (self.root / "provider" / "client-result.json").read_text()
        )
        self.assertIs(
            final["native_result"]["cleanup_confirmed"],
            True,
            json.dumps(
                {"body": final["native_result"]["body"], "artifact": artifact},
                ensure_ascii=False,
            ),
        )
        self.assertEqual(final["native_question"]["phase"], "failed")
        self.assertEqual(final["pending_delivery_id"], wait["delivery_id"])
        self.assertNotEqual(final["native"].get("last_ack"), wait["delivery_id"])
        self.assertFalse((self.root / "provider" / "q.sock").exists())
        self.assertEqual(artifact["launch_nonce"], "planner1234")
        self.assertIs(artifact["receipt"]["cleanup_confirmed"], True)
        self.assertIn("SIGTERM", artifact["receipt"]["error"])
        messages = [
            json.loads(line)
            for line in executables.library.with_name("wire.jsonl")
            .read_text()
            .splitlines()
        ]
        self.assertEqual(
            sum(message.get("method") == "session/close" for message in messages), 1
        )
        self.assertTrue(
            any(message.get("method") == "session/cancel" for message in messages)
        )

    def setUp(self) -> None:
        self.cancellation = threading.Event()
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(mode=0o700)
        self.state_dir.chmod(0o700)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(mode=0o700)
        self.workspace.chmod(0o700)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _executables(self) -> NativeAcpExecutables:
        bin_dir = self.root / "bin"
        bin_dir.mkdir(mode=0o700)
        bin_dir.chmod(0o700)
        node = bin_dir / "node"
        node.write_text("#!/bin/sh\n", encoding="utf-8")
        node.chmod(0o700)
        for package_name, version, command in (
            ("@agentclientprotocol/sdk", "1.3.0", None),
            (
                "@agentclientprotocol/claude-agent-acp",
                "0.70.0",
                "claude-agent-acp",
            ),
        ):
            package = self.root / "node_modules" / package_name
            (package / "dist").mkdir(parents=True, mode=0o700)
            entry_name = "acp.js" if command is None else "index.js"
            metadata = {"name": package_name, "version": version}
            if command is None:
                metadata.update(
                    main="dist/acp.js", exports={".": {"import": "./dist/acp.js"}}
                )
            else:
                metadata.update(
                    bin={command: "dist/index.js"},
                    dependencies={"@agentclientprotocol/sdk": "1.3.0"},
                )
            (package / "package.json").write_text(
                json.dumps(metadata), encoding="utf-8"
            )
            entry = package / "dist" / entry_name
            entry.write_text("#!/bin/sh\n", encoding="utf-8")
            entry.chmod(0o700)
            if command is not None:
                (package / "dist" / "lib.js").write_text(
                    "export {};\n", encoding="utf-8"
                )
                (bin_dir / command).symlink_to(entry)
        return NativeAcpExecutables.resolve(path=str(bin_dir))

    def _state(self) -> tuple[dict[str, object], NativeAcpExecutables, Path]:
        executables = self._executables()
        state_path = self.state_dir / "state.json"
        prompt_path = cli.create_prompt_file(
            self.state_dir, "planner", "planner1234", "inspect the workspace"
        )
        provider_root = self.root / "provider"
        provider_root.mkdir(mode=0o700)
        policy, policy_digest = create_write_policy(
            provider_root,
            self.workspace,
            state_path,
            None,
            executables.agent,
            permission="read-only",
        )
        snapshot_root = self.root / "snapshot"
        snapshot_root.mkdir(mode=0o700)
        assignment = {
            "task_id": "task-1",
            "dispatch_id": "dispatch-1",
            "terminal_handle": "terminal-1",
            "completion_observed": False,
            "launcher_owned_runner": True,
            "launch_nonce": "planner1234",
            "prompt_path": str(prompt_path),
            "execution": "background",
            "adapter_id": "claude-acp-0.70.0",
            "agent_command": cli.acp_agent_command(
                "agent-team-test",
                "planner",
                "planner1234",
                executables=executables,
                write_policy=policy,
                questions=True,
            ),
            "session_name": "agent-team-planner-planner1234",
            "provider_private_root": str(provider_root),
            "snapshot_root": str(snapshot_root),
            "adapter_snapshot": adapter_snapshot(executables),
            "write_policy_path": str(policy),
            "write_policy_sha256": policy_digest,
            "question_socket": str(provider_root / "q.sock"),
        }
        state = {
            "version": 3,
            "runtime": "tmux",
            "team_id": "agent-team-test",
            "workspace": str(self.workspace),
            "config_path": str(self.root / "config.toml"),
            "state_path": str(state_path),
            "launcher_path": str(self.root / "agent-team"),
            "run_id": "run-1",
            "main_terminal": "main-terminal",
            "role_specs": {
                "main": {
                    "provider": "claude",
                    "transport": "direct",
                    "model": "fable",
                    "effort": "high",
                    "permission": "orchestrator",
                    "instructions": "main instructions",
                    "execution": "tui_direct",
                },
                "planner": {
                    "provider": "claude",
                    "transport": "acp",
                    "model": "fable",
                    "effort": "high",
                    "permission": "read-only",
                    "instructions": "planner instructions",
                    "execution": "background",
                    "adapter_id": "claude-acp-0.70.0",
                    "acp_executables": executables.as_dict(),
                    "scoped_wrapper_sha256": checked_digest(SCOPED_AGENT),
                    "scoped_client_sha256": checked_digest(SCOPED_CLIENT),
                    "scoped_policy_sha256": checked_digest(SCOPED_POLICY),
                    "scoped_question_client_sha256": checked_digest(SCOPED_QUESTIONS),
                },
            },
            "roles": {"planner": assignment},
            "native": {
                "phase": "running",
                "run_nonce": "mainnonce1234",
                "main_argv": ["/usr/bin/true"],
            },
        }
        cli.write_state(state_path, state)
        return state, executables, prompt_path

    def _run(
        self, state: dict[str, object], state_path: Path, prompt_path: Path
    ) -> int:
        return cli._acp_run_turn(
            cancellation=self.cancellation,
            state=state,
            role="planner",
            state_path=state_path,
            task_id="task-1",
            dispatch_id="dispatch-1",
            terminal_handle="terminal-1",
            prompt_path=prompt_path,
            launch_nonce="planner1234",
        )

    def write_client_result(
        self, receipt: dict[str, object], *, nonce: str = "planner1234"
    ) -> None:
        path = self.root / "provider" / "client-result.json"
        with path.open("x", encoding="utf-8") as target:
            json.dump({"version": 1, "launch_nonce": nonce, "receipt": receipt}, target)
        path.chmod(0o600)

    def test_nonzero_client_requires_matching_typed_cleanup_artifact(self) -> None:
        base = self.root
        for case in (
            "missing",
            "malformed",
            "wrong-nonce",
            "unconfirmed",
            "confirmed",
            "stdout-mismatch",
            "publication-failed",
        ):
            with self.subTest(case=case):
                self.root = base / case
                self.root.mkdir(mode=0o700)
                self.state_dir = self.root / "state"
                self.workspace = self.root / "workspace"
                self.state_dir.mkdir(mode=0o700)
                self.workspace.mkdir(mode=0o700)
                state, _executables, prompt_path = self._state()
                receipt = {
                    "error": "session close did not complete",
                    "session_id": "test-session",
                    "model": "fable",
                    "effort": "high",
                    "cleanup_confirmed": case != "unconfirmed",
                }

                def fail_client(
                    *_args: object,
                    case: str = case,
                    receipt: dict[str, object] = receipt,
                    **_kwargs: object,
                ) -> ProcessResult:
                    if case != "missing":
                        self.write_client_result(
                            receipt,
                            nonce="different1234"
                            if case == "wrong-nonce"
                            else "planner1234",
                        )
                        if case == "malformed":
                            (self.root / "provider" / "client-result.json").write_text(
                                "not-json", encoding="utf-8"
                            )
                    stdout = (
                        {**receipt, "cleanup_confirmed": False}
                        if case == "stdout-mismatch"
                        else receipt
                    )
                    return ProcessResult(
                        2 if case == "publication-failed" else 1,
                        json.dumps(stdout),
                        "misleading stderr: cleanup succeeded",
                    )

                with (
                    mock.patch.object(
                        cli.ProcessRunner, "run", side_effect=fail_client
                    ),
                    mock.patch.object(native_backend, "_assert_publisher"),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    result = self._run(
                        state, self.state_dir / "state.json", prompt_path
                    )
                self.assertEqual(result, 1)
                saved = read_state(self.state_dir / "state.json")
                self.assertEqual(saved["native_result"]["outcome"], "failed")
                self.assertIs(
                    saved["native_result"]["cleanup_confirmed"], case == "confirmed"
                )
                self.assertIn("planner", saved["roles"])
                self.assertTrue((self.root / "provider").is_dir())

    def test_native_validation_drift_publishes_failed_completion_before_provider(
        self,
    ) -> None:
        state, executables, prompt_path = self._state()
        executables.agent.write_text("changed\n", encoding="utf-8")
        state_path = self.state_dir / "state.json"

        with (
            mock.patch.object(
                cli,
                "run_acpx",
                side_effect=AssertionError(
                    "provider must not start after validation failure"
                ),
            ),
            mock.patch.object(native_backend, "_assert_publisher"),
        ):
            result = self._run(state, state_path, prompt_path)

        self.assertEqual(result, 1)
        published = read_state(state_path)["native_result"]
        self.assertIsInstance(published, dict)
        assert isinstance(published, dict)
        self.assertEqual(published["outcome"], "failed")
        self.assertTrue(published["cleanup_confirmed"])
        self.assertIn("ACP runner validation failed", published["body"])

    def test_native_prompt_validation_publishes_failed_completion_before_provider(
        self,
    ) -> None:
        state, _executables, prompt_path = self._state()
        prompt_path.unlink()
        state_path = self.state_dir / "state.json"

        with (
            mock.patch.object(
                cli,
                "run_acpx",
                side_effect=AssertionError(
                    "provider must not start after validation failure"
                ),
            ),
            mock.patch.object(native_backend, "_assert_publisher"),
        ):
            result = self._run(state, state_path, prompt_path)

        self.assertEqual(result, 1)
        published = read_state(state_path)["native_result"]
        self.assertIsInstance(published, dict)
        assert isinstance(published, dict)
        self.assertEqual(published["outcome"], "failed")
        self.assertTrue(published["cleanup_confirmed"])
        self.assertIn("ACP runner validation failed", published["body"])

    def test_exit_status_uses_the_published_outcome_after_stop_arbitration(
        self,
    ) -> None:
        state, _executables, prompt = self._state()
        receipt = {
            "output": "finished before stop publication",
            "session_id": "fixture-session",
            "model": "fable",
            "effort": "high",
            "cleanup_confirmed": True,
        }
        self.write_client_result(receipt)
        with (
            mock.patch.object(
                cli.ProcessRunner,
                "run",
                return_value=ProcessResult(0, json.dumps(receipt), ""),
            ),
            mock.patch.object(
                native_backend, "publish_completion", return_value="failed"
            ) as publish,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = self._run(state, self.state_dir / "state.json", prompt)
        self.assertEqual(publish.call_args.kwargs["outcome"], "succeeded")
        self.assertEqual(result, 1)

    def test_native_result_body_accepts_maximum_provider_output(self) -> None:
        state, _executables, prompt_path = self._state()
        state_path = self.state_dir / "state.json"
        output = "x" * cli.MAX_ACP_OUTPUT_CHARS
        prompt_completed = ProcessResult(
            0,
            json.dumps(
                {
                    "output": output,
                    "session_id": "test-session",
                    "model": "fable",
                    "effort": "high",
                    "cleanup_confirmed": True,
                }
            ),
            "",
        )

        def completed_client(*_args: object, **_kwargs: object) -> ProcessResult:
            self.write_client_result(json.loads(prompt_completed.stdout))
            return prompt_completed

        with (
            mock.patch.object(
                cli.ProcessRunner,
                "run",
                side_effect=completed_client,
            ),
            mock.patch.object(
                native_backend, "publish_completion", return_value="succeeded"
            ) as publish,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = self._run(state, state_path, prompt_path)

        self.assertEqual(result, 0)
        publish.assert_called_once()
        body = publish.call_args.kwargs["body"]
        self.assertEqual(len(body), cli.MAX_ACP_OUTPUT_CHARS)
        self.assertTrue(body.endswith(output[-100:]))

    def test_native_result_body_prioritizes_a_long_failure_reason(self) -> None:
        state, _executables, prompt_path = self._state()
        state_path = self.state_dir / "state.json"
        failure = "provider failure: " + ("reason " * 40_000)

        with (
            mock.patch.object(
                cli.ProcessRunner,
                "run",
                side_effect=ExecutionError(failure, cleanup_confirmed=True),
            ),
            mock.patch.object(
                native_backend, "publish_completion", return_value="failed"
            ) as publish,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = self._run(state, state_path, prompt_path)

        self.assertEqual(result, 1)
        publish.assert_called_once()
        body = publish.call_args.kwargs["body"]
        self.assertLessEqual(len(body), cli.MAX_ACP_OUTPUT_CHARS)
        self.assertIn("ACP runner failure: provider failure:", body)
        self.assertIn("reason reason", body)

    def test_native_validation_keeps_assignment_when_completion_identity_is_unknown(
        self,
    ) -> None:
        state, _executables, prompt_path = self._state()
        roles = state["roles"]
        assert isinstance(roles, dict)
        assignment = roles["planner"]
        assert isinstance(assignment, dict)
        assignment["task_id"] = "foreign-task"
        state_path = self.state_dir / "state.json"
        cli.write_state(state_path, state)

        with (
            mock.patch.object(
                cli,
                "run_acpx",
                side_effect=AssertionError(
                    "provider must not start after validation failure"
                ),
            ),
            mock.patch.object(native_backend, "publish_completion") as publish,
        ):
            result = self._run(state, state_path, prompt_path)

        self.assertEqual(result, 1)
        publish.assert_not_called()
        self.assertNotIn("native_result", read_state(state_path))


if __name__ == "__main__":
    unittest.main()
