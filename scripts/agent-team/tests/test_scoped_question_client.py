from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUESTION_CLIENT = ROOT / "agent_team" / "scoped_question_client.mjs"
NODE = shutil.which("node")


def form_request() -> dict[str, object]:
    return {
        "mode": "form",
        "sessionId": "session-1",
        "toolCallId": "tool-1",
        "message": "Please choose the deployment plan.",
        "requestedSchema": {
            "type": "object",
            "properties": {
                "question_0": {
                    "type": "string",
                    "title": "Plan",
                    "oneOf": [
                        {
                            "const": "safe",
                            "title": "Safe",
                            "description": "Use the conservative plan.",
                            "_meta": {
                                "_claude/askUserQuestionOption": {
                                    "preview": "preview: safe",
                                }
                            },
                        },
                        {
                            "const": "fast",
                            "title": "Fast",
                            "description": "Use the faster plan.",
                        },
                    ],
                },
                "question_0_custom": {
                    "type": "string",
                    "title": "Other",
                    "description": "Type another plan.",
                    "_meta": {
                        "_askUserQuestionCustomAnswer": {
                            "questionId": "question_0",
                            "isCustomAnswer": True,
                        }
                    },
                },
            },
        },
    }


@unittest.skipUnless(NODE, "Node.js is required for the question channel")
class ScopedQuestionClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="agent-team-question-")
        self.root = Path(self.directory.name)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.socket_path = self.private / "q.sock"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _probe(
        self, operation: str, value: object
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        script = """
const client = await import(process.argv[1]);
const operation = process.argv[2];
const value = JSON.parse(process.argv[3]);
try {
  const result = operation === "format" ? client.formatQuestionForm(value) : client.validateQuestionForm(value);
  process.stdout.write(JSON.stringify({ok: true, result}));
} catch (error) {
  process.stdout.write(JSON.stringify({ok: false, error: String(error?.message ?? error)}));
}
"""
        result = subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                script,
                str(QUESTION_CLIENT),
                operation,
                json.dumps(value, ensure_ascii=False),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result, json.loads(result.stdout)

    def test_form_formatter_preserves_single_select_option_context(self) -> None:
        result, payload = self._probe("format", form_request())
        self.assertEqual(result.stderr, "")
        self.assertTrue(payload["ok"], payload)
        formatted = payload["result"]
        self.assertEqual(formatted["fields"], ["question_0_custom"])
        body = formatted["questions"][0]["body"]
        self.assertIn("Please choose the deployment plan.", body)
        self.assertIn("Safe", body)
        self.assertIn("Use the conservative plan.", body)
        self.assertIn("preview: safe", body)
        self.assertIn("Fast", body)

    def test_form_formatter_preserves_multi_select_questions_and_rejects_unknown_fields(
        self,
    ) -> None:
        form = form_request()
        properties = form["requestedSchema"]["properties"]
        properties["question_0"]["description"] = "Choose the deployment plan."
        properties["question_1"] = {
            "type": "array",
            "description": "Choose the checks to run.",
            "items": {
                "anyOf": [
                    {"const": "lint", "title": "Lint", "description": "Run lint."},
                    {"const": "tests", "title": "Tests", "description": "Run tests."},
                ]
            },
        }
        properties["question_1_custom"] = {
            "type": "string",
            "_meta": {
                "_askUserQuestionCustomAnswer": {
                    "questionId": "question_1",
                    "isCustomAnswer": True,
                }
            },
        }
        _result, payload = self._probe("format", form)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(
            payload["result"]["fields"],
            ["question_0_custom", "question_1_custom"],
        )
        self.assertIn(
            "Choose the checks to run.", payload["result"]["questions"][1]["body"]
        )
        self.assertIn("Lint", payload["result"]["questions"][1]["body"])
        self.assertIn("Run tests.", payload["result"]["questions"][1]["body"])

        malformed = dict(form_request())
        malformed["unexpected"] = True
        _, malformed_payload = self._probe("validate", malformed)
        self.assertFalse(malformed_payload["ok"])
        self.assertIn("unknown", malformed_payload["error"])

    def test_prefilled_answer_and_unknown_schema_field_are_rejected(self) -> None:
        prefilled = form_request()
        prefilled["answers"] = {"question_0_custom": "model supplied"}
        _, payload = self._probe("validate", prefilled)
        self.assertFalse(payload["ok"])
        self.assertIn("unknown", payload["error"])

        malformed = form_request()
        malformed["requestedSchema"]["properties"]["question_0"]["default"] = "safe"
        _, malformed_payload = self._probe("validate", malformed)
        self.assertFalse(malformed_payload["ok"])
        self.assertIn("default", malformed_payload["error"])

        unsupported = form_request()
        unsupported["mode"] = "url"
        _, unsupported_payload = self._probe("validate", unsupported)
        self.assertFalse(unsupported_payload["ok"])
        self.assertIn("form", unsupported_payload["error"])

        oversized = form_request()
        oversized["requestedSchema"]["properties"]["question_0"]["oneOf"][0][
            "description"
        ] = "x" * 20_001
        _, oversized_payload = self._probe("validate", oversized)
        self.assertFalse(oversized_payload["ok"])
        self.assertIn("20000", oversized_payload["error"])

    def test_leading_zero_property_aliases_are_rejected(self) -> None:
        aliased = form_request()
        properties = aliased["requestedSchema"]["properties"]
        properties["question_00"] = dict(properties["question_0"])
        properties["question_00_custom"] = dict(properties["question_0_custom"])
        _, payload = self._probe("validate", aliased)
        self.assertFalse(payload["ok"])
        self.assertIn("unknown", payload["error"])

    def test_socket_exchange_sends_fixed_question_and_received_frames(self) -> None:
        frames: list[dict[str, object]] = []
        server_error: list[BaseException] = []
        ready = threading.Event()

        def serve() -> None:
            try:
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(str(self.socket_path))
                server.listen(1)
                self.socket_path.chmod(0o600)
                ready.set()
                connection, _ = server.accept()
                with connection, server:
                    reader = connection.makefile("rb")
                    frames.append(json.loads(reader.readline()))
                    connection.sendall(
                        json.dumps(
                            {
                                "kind": "answer",
                                "answers": {"question_0_custom": "Use safe."},
                            }
                        ).encode()
                        + b"\n"
                    )
                    frames.append(json.loads(reader.readline()))
                    connection.sendall(
                        json.dumps(
                            {
                                "kind": "recorded",
                                "session_id": "session-1",
                                "tool_call_id": "tool-1",
                            }
                        ).encode()
                        + b"\n"
                    )
            except (OSError, ValueError, UnicodeError) as error:
                server_error.append(error)
                ready.set()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(timeout=5))
        script = """
const client = await import(process.argv[1]);
const request = JSON.parse(process.argv[3]);
const channel = new client.ScopedQuestionClient(process.argv[2]);
try {
  const result = await channel.request(request, {observedToolCall: true});
  process.stdout.write(JSON.stringify(result));
} finally {
  await channel.close();
}
"""
        result = subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                script,
                str(QUESTION_CLIENT),
                str(self.socket_path),
                json.dumps(form_request(), ensure_ascii=False),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        thread.join(timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"action": "accept", "content": {"question_0_custom": "Use safe."}},
        )
        self.assertEqual(server_error, [])
        self.assertEqual(frames[0]["kind"], "question")
        self.assertEqual(frames[0]["session_id"], "session-1")
        self.assertEqual(frames[0]["tool_call_id"], "tool-1")
        self.assertEqual(frames[0]["questions"][0]["field"], "question_0_custom")
        self.assertEqual(
            frames[1],
            {"kind": "received", "session_id": "session-1", "tool_call_id": "tool-1"},
        )

    def test_answer_key_mismatch_and_disconnect_fail_closed(self) -> None:
        for mode in (
            "mismatch",
            "disconnect",
            "recorded-mismatch",
            "recorded-disconnect",
        ):
            with self.subTest(mode=mode):
                if self.socket_path.exists():
                    self.socket_path.unlink()
                server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                server.bind(str(self.socket_path))
                server.listen(1)
                self.socket_path.chmod(0o600)

                def serve(server=server, mode=mode) -> None:
                    try:
                        connection, _ = server.accept()
                        with connection:
                            reader = connection.makefile("rb")
                            reader.readline()
                            if mode == "mismatch":
                                connection.sendall(
                                    b'{"kind":"answer","answers":{"wrong":"value"}}\n'
                                )
                            elif mode.startswith("recorded-"):
                                connection.sendall(
                                    b'{"kind":"answer","answers":{"question_0_custom":"Use safe."}}\n'
                                )
                                reader.readline()
                                if mode == "recorded-mismatch":
                                    connection.sendall(
                                        b'{"kind":"recorded","session_id":"wrong","tool_call_id":"tool-1"}\n'
                                    )
                    except OSError:
                        pass

                thread = threading.Thread(target=serve, daemon=True)
                thread.start()
                script = """
const client = await import(process.argv[1]);
const request = JSON.parse(process.argv[3]);
const channel = new client.ScopedQuestionClient(process.argv[2]);
try {
  await channel.request(request, {observedToolCall: true});
  process.exitCode = 0;
} catch (error) {
  process.stderr.write(String(error?.message ?? error));
  process.exitCode = 1;
} finally {
  await channel.close();
}
"""
                result = subprocess.run(
                    [
                        NODE,
                        "--input-type=module",
                        "-e",
                        script,
                        str(QUESTION_CLIENT),
                        str(self.socket_path),
                        json.dumps(form_request(), ensure_ascii=False),
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                thread.join(timeout=5)
                server.close()
                if self.socket_path.exists():
                    self.socket_path.unlink()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr)


if __name__ == "__main__":
    unittest.main()
