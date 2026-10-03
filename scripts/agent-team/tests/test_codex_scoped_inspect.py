from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSPECT = ROOT / "agent_team" / "codex_scoped_inspect.mjs"
BRIDGE = ROOT / "agent_team" / "codex_scoped_bridge.mjs"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for scoped Codex inspection")
class CodexScopedInspectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="agent-team-codex-inspect-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config = {
            "config": {
                "model": "gpt-test",
                "model_reasoning_effort": "medium",
                "model_provider": "openai",
            },
            "origins": {},
            "layers": [],
        }
        self.snapshot = hashlib.sha256(
            json.dumps(self.config, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def run_probe(
        self,
        fake_child: str,
        *,
        options: dict | None = None,
        omit: str | None = None,
    ) -> dict:
        assert NODE is not None
        fake = self.root / "fake-child.mjs"
        fake.write_text(fake_child, encoding="utf-8")
        selected = {
            "child": "child",
            "workspace": str(self.workspace),
            "model": "gpt-test",
            "effort": "medium",
        }
        if options is not None:
            selected.update(options)
        if omit is not None:
            selected.pop(omit, None)
        driver = r"""
            import {spawn} from "node:child_process";
            const {inspectConfig} = await import(process.argv[2]);
            const child = spawn(process.execPath, [process.argv[3]], {stdio:"pipe"});
            try {
              const value = await inspectConfig({
                child,
                workspace: process.argv[4],
                model: process.argv[5],
                effort: process.argv[6],
              });
              process.stdout.write(JSON.stringify({ok:true, value}));
            } catch (error) {
              process.stdout.write(JSON.stringify({ok:false, error:String(error?.message ?? error)}));
            }
        """
        result = subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                driver,
                "inspect-probe",
                str(INSPECT),
                str(fake),
                selected.get("workspace", ""),
                selected.get("model", ""),
                selected.get("effort", ""),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
            cwd=self.root,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def fake_server(
        self,
        *,
        config: dict | None = None,
        extra: str = "",
        exit_code: int = 0,
    ) -> str:
        encoded = json.dumps(self.config if config is None else config)
        return f"""
            import {{createInterface}} from "node:readline";
            const rl = createInterface({{input: process.stdin}});
            const config = {encoded};
            const send = (value) => process.stdout.write(`${{JSON.stringify(value)}}\\n`);
            rl.on("line", (line) => {{
              const message = JSON.parse(line);
              {extra}
              if (message.method === "initialize") return send({{id:message.id,result:{{}}}});
              if (message.method === "configRequirements/read") return send({{id:message.id,result:{{requirements:null}}}});
              if (message.method === "config/read") return process.stdout.write(`${{JSON.stringify({{id:message.id,result:config}})}}\\n`, () => process.exit({exit_code}));
            }});
            process.stdin.resume();
        """

    def test_returns_only_snapshot_after_pinned_three_request_sequence(self) -> None:
        fake = self.fake_server(
            extra='if (Object.prototype.hasOwnProperty.call(message, "jsonrpc")) process.exit(17);'
        )
        result = self.run_probe(fake)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["value"], {"configSnapshot": self.snapshot})

    def test_invalid_options_stop_the_already_spawned_child(self) -> None:
        fake = "process.stdin.resume(); setInterval(() => {}, 1000);"
        result = self.run_probe(fake, omit="effort")
        self.assertFalse(result["ok"], result)
        self.assertIn("invalid options", result["error"])

    def test_nonzero_child_exit_cannot_be_reported_as_success(self) -> None:
        result = self.run_probe(self.fake_server(exit_code=7))
        self.assertFalse(result["ok"], result)
        self.assertIn("cleanup", result["error"])

    def test_non_null_requirements_are_rejected_without_disclosure(self) -> None:
        fake = self.fake_server(
            extra='if (message.method === "configRequirements/read") return send({id:message.id,result:{requirements:{secret:"auth-secret"}}});'
        )
        result = self.run_probe(fake)
        self.assertFalse(result["ok"], result)
        self.assertNotIn("auth-secret", result["error"])

    def test_config_path_and_selected_bindings_are_validated(self) -> None:
        expected_workspace = json.dumps(str(self.workspace))
        fake = self.fake_server(
            extra=f'if (message.method === "config/read" && (message.params.cwd !== {expected_workspace} || message.params.includeLayers !== true)) process.exit(17);'
        )
        result = self.run_probe(fake)
        self.assertTrue(result["ok"], result)
        for field, value in (
            ("model", "other-model"),
            ("model_reasoning_effort", "high"),
            ("model_provider", "other-provider"),
        ):
            with self.subTest(field=field):
                changed = {
                    **self.config,
                    "config": {**self.config["config"], field: value},
                }
                result = self.run_probe(self.fake_server(config=changed))
                self.assertFalse(result["ok"], result)
                self.assertNotIn(value, result["error"])

    def test_jsonrpc_marker_and_unknown_server_request_fail_closed(self) -> None:
        for mode in ("marker", "request"):
            with self.subTest(mode=mode):
                if mode == "marker":
                    fake = self.fake_server(
                        extra='if (message.method === "initialize") return process.stdout.write(JSON.stringify({jsonrpc:"2.0",id:message.id,result:{}})+"\\n");'
                    )
                else:
                    fake = self.fake_server(
                        extra='if (message.method === "initialize") { send({id:"server-request",method:"unknown/server",params:{secret:"auth-secret"}}); return send({id:message.id,result:{}}); }'
                    )
                result = self.run_probe(fake)
                self.assertFalse(result["ok"], result)
                self.assertNotIn("auth-secret", result["error"])

    def test_malformed_frame_and_eof_fail_closed(self) -> None:
        cases = {
            "malformed": 'process.stdout.write("not-json\\n");',
            "eof": "process.stdout.end(() => process.exit(0));",
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                result = self.run_probe(body)
                self.assertFalse(result["ok"], result)

    def test_helper_has_no_process_spawn_or_network_client(self) -> None:
        source = INSPECT.read_text(encoding="utf-8")
        self.assertNotIn("child_process", source)
        self.assertNotIn("spawn(", source)
        self.assertNotIn("node:net", source)
        self.assertNotIn("node:http", source)
        self.assertIn('"jsonrpc"', source)
        self.assertTrue(BRIDGE.exists())


if __name__ == "__main__":
    unittest.main()
