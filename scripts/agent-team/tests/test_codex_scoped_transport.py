from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSPORT = ROOT / "agent_team" / "codex_scoped_transport.mjs"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for the Codex scoped transport")
class CodexScopedTransportTest(unittest.TestCase):
    def run_probe(self, script: str, *args: object) -> subprocess.CompletedProcess[str]:
        assert NODE is not None
        return subprocess.run(
            [
                NODE,
                "--input-type=module",
                "-e",
                script,
                str(TRANSPORT),
                *[json.dumps(arg, ensure_ascii=False) for arg in args],
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(
            prefix="agent-team-codex-transport-"
        )
        self.root = Path(self.directory.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "read.txt").write_text("hello\n", encoding="utf-8")
        (self.workspace / "src" / "write.txt").write_text("before", encoding="utf-8")
        self.protected = self.root / "protected"
        self.protected.mkdir()
        self.policy = {
            "workspace": str(self.workspace.resolve()),
            "allowed_paths": ["src/"],
            "forbidden_paths": ["src/blocked/"],
            "protected_paths": [str(self.protected.resolve())],
            "permission": "workspace-write",
        }
        self.config = {
            "config": {
                "model": "gpt-test",
                "model_reasoning_effort": "medium",
                "model_provider": "openai",
                "mcp_servers": {},
            },
            "origins": {},
            "layers": [],
        }
        canonical = json.dumps(
            self.config, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        self.snapshot = hashlib.sha256(canonical.encode()).hexdigest()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def run_scenario(self, fake_child: str, driver: str) -> dict[str, object]:
        fake_path = self.root / "fake-child.mjs"
        fake_path.write_text(fake_child, encoding="utf-8")
        result = self.run_probe(driver, str(fake_path), self.policy, self.snapshot)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "", result.stderr)
        return json.loads(result.stdout)

    def test_transport_exports_run_transport(self) -> None:
        result = self.run_probe(
            """
            const transport = await import(process.argv[1]);
            process.stdout.write(JSON.stringify({runTransport: typeof transport.runTransport}));
            """
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"runTransport": "function"})

    def test_success_routes_notifications_and_dynamic_tool_request_over_real_pipes(
        self,
    ) -> None:
        fake_child = r"""
            import {createInterface} from "node:readline";
            const rl = createInterface({input: process.stdin});
            const send = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
            const pending = new Map();
            rl.on("line", async (line) => {
              const message = JSON.parse(line);
              if (message.id === "server-tool") {
                send({method:"tool-result",params:{result:message.result,error:message.error}});
                send({method:"turn/completed",params:{threadId:"thread-1",turn:{id:"turn-1",status:"completed"}}});
                return;
              }
              if (message.method === "initialized") {
                send({method:"initialized-seen",params:{}});
                return;
              }
              if (!Object.prototype.hasOwnProperty.call(message, "id")) return;
              if (message.method === "initialize") return send({ id:message.id, result:{serverInfo:{name:"fake"}}});
              if (message.method === "configRequirements/read") return send({ id:message.id, result:{requirements:null}});
              if (message.method === "config/read") return send({ id:message.id, result:JSON.parse(process.argv[2])});
              if (message.method === "thread/start") return send({ id:message.id, result:{thread:{id:"thread-1",cwd:message.params.cwd,model:message.params.model,ephemeral:true,reasoningEffort:"medium"},cwd:message.params.cwd,model:message.params.model,modelProvider:"openai",reasoningEffort:"medium",approvalPolicy:"never",approvalsReviewer:"user",activePermissionProfile:{id:":read-only",extends:null}}});
              if (message.method === "turn/start") {
                send({method:"turn/started",params:{threadId:"thread-1",turn:{id:"turn-1",status:"inProgress"}}});
                send({id:"server-tool",method:"item/tool/call",params:{threadId:"thread-1",turnId:"turn-1",callId:"call-1",tool:"write_text",namespace:null,arguments:{file_path:process.argv[3]+"/src/write.txt",content:"changed"}}});
                setTimeout(() => send({id:message.id,result:{turn:{id:"turn-1",status:"inProgress"}}}), 10);
                return;
              }
              if (message.method === "turn/interrupt") return send({id:message.id,result:{}});
              if (message.method === "thread/unsubscribe") return send({id:message.id,result:{}});
              if (pending.has(message.id)) return pending.delete(message.id);
              send({id:message.id,result:{}});
            });
            rl.on("close", () => process.exit(0));
            process.stdin.resume();
        """
        driver = r"""
            import {spawn} from "node:child_process";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const fakeChild = JSON.parse(process.argv[2]);
            const snapshot = JSON.parse(process.argv[4]);
            const child = spawn(process.execPath, [fakeChild, JSON.stringify({config:{model:"gpt-test",model_reasoning_effort:"medium",model_provider:"openai",mcp_servers:{}},origins:{},layers:[]}), policy.workspace], {stdio:"pipe"});
            const input = new PassThrough();
            const output = new PassThrough();
            const frames = [];
            let buffer = "";
            output.on("data", (chunk) => {
              buffer += chunk.toString();
              let index;
              while ((index = buffer.indexOf("\n")) >= 0) {
                const line = buffer.slice(0,index); buffer = buffer.slice(index+1);
                if (line) frames.push(JSON.parse(line));
              }
            });
            const waitFor = async (predicate) => {
              const deadline = Date.now() + 5000;
              while (!predicate()) { if (Date.now() > deadline) throw new Error("timeout "+JSON.stringify(frames)); await new Promise((resolve) => setTimeout(resolve, 5)); }
            };
            const send = (value) => input.write(`${JSON.stringify(value)}\n`);
            const resultPromise = runTransport({child,input,output,bridgeOptions:{policy,model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:snapshot}});
            send({id:1,method:"initialize",params:{clientInfo:{name:"test"}}});
            await waitFor(() => frames.some((frame) => frame.id === 1));
            send({method:"initialized",params:{}});
            await waitFor(() => frames.some((frame) => frame.method === "initialized-seen"));
            send({id:2,method:"thread/start",params:{cwd:policy.workspace,modelProvider:null,config:{projects:{[policy.workspace]:{trust_level:"trusted"}}}}});
            await waitFor(() => frames.some((frame) => frame.id === 2));
            send({id:3,method:"turn/start",params:{threadId:"thread-1",input:[{type:"text",text:"hello",text_elements:[]}]}});
            await waitFor(() => frames.some((frame) => frame.id === 3));
            await waitFor(() => frames.some((frame) => frame.method === "tool-result"));
            input.end();
            const result = await resultPromise;
            process.stdout.write(JSON.stringify({result,frames}));
        """
        result = self.run_scenario(fake_child, driver)
        self.assertTrue(result["result"]["ok"], result)
        self.assertEqual(result["result"]["childExitCode"], 0)
        self.assertEqual(
            (self.workspace / "src" / "write.txt").read_text(encoding="utf-8"),
            "changed",
            result,
        )
        self.assertTrue(all("jsonrpc" not in frame for frame in result["frames"]))
        self.assertIn(
            {"id": 1, "result": {"serverInfo": {"name": "fake"}}},
            result["frames"],
        )
        self.assertTrue(
            any(
                frame.get("id") == 2 and "result" in frame for frame in result["frames"]
            )
        )
        self.assertIn(
            {
                "method": "turn/started",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "inProgress"},
                },
            },
            result["frames"],
        )
        self.assertIn(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread-1",
                    "turn": {"id": "turn-1", "status": "completed"},
                },
            },
            result["frames"],
        )

    def test_interrupt_request_is_dispatched_while_turn_is_still_running(self) -> None:
        fake_child = r"""
            import {createInterface} from "node:readline";
            const rl = createInterface({input: process.stdin});
            const send = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
            let timer;
            rl.on("line", (line) => {
              const message = JSON.parse(line);
              if (!Object.prototype.hasOwnProperty.call(message, "id")) return;
              if (message.method === "initialize") return send({id:message.id,result:{}});
              if (message.method === "configRequirements/read") return send({id:message.id,result:{requirements:null}});
              if (message.method === "config/read") return send({id:message.id,result:JSON.parse(process.argv[2])});
              if (message.method === "thread/start") return send({id:message.id,result:{thread:{id:"thread-1",cwd:message.params.cwd,model:message.params.model,ephemeral:true,reasoningEffort:"medium"},cwd:message.params.cwd,model:message.params.model,modelProvider:"openai",reasoningEffort:"medium",approvalPolicy:"never",approvalsReviewer:"user",activePermissionProfile:{id:":read-only",extends:null}}});
              if (message.method === "turn/start") {
                send({method:"turn/started",params:{threadId:"thread-1",turn:{id:"turn-1",status:"inProgress"}}});
                send({id:message.id,result:{turn:{id:"turn-1",status:"inProgress"}}});
                timer = setTimeout(() => send({method:"turn/completed",params:{threadId:"thread-1",turn:{id:"turn-1",status:"completed"}}}), 300);
                return;
              }
              if (message.method === "turn/interrupt") return send({id:message.id,result:{}});
              if (message.method === "thread/unsubscribe") return send({id:message.id,result:{}});
              send({id:message.id,result:{}});
            });
            rl.on("close", () => { clearTimeout(timer); process.exit(0); });
            process.stdin.resume();
        """
        driver = r"""
            import {spawn} from "node:child_process";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const child = spawn(process.execPath, [JSON.parse(process.argv[2]), JSON.stringify({config:{model:"gpt-test",model_reasoning_effort:"medium",model_provider:"openai",mcp_servers:{}},origins:{},layers:[]}), policy.workspace], {stdio:"pipe"});
            const input = new PassThrough(); const output = new PassThrough(); const frames = []; let buffer = "";
            output.on("data", (chunk) => { buffer += chunk.toString(); let index; while ((index = buffer.indexOf("\n")) >= 0) { const line = buffer.slice(0,index); buffer = buffer.slice(index+1); if (line) frames.push(JSON.parse(line)); } });
            const waitFor = async (predicate) => { const deadline = Date.now()+5000; while (!predicate()) { if (Date.now()>deadline) throw new Error("timeout "+JSON.stringify(frames)); await new Promise((resolve)=>setTimeout(resolve,5)); } };
            const send = (value) => input.write(`${JSON.stringify(value)}\n`);
            const resultPromise = runTransport({child,input,output,bridgeOptions:{policy,model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:JSON.parse(process.argv[4])}});
            send({id:1,method:"initialize",params:{clientInfo:{name:"test"}}}); await waitFor(()=>frames.some((frame)=>frame.id===1));
            send({method:"initialized",params:{}});
            send({id:2,method:"thread/start",params:{cwd:policy.workspace,modelProvider:null,config:{projects:{[policy.workspace]:{trust_level:"trusted"}}}}}); await waitFor(()=>frames.some((frame)=>frame.id===2));
            send({id:3,method:"turn/start",params:{threadId:"thread-1",input:[{type:"text",text:"hello",text_elements:[]}]}}); await waitFor(()=>frames.some((frame)=>frame.id===3));
            send({id:4,method:"turn/interrupt",params:{threadId:"thread-1",turnId:"turn-1"}}); await waitFor(()=>frames.some((frame)=>frame.id===4));
            input.end(); const result = await resultPromise; process.stdout.write(JSON.stringify({result,frames}));
        """
        result = self.run_scenario(fake_child, driver)
        self.assertTrue(
            any(
                frame.get("id") == 4 and frame.get("result") == {}
                for frame in result["frames"]
            ),
            result,
        )
        self.assertEqual(result["result"]["reason"], "upstream EOF")

    def test_child_server_request_error_does_not_echo_request_params(self) -> None:
        fake_child = r"""
            import {createInterface} from "node:readline";
            const rl = createInterface({input: process.stdin});
            const send = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
            rl.on("line", (line) => {
              const message = JSON.parse(line);
              if (message.id === "bad-server-request") {
                send({method:"server/error-seen",params:{error:message.error}});
                return;
              }
              if (!Object.prototype.hasOwnProperty.call(message, "id")) return;
              if (message.method === "initialize") {
                send({id:"bad-server-request",method:"unsupported/server/request",params:{secret:"must-not-echo"}});
                return send({id:message.id,result:{}});
              }
              send({id:message.id,result:{}});
            });
            rl.on("close", () => process.exit(0));
            process.stdin.resume();
        """
        driver = r"""
            import {spawn} from "node:child_process";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const child = spawn(process.execPath, [JSON.parse(process.argv[2])], {stdio:"pipe"});
            const input = new PassThrough(); const output = new PassThrough(); const frames = []; let buffer = "";
            output.on("data", (chunk) => { buffer += chunk.toString(); let index; while ((index = buffer.indexOf("\n")) >= 0) { const line = buffer.slice(0,index); buffer = buffer.slice(index+1); if (line) frames.push(JSON.parse(line)); } });
            const waitFor = async (predicate) => { const deadline = Date.now()+5000; while (!predicate()) { if (Date.now()>deadline) throw new Error("timeout "+JSON.stringify(frames)); await new Promise((resolve)=>setTimeout(resolve,5)); } };
            const resultPromise = runTransport({child,input,output,bridgeOptions:{policy,model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:JSON.parse(process.argv[4])}});
            input.write(`${JSON.stringify({id:1,method:"initialize",params:{clientInfo:{name:"test"}}})}\n`);
            await waitFor(()=>frames.some((frame)=>frame.id===1)); await waitFor(()=>frames.some((frame)=>frame.method==="server/error-seen")); input.end(); const result = await resultPromise; process.stdout.write(JSON.stringify({result,frames}));
        """
        result = self.run_scenario(fake_child, driver)
        seen = next(
            (
                frame
                for frame in result["frames"]
                if frame.get("method") == "server/error-seen"
            ),
            None,
        )
        self.assertIsNotNone(seen, result)
        self.assertEqual(seen["params"]["error"]["message"], "server request rejected")
        self.assertNotIn("must-not-echo", json.dumps(result))

    def test_malformed_parent_json_fails_closed_with_bounded_error(self) -> None:
        fake_child = r"""
            import {createInterface} from "node:readline";
            const rl = createInterface({input: process.stdin});
            rl.on("close", () => process.exit(0));
            process.stdin.resume();
        """
        driver = r"""
            import {spawn} from "node:child_process";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const child = spawn(process.execPath, [JSON.parse(process.argv[2])], {stdio:"pipe"});
            const input = new PassThrough(); const output = new PassThrough(); const chunks = []; output.on("data", (chunk)=>chunks.push(chunk));
            const resultPromise = runTransport({child,input,output,bridgeOptions:{policy,model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:JSON.parse(process.argv[4])}});
            input.write('{"id":1,"method":"initialize","secret":"must-not-echo"}\n');
            const result = await resultPromise; process.stdout.write(JSON.stringify({result,output:Buffer.concat(chunks).toString("utf8")}));
        """
        result = self.run_scenario(fake_child, driver)
        self.assertFalse(result["result"]["ok"])
        self.assertIn("invalid request", result["output"])
        self.assertNotIn("must-not-echo", result["output"])

    def test_jsonrpc_marker_is_rejected_on_fixed_codex_wire(self) -> None:
        fake_child = r"""
            import {createInterface} from "node:readline";
            const rl = createInterface({input: process.stdin});
            rl.on("close", () => process.exit(0));
            process.stdin.resume();
        """
        driver = r"""
            import {spawn} from "node:child_process";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const child = spawn(process.execPath, [JSON.parse(process.argv[2])], {stdio:"pipe"});
            const input = new PassThrough(); const output = new PassThrough(); const chunks = []; output.on("data", (chunk)=>chunks.push(chunk));
            const resultPromise = runTransport({child,input,output,bridgeOptions:{policy,model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:JSON.parse(process.argv[4])}});
            input.write('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"test"}}}\n');
            const result = await resultPromise; process.stdout.write(JSON.stringify({result,output:Buffer.concat(chunks).toString("utf8")}));
        """
        result = self.run_scenario(fake_child, driver)
        self.assertFalse(result["result"]["ok"])
        self.assertIn("invalid request", result["output"])

    def test_jsonrpc_marker_from_child_is_rejected(self) -> None:
        fake_child = r"""
            import {createInterface} from "node:readline";
            const rl = createInterface({input: process.stdin});
            const send = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
            rl.on("line", (line) => {
              const message = JSON.parse(line);
              if (message.method === "initialize") send({jsonrpc:"2.0",id:message.id,result:{}});
            });
            rl.on("close", () => process.exit(0));
            process.stdin.resume();
        """
        driver = r"""
            import {spawn} from "node:child_process";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const child = spawn(process.execPath, [JSON.parse(process.argv[2])], {stdio:"pipe"});
            const input = new PassThrough(); const output = new PassThrough();
            const resultPromise = runTransport({child,input,output,bridgeOptions:{policy,model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:JSON.parse(process.argv[4])}});
            input.write(`${JSON.stringify({id:1,method:"initialize",params:{clientInfo:{name:"test"}}})}\n`);
            const result = await resultPromise; process.stdout.write(JSON.stringify({result}));
        """
        result = self.run_scenario(fake_child, driver)
        self.assertFalse(result["result"]["ok"])
        self.assertIn("invalid child JSON-RPC message", result["result"]["reason"])

    def test_duplicate_parent_request_id_fails_closed(self) -> None:
        fake_child = r"""
            import {createInterface} from "node:readline";
            const rl = createInterface({input: process.stdin});
            const send = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
            rl.on("line", (line) => {
              const message = JSON.parse(line);
              if (message.method === "initialize") setTimeout(() => send({id:message.id,result:{}}), 200);
            });
            rl.on("close", () => process.exit(0));
            process.stdin.resume();
        """
        driver = r"""
            import {spawn} from "node:child_process";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const child = spawn(process.execPath, [JSON.parse(process.argv[2])], {stdio:"pipe"});
            const input = new PassThrough(); const output = new PassThrough(); const chunks = []; output.on("data", (chunk)=>chunks.push(chunk));
            const resultPromise = runTransport({child,input,output,bridgeOptions:{policy,model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:JSON.parse(process.argv[4])}});
            const request = JSON.stringify({id:1,method:"initialize",params:{clientInfo:{name:"test"}}});
            input.write(`${request}\n${request}\n`);
            const result = await resultPromise; process.stdout.write(JSON.stringify({result,output:Buffer.concat(chunks).toString("utf8")}));
        """
        result = self.run_scenario(fake_child, driver)
        self.assertFalse(result["result"]["ok"])
        self.assertEqual(result["result"]["reason"], "duplicate parent request id")
        self.assertIn("duplicate request id", result["output"])

    def test_upstream_eof_terminates_a_hanging_child_with_bounded_exact_kill(
        self,
    ) -> None:
        fake_child = r"""
            process.stdin.resume();
            setInterval(() => {}, 1000);
        """
        driver = r"""
            import {spawn} from "node:child_process";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const child = spawn(process.execPath, [JSON.parse(process.argv[2])], {stdio:"pipe"});
            const input = new PassThrough(); const output = new PassThrough();
            const resultPromise = runTransport({child,input,output,bridgeOptions:{policy,model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:JSON.parse(process.argv[4])}});
            input.end(); const result = await resultPromise; process.stdout.write(JSON.stringify({result,childCode:child.exitCode,childSignal:child.signalCode}));
        """
        result = self.run_scenario(fake_child, driver)
        self.assertEqual(result["result"]["reason"], "upstream EOF")
        self.assertIn(result["result"]["childSignal"], ["SIGTERM", "SIGKILL"])
        self.assertIsNotNone(result["childSignal"])

    def test_sigterm_uses_transport_cleanup_before_returning_failure(self) -> None:
        fake_child = r"""
            process.stdin.resume();
            setInterval(() => {}, 1000);
        """
        driver = r"""
            import {spawn} from "node:child_process";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]);
            const policy = JSON.parse(process.argv[3]);
            const child = spawn(process.execPath, [JSON.parse(process.argv[2])], {stdio:"pipe"});
            const input = new PassThrough(); const output = new PassThrough();
            const resultPromise = runTransport({child,input,output,bridgeOptions:{policy,model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:JSON.parse(process.argv[4])}});
            setTimeout(() => process.kill(process.pid, "SIGTERM"), 20);
            const result = await resultPromise; process.stdout.write(JSON.stringify({result,childCode:child.exitCode,childSignal:child.signalCode}));
        """
        result = self.run_scenario(fake_child, driver)
        self.assertEqual(result["result"]["reason"], "received SIGTERM")
        self.assertIsNotNone(result["result"]["childSignal"])
        self.assertEqual(result["result"]["exitCode"], 1)

    def test_setup_failure_closes_and_stops_the_supplied_child(self) -> None:
        driver = r"""
            import {EventEmitter} from "node:events";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]);
            class FakeChild extends EventEmitter {
              constructor() { super(); this.stdin = new PassThrough(); this.stdout = {}; this.stderr = null; this.exitCode = null; this.signalCode = null; this.killSignal = null; }
              kill(signal) { this.killSignal = signal; this.signalCode = signal; queueMicrotask(() => this.emit("close", null, signal)); return true; }
            }
            const child = new FakeChild(); const input = new PassThrough(); const output = new PassThrough();
            try { await runTransport({child,input,output,bridgeOptions:{}}); process.stdout.write(JSON.stringify({accepted:true})); }
            catch (error) { process.stdout.write(JSON.stringify({accepted:false,error:error.message,ended:child.stdin.writableEnded,killSignal:child.killSignal})); }
        """
        result = self.run_probe(driver, self.policy, self.snapshot)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertFalse(value["accepted"])
        self.assertTrue(value["ended"])
        self.assertIn(value["killSignal"], ["SIGTERM", "SIGKILL"])

    def test_invalid_child_stdin_listener_is_rejected_before_writer_setup(self) -> None:
        driver = r"""
            import {EventEmitter} from "node:events";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]);
            class FakeChild extends EventEmitter {
              constructor() { super(); this.stdin = new PassThrough(); this.stdin.on = null; this.stdout = new PassThrough(); this.stderr = null; this.exitCode = null; this.signalCode = null; this.killSignal = null; }
              kill(signal) { this.killSignal = signal; this.signalCode = signal; queueMicrotask(() => this.emit("close", null, signal)); return true; }
            }
            const child = new FakeChild();
            const bridgeOptions = {policy:JSON.parse(process.argv[2]),configSnapshot:JSON.parse(process.argv[3]),model:"gpt-test",effort:"medium",instructions:"fixture"};
            try { await runTransport({child,input:new PassThrough(),output:new PassThrough(),bridgeOptions}); process.stdout.write(JSON.stringify({accepted:true})); }
            catch (error) { process.stdout.write(JSON.stringify({accepted:false,error:error.message,ended:child.stdin.writableEnded,killSignal:child.killSignal})); }
        """
        result = self.run_probe(driver, self.policy, self.snapshot)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertFalse(value["accepted"])
        self.assertIn("stdio and lifecycle interfaces", value["error"])
        self.assertTrue(value["ended"])
        self.assertEqual(value["killSignal"], "SIGTERM")

    def test_child_error_during_cleanup_cannot_report_success(self) -> None:
        driver = r"""
            import {EventEmitter} from "node:events";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]);
            class FakeChild extends EventEmitter {
              constructor() { super(); this.stdin = new PassThrough(); this.stdout = new PassThrough(); this.stderr = new PassThrough(); this.exitCode = null; this.signalCode = null; }
              kill(signal) { this.emit("close", null, signal); return true; }
            }
            const child = new FakeChild(); const input = new PassThrough(); const output = new PassThrough();
            const resultPromise = runTransport({child,input,output,bridgeOptions:{policy:JSON.parse(process.argv[2]),model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:process.argv[3]}});
            input.end(); setTimeout(() => child.emit("error", new Error("spawn failure")), 10);
            const result = await resultPromise; process.stdout.write(JSON.stringify({result}));
        """
        result = self.run_probe(driver, self.policy, self.snapshot)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)["result"]
        self.assertFalse(value["ok"])
        self.assertEqual(value["exitCode"], 1)
        self.assertTrue(value["childStopped"])

    def test_concurrent_initialize_is_rejected_before_second_child_rpc(self) -> None:
        fake_child = r"""
            import fs from "node:fs";
            import {createInterface} from "node:readline";
            const rl = createInterface({input:process.stdin}); let count = 0;
            const send = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);
            rl.on("line", (line) => { const message = JSON.parse(line); if (message.method === "initialize") { count += 1; fs.writeFileSync(process.argv[2], String(count)); setTimeout(() => send({id:message.id,result:{}}), 200); } });
            rl.on("close", () => process.exit(0)); process.stdin.resume();
        """
        driver = r"""
            import fs from "node:fs";
            import {spawn} from "node:child_process";
            import {PassThrough} from "node:stream";
            const {runTransport} = await import(process.argv[1]); const fakeChild = JSON.parse(process.argv[2]); const marker = `${fakeChild}.count`; const policy = JSON.parse(process.argv[3]);
            const child = spawn(process.execPath,[fakeChild,marker],{stdio:"pipe"}); const input=new PassThrough(); const output=new PassThrough(); const chunks=[]; output.on("data",(chunk)=>chunks.push(chunk));
            const resultPromise=runTransport({child,input,output,bridgeOptions:{policy,model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:JSON.parse(process.argv[4])}});
            input.write(`${JSON.stringify({id:1,method:"initialize",params:{clientInfo:{name:"test"}}})}\n${JSON.stringify({id:2,method:"initialize",params:{clientInfo:{name:"test"}}})}\n`);
            const result=await resultPromise; const count=fs.existsSync(marker)?fs.readFileSync(marker,"utf8"):"0"; process.stdout.write(JSON.stringify({result,count,output:Buffer.concat(chunks).toString("utf8")}));
        """
        result = self.run_scenario(fake_child, driver)
        self.assertFalse(result["result"]["ok"])
        self.assertEqual(result["result"]["reason"], "duplicate initialize request")
        self.assertEqual(result["count"], "1")
        self.assertIn("initialize may only be requested once", result["output"])

    def test_turn_validation_failure_releases_queued_server_request(self) -> None:
        fake_child = r"""
            import {createInterface} from "node:readline";
            const rl=createInterface({input:process.stdin}); const send=(value)=>process.stdout.write(`${JSON.stringify(value)}\n`);
            rl.on("line",(line)=>{const message=JSON.parse(line);if(message.id==="tool-request"){send({method:"tool-error-seen",params:{error:message.error}});return;}if(message.method==="initialize")return send({id:message.id,result:{}});if(message.method==="configRequirements/read")return send({id:message.id,result:{requirements:null}});if(message.method==="config/read")return send({id:message.id,result:JSON.parse(process.argv[2])});if(message.method==="thread/start")return send({id:message.id,result:{thread:{id:"thread-1",cwd:message.params.cwd,model:message.params.model,ephemeral:true,reasoningEffort:"medium"},cwd:message.params.cwd,model:message.params.model,modelProvider:"openai",reasoningEffort:"medium",approvalPolicy:"never",approvalsReviewer:"user",activePermissionProfile:{id:":read-only",extends:null}}});if(!Object.prototype.hasOwnProperty.call(message,"id"))return;if(message.method==="turn/start"){send({method:"turn/started",params:{threadId:"thread-1",turn:{id:"turn-1",status:"inProgress"}}});send({id:"tool-request",method:"item/tool/call",params:{threadId:"thread-1",turnId:"turn-1",callId:"call-1",tool:"read_text",namespace:null,arguments:{file_path:process.argv[3]+"/src/read.txt",offset:1,limit:1}}});return setTimeout(()=>send({id:message.id,result:{turn:{id:"wrong",status:"inProgress"}}}),10);}if(message.method==="thread/unsubscribe")return send({id:message.id,result:{}});send({id:message.id,result:{}});});
            rl.on("close",()=>process.exit(0)); process.stdin.resume();
        """
        driver = r"""
            import {spawn} from "node:child_process"; import {PassThrough} from "node:stream";
            const {runTransport}=await import(process.argv[1]); const policy=JSON.parse(process.argv[3]); const child=spawn(process.execPath,[JSON.parse(process.argv[2]),JSON.stringify({config:{model:"gpt-test",model_reasoning_effort:"medium",model_provider:"openai",mcp_servers:{}},origins:{},layers:[]}),policy.workspace],{stdio:"pipe"});const input=new PassThrough();const output=new PassThrough();const frames=[];let buffer="";output.on("data",(chunk)=>{buffer+=chunk;let index;while((index=buffer.indexOf("\n"))>=0){const line=buffer.slice(0,index);buffer=buffer.slice(index+1);if(line)frames.push(JSON.parse(line));}});const waitFor=async(predicate)=>{const deadline=Date.now()+5000;while(!predicate()){if(Date.now()>deadline)throw new Error("timeout "+JSON.stringify(frames));await new Promise((resolve)=>setTimeout(resolve,5));}};const send=(value)=>input.write(`${JSON.stringify(value)}\n`);const resultPromise=runTransport({child,input,output,bridgeOptions:{policy,model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:JSON.parse(process.argv[4])}});send({id:1,method:"initialize",params:{clientInfo:{name:"test"}}});await waitFor(()=>frames.some((frame)=>frame.id===1));send({method:"initialized",params:{}});send({id:2,method:"thread/start",params:{cwd:policy.workspace,modelProvider:null,config:{projects:{[policy.workspace]:{trust_level:"trusted"}}}}});await waitFor(()=>frames.some((frame)=>frame.id===2));send({id:3,method:"turn/start",params:{threadId:"thread-1",input:[{type:"text",text:"hello",text_elements:[]}]}});await waitFor(()=>frames.some((frame)=>frame.id===3&&frame.error));await waitFor(()=>frames.some((frame)=>frame.method==="tool-error-seen"));input.end();const result=await resultPromise;process.stdout.write(JSON.stringify({result,frames}));
        """
        result = self.run_scenario(fake_child, driver)
        self.assertTrue(
            any(
                frame.get("id") == 3 and "error" in frame for frame in result["frames"]
            ),
            result,
        )
        self.assertTrue(
            any(frame.get("method") == "tool-error-seen" for frame in result["frames"]),
            result,
        )

    def test_parent_output_close_failure_is_reported(self) -> None:
        fake_child = r"""
            process.stdin.resume(); process.stdin.on("end", () => process.exit(0));
        """
        driver = r"""
            import {spawn} from "node:child_process"; import {PassThrough} from "node:stream";
            const {runTransport}=await import(process.argv[1]);const policy=JSON.parse(process.argv[3]);const child=spawn(process.execPath,[JSON.parse(process.argv[2])],{stdio:"pipe"});const input=new PassThrough();const output={write:()=>true,end:()=>{throw new Error("close failure");}};const resultPromise=runTransport({child,input,output,bridgeOptions:{policy,model:"gpt-test",effort:"medium",instructions:"role",configSnapshot:JSON.parse(process.argv[4])}});input.end();const result=await resultPromise;process.stdout.write(JSON.stringify({result}));
        """
        result = self.run_scenario(fake_child, driver)
        self.assertFalse(result["result"]["ok"])
        self.assertFalse(result["result"]["outputOk"])
        self.assertEqual(result["result"]["exitCode"], 1)


if __name__ == "__main__":
    unittest.main()
