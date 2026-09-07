import fs from "node:fs";
import readline from "node:readline";

const logPath = new URL("./wire.jsonl", import.meta.url);
const { zInitializeRequest } = await import(new URL("./schema/zod.gen.js", import.meta.resolve("@agentclientprotocol/sdk")));

export class SettingsManager {
  async initialize() {}
  getSettings() { return { permissions: { defaultMode: "default" } }; }
  dispose() {}
}

export function runAcp() {
  let resolveClosed;
  const closed = new Promise((resolve) => { resolveClosed = resolve; });
  let options;
  let model = "unset";
  let effort = "unset";
  let promptId;
  const sessionId = "native-question-fixture-session";
  const toolCallId = "native-question-fixture-tool";
  const config = () => [
    { id: "model", type: "select", name: "Model", currentValue: model, options: [] },
    { id: "effort", type: "select", name: "Effort", currentValue: effort, options: [] },
  ];
  const send = (value) => process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", ...value })}\n`);
  const agent = {
    async newSession(params) {
      options = params._meta.claudeCode.options;
      model = options.model;
      return { sessionId, configOptions: config() };
    },
    async loadSession() {},
    async resumeSession() {},
    async unstable_forkSession() {},
    async setSessionMode() {},
    async setSessionConfigOption(params) {
      if (params.configId === "model") model = params.value;
      else if (params.configId === "effort") effort = params.value;
      return { configOptions: config() };
    },
    async dispose() {},
  };
  for (const name of [
    "authenticate", "logout", "unstable_listProviders", "unstable_setProvider",
    "unstable_disableProvider", "listSessions", "deleteSession", "steer", "goal",
  ]) agent[name] = async () => { throw new Error("fixture has no provider or authentication"); };
  const input = readline.createInterface({ input: process.stdin });
  input.once("close", () => resolveClosed());
  input.on("line", async (line) => {
    const message = JSON.parse(line);
    fs.appendFileSync(logPath, `${JSON.stringify(message)}\n`);
    const params = message.params;
    try {
      if (message.method === "initialize") {
        const normalized = zInitializeRequest.parse(params);
        const form = normalized.clientCapabilities.elicitation?.form;
        if (typeof form !== "object" || form === null || Array.isArray(form)) throw new Error("form capability missing after selected SDK parsing");
        fs.appendFileSync(logPath, `${JSON.stringify({ event: "parsed-initialize", elicitation: normalized.clientCapabilities.elicitation })}\n`);
        send({ id: message.id, result: { protocolVersion: 1, agentCapabilities: { loadSession: false }, authMethods: [] } });
      } else if (message.method === "session/new") {
        send({ id: message.id, result: await agent.newSession(params) });
      } else if (message.method === "session/set_config_option") {
        send({ id: message.id, result: await agent.setSessionConfigOption(params) });
      } else if (message.method === "session/prompt") {
        promptId = message.id;
        const rawInput = { questions: [{
          question: "どの資料を根拠にしますか？", header: "根拠",
          options: [{ label: "設定", description: "設定ファイルを確認する" }], multiSelect: false,
        }] };
        const hook = options.hooks.PreToolUse[0].hooks[0];
        const decision = await hook({ hook_event_name: "PreToolUse", tool_name: "AskUserQuestion", tool_input: rawInput });
        if (decision.hookSpecificOutput?.permissionDecision !== "ask") throw new Error("question bypassed its permission callback");
        send({ method: "session/update", params: { sessionId, update: {
          sessionUpdate: "tool_call", toolCallId, status: "pending", title: "AskUserQuestion", kind: "other",
          rawInput, _meta: { claudeCode: { toolName: "AskUserQuestion" } },
        } } });
        send({ id: "question-request", method: "elicitation/create", params: {
          mode: "form", sessionId, toolCallId, message: rawInput.questions[0].question,
          requestedSchema: { type: "object", properties: {
            question_0: { type: "string", title: "根拠", oneOf: [{ const: "設定", title: "設定", description: "設定ファイルを確認する" }] },
            question_0_custom: { type: "string", title: "Other", _meta: { _askUserQuestionCustomAnswer: { questionId: "question_0", isCustomAnswer: true } } },
          } },
        } });
      } else if (message.id === "question-request") {
        if (message.result?.action !== "accept") {
          if (promptId !== undefined) send({ id: promptId, result: { stopReason: "cancelled" } });
          promptId = undefined;
          return;
        }
        send({ method: "session/update", params: { sessionId, update: {
          sessionUpdate: "agent_message_chunk", messageId: "answer-result",
          content: { type: "text", text: `確認した回答: ${message.result.content.question_0_custom}` },
        } } });
        send({ id: promptId, result: { stopReason: "end_turn" } });
        promptId = undefined;
      } else if (message.method === "session/close") {
        send({ id: message.id, result: {} });
      } else if (message.method === "session/cancel") {
        if (promptId !== undefined) send({ id: promptId, result: { stopReason: "cancelled" } });
        promptId = undefined;
      } else if (message.id !== undefined) {
        throw new Error(`unsupported fixture request: ${message.method}`);
      }
    } catch (error) {
      send({ id: message.id, error: { code: -32000, message: String(error.message) } });
    }
  });
  return { agent, connection: { closed } };
}
