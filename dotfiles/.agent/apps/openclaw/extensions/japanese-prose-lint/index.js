import { spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import { homedir } from "node:os"
import { extname, isAbsolute, resolve } from "node:path"

const pendingPaths = new Map()
const supported = new Set([".md", ".markdown", ".txt"])

function callKey(event, context) {
  const toolCallId = event.toolCallId ?? context?.toolCallId
  return toolCallId ? `${context?.sessionKey ?? ""}:${toolCallId}` : undefined
}

function boundedFeedback(stdout) {
  const lines = stdout.trim().split("\n")
  const visible = lines.slice(0, 20)
  if (lines.length > 20) visible.push(`ほか${lines.length - 20}件あります。修正後に lint を再実行してください。`)
  return visible.join("\n")
}

function pathsFrom(event) {
  if (!["write", "edit", "apply_patch"].includes(event.toolName)) return []
  const params = event.params ?? {}
  const cwd = params.cwd ?? process.cwd()
  const values = [params.filePath, params.file_path, params.path, ...(event.derivedPaths ?? [])]
    .filter((value) => typeof value === "string")
  const patch = params.patchText ?? params.patch ?? ""
  for (const match of patch.matchAll(/^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$/gm)) {
    values.push(match[1] ?? match[2])
  }
  return [...new Set(values.map((value) => (isAbsolute(value) ? value : resolve(cwd, value))))]
    .filter((path) => supported.has(extname(path).toLowerCase()))
}

export default {
  id: "japanese-prose-lint",
  name: "Japanese Prose Lint",
  register(api) {
    api.on("before_tool_call", (event, context) => {
      const paths = pathsFrom(event)
      const key = callKey(event, context)
      if (paths.length === 0 || !key) return
      pendingPaths.set(key, paths)
    })
    api.on("tool_result_persist", (event, context) => {
      const key = callKey(event, context)
      if (!key) return
      const paths = pendingPaths.get(key)?.filter((path) => existsSync(path))
      pendingPaths.delete(key)
      if (!paths) return
      const lint = spawnSync(resolve(homedir(), ".openclaw/hooks/japanese_prose_lint.sh"), ["--check", ...paths], {
        encoding: "utf8",
      })
      let feedback
      if (lint.status === 1) {
        feedback = `日本語 lint で修正候補が見つかりました。\n${boundedFeedback(lint.stdout)}`
      } else if (lint.status !== 0) {
        feedback = "日本語 lint の実行に失敗しました。設定を確認してください。"
      }
      if (!feedback) return
      const content = Array.isArray(event.message.content)
        ? [...event.message.content, { type: "text", text: `\n\n${feedback}` }]
        : event.message.content
      return { message: { ...event.message, content } }
    })
  },
}
