import assert from "node:assert/strict";
import {
  isSuccessfulTurnEnd,
  makePostToolPayload,
  postToolTargets,
  proseContextFromOutput,
} from "../dotfiles/.agent/apps/deepseek/logic.mjs";
import test from "node:test";

test("native event adapters preserve DSH file arguments and success state", () => {
  const exec = {
    name: "edit",
    arguments: {
      file_path: "notes.md",
      old_string: "before",
      new_string: "after",
      replace_all: false,
    },
    agent: { session: { header: { cwd: "/workspace" } } },
  };

  assert.deepEqual(makePostToolPayload(exec), {
    hook_event_name: "PostToolUse",
    cwd: "/workspace",
    tool_name: "edit",
    tool_input: {
      file_path: "notes.md",
      old_string: "before",
      new_string: "after",
      replace_all: false,
    },
  });
  assert.deepEqual(postToolTargets(exec, { isError: false }), {
    jupytext: false,
    prose: true,
  });
  assert.equal(postToolTargets(exec, { isError: true }), undefined);
  assert.equal(postToolTargets({ ...exec, name: "bash" }, { isError: false }), undefined);
});

test("jupytext runs only for successful Python file writes and edits", () => {
  const exec = { name: "write", arguments: { file_path: "analysis.py", content: "x = 1\n" } };
  assert.deepEqual(postToolTargets(exec, { isError: false }), {
    jupytext: true,
    prose: false,
  });
  assert.equal(postToolTargets(exec, { isError: true }), undefined);
});

test("completion notification accepts only a completed turn end", () => {
  assert.equal(isSuccessfulTurnEnd({
    type: "turn/end",
    data: { turn: 1, reason: { kind: "completed" } },
  }), true);
  for (const reason of ["error", "aborted", "blocked", "max-tokens"]) {
    assert.equal(isSuccessfulTurnEnd({
      type: "turn/end",
      data: { turn: 1, reason: { kind: reason } },
    }), false, `${reason} must not signal successful completion`);
  }
  assert.equal(isSuccessfulTurnEnd({ type: "step/end", data: {} }), false);
});

test("prose hook output must be valid JSON with nonempty additionalContext", () => {
  assert.equal(proseContextFromOutput({ exitCode: 0, stdout: "not-json" }), undefined);
  assert.equal(proseContextFromOutput({ exitCode: 1, stdout: '{"additionalContext":"finding"}' }), undefined);
  assert.equal(proseContextFromOutput({ exitCode: 0, stdout: '{"additionalContext":"  "}' }), undefined);
  assert.equal(proseContextFromOutput({
    exitCode: 0,
    stdout: '{"additionalContext":"finding"}',
  }), "finding");
});
