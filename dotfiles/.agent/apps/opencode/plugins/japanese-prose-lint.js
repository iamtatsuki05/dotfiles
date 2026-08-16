import { spawnSync } from "node:child_process"
import { existsSync } from "node:fs"
import { homedir } from "node:os"
import { extname, isAbsolute, resolve } from "node:path"

const supported = new Set([".md", ".markdown", ".txt"])

function boundedFeedback(stdout) {
  const lines = stdout.trim().split("\n")
  const visible = lines.slice(0, 20)
  if (lines.length > 20) visible.push(`ほか${lines.length - 20}件あります。修正後に lint を再実行してください。`)
  return visible.join("\n")
}

function pathsFrom(input) {
  if (!input || !["write", "edit", "apply_patch"].includes(input.tool)) return []
  const args = input.args ?? {}
  const cwd = input.cwd ?? process.cwd()
  const values = [args.filePath, args.file_path, args.path].filter((value) => typeof value === "string")
  const patch = args.patchText ?? args.patch ?? ""
  for (const match of patch.matchAll(/^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$/gm)) {
    values.push(match[1] ?? match[2])
  }
  return [...new Set(values.map((value) => (isAbsolute(value) ? value : resolve(cwd, value))))]
    .filter((path) => supported.has(extname(path).toLowerCase()) && existsSync(path))
}

export const JapaneseProseLint = async () => ({
  "tool.execute.after": async (input, output) => {
    const paths = pathsFrom(input)
    if (paths.length === 0) return
    const command = resolve(homedir(), ".config/opencode/hooks/japanese_prose_lint.sh")
    const lint = spawnSync(command, ["--check", ...paths], { encoding: "utf8" })
    if (lint.status === 1) {
      output.output = `${output.output ?? ""}\n\n日本語 lint で修正候補が見つかりました。\n${boundedFeedback(lint.stdout)}`
    } else if (lint.status !== 0) {
      output.output = `${output.output ?? ""}\n\n日本語 lint の実行に失敗しました。設定を確認してください。`
    }
  },
})
