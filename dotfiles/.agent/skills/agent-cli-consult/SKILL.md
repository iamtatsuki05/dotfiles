---
name: agent-cli-consult
description: "Consult Codex CLI or Claude Code CLI when an external opinion is requested. Excludes generic reviews and the current harness's own subagents."
---

# Agent CLI Consult

外部の agent CLI(Codex CLI 0.152.1 / Claude Code 2.1.257 で確認)に読み取り専用でレビュー・分析・調査を依頼する。CLI はユーザー指定に従い、指定がなければ自分と別系統の CLI を選んで理由を一言添える。

## 実行形

prompt は stdin(heredoc)で渡す。長い日本語 prompt を引数にすると Claude Code は `Input must be provided either through stdin or as a prompt argument when using --print` で失敗する。1 行の短い依頼だけ引数でよい。

```bash
# Codex CLI(引数なしなら prompt を stdin から読む)
codex exec --sandbox read-only --cd <project_dir> --output-last-message <out.md> <<'EOF'
<依頼本文>
確認や質問は不要です。具体的な提案・修正案・コード例まで自主的に出力してください。
EOF

# Claude Code CLI(--cd 相当が無いので cd してから実行)
cd <project_dir> && claude -p --allowedTools "Read,Glob,Grep,WebFetch,WebSearch" <<'EOF'
<依頼本文>
確認や質問は不要です。具体的な提案・修正案・コード例まで自主的に出力してください。
EOF
```

- 依頼本文には目的、対象範囲(file や diff)、制約、期待する返答形式(重大度順 + file:line)を書き、末尾の「確認や質問は不要です…」の 1 文を必ず付ける。
- 実装まで任せる場合(ユーザーが実装を依頼し、書き込み対象が明確なときだけ): Codex は `--sandbox workspace-write`、Claude Code は `--permission-mode acceptEdits`。承認・sandbox を全て外す flag は使わない。
- git repo 外のディレクトリでは `codex exec` に `--skip-git-repo-check` が要る。
- 作業ツリーの差分レビューは `codex exec review --uncommitted`(branch 比較は `--base <branch>`)。
- macOS には GNU `timeout` が無い(`gtimeout` も未導入)。`timeout 600 codex ...` と書かず、Bash tool の timeout か background 実行で待つ。

## フラグ対応表

| 項目 | codex exec | claude -p |
|------|-----------|-----------|
| 読み取り専用 | `--sandbox read-only` | `--allowedTools "Read,Glob,Grep,WebFetch,WebSearch"` |
| 書き込み許可 | `--sandbox workspace-write` | `--permission-mode acceptEdits` |
| ディレクトリ | `--cd <dir>`(`-C`) | `cd <dir> &&` |
| モデル | `-m <model>` | `--model <model>` |
| 結果をファイルへ | `--output-last-message <file>` | `--output-format json > <file>` |
| session を残さない | `--ephemeral` | `--no-session-persistence` |
| repo 外で実行 | `--skip-git-repo-check` | 不要 |

## 結果の扱い

- CLI 出力は提案として扱い、採用前に該当ファイル・差分・テスト観点を自分で確認する。根拠のない断定や大きな設計変更はそのまま報告しない。
- 実装を任せた場合は `git diff` で変更を確認し、プロジェクトの test / lint / 型チェックを走らせてから採否を決める。
- WebFetch / WebSearch を許可するのは外部情報が必要な調査だけ。機密コードや未公開情報を検索クエリに含めない。
- 失敗時は終了コード、主要なエラー、再実行に必要な権限・前提を短く報告する。権限拡大や追加インストールはユーザー確認後に行う。
