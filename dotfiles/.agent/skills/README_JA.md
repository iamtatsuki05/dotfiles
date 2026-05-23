# Agent Skills

English version: [README.md](README.md)

このディレクトリは、Codex 互換 agent と Waza eval で使う skill の共通置き場です。
各 agent home へは `dotfiles/.agent/sync.sh` から symlink されます。

## 全体像

```text
skills/
├── .system/                 # OpenAI bundled / system skill
├── <skill>/                 # repo-local skill
├── mattpocock/<skill>/      # vendored external skill group
├── superpowers/<skill>/     # vendored external skill group
├── upstreams.json           # external skill manifest
└── review-prompts/          # upstream review prompt templates
```

`SKILL.md` を持つディレクトリが、agent に読み込ませる 1 skill の単位です。
`references/`、`scripts/`、`agents/`、`assets/` は、その skill だけで使う補助資料です。

## 管理区分

- `repo-local`: この dotfiles で直接管理している skill。
- `system`: Codex / OpenAI 系の bundled skill。基本は upstream 由来で、手編集時は差分の意図を明確に残します。
- `vendored`: 外部 repository から取り込んだ skill。`upstreams.json` に repository、固定 commit、mapping、security review を記録します。
- `local-only`: ローカル導入・生成物として置かれている skill。通常は Git 管理しません。
- `support`: skill ではないが、skill 管理や review に使う補助ファイル。

外部 skill を追加・更新する場合は、手でコピーするのではなく `scripts/agent_skill_upstreams.py` と `upstreams.json` の枠組みを使います。

## ルート直下の repo-local skill

| Skill | 用途 | 備考 |
|---|---|---|
| `agent-job-scheduler` | 複数 agent CLI の長時間・非対話ジョブを CSV 台帳で queue / retry / cancel する。 | 内部アプリ本体、README、pytest を含む大きめの skill。 |
| `alphaxiv-paper-lookup` | arXiv / alphaxiv 論文の要約、比較、実装詳細抽出。 | 論文調査用。 |
| `api-design` | REST API / OpenAPI / versioning / auth / error response の設計・レビュー。 | `eng-practices` と連携。 |
| `auto-debugger` | エラー、stack trace、失敗テストの原因調査と修正。 | 実装前に再現・仮説・検証を重視。 |
| `chronicle` | ユーザー画面と最近の作業履歴を使った文脈補完。 | Chronicle が有効な環境専用。 |
| `ci-cd` | GitHub Actions などの CI/CD 設計・修正・調査。 | workflow YAML とログ調査向け。 |
| `claude-code` | Claude Code CLI に相談する workflow。 | 明示的に Claude Code 相談を求められた時に使う。 |
| `codex` | Codex CLI に相談する workflow。 | 明示的に Codex 相談を求められた時に使う。 |
| `colab-mcp` | Google Colab と local MCP agent の接続設定・トラブルシュート。 | Google 公式 colab-mcp 用。 |
| `database-dev` | DB schema、query、index、migration、性能問題の設計・レビュー。 | SQL / NoSQL 両方を対象。 |
| `eng-practices` | code review、CL/PR 説明、small CL、review comment 作法。 | Google eng-practices を repo 向けに要約した共通 skill。 |
| `go-dev` | Go 実装、テスト、並行処理、interface、module 周り。 | `eng-practices` と連携。 |
| `goal-prompt-builder` | Codex `/goal` 用の長期作業 prompt を作る。 | durable objective と検証条件を固める。 |
| `gws` | Google Calendar / Drive / Gmail / Tasks を `gws` CLI で扱う。 | 外部操作は確認を重視。 |
| `kimi-webbridge` | ユーザーの実ブラウザを local daemon 経由で操作する。 | ログイン済み browser session が必要な作業向け。 |
| `magika` | ファイル種別の判定、拡張子と中身の確認。 | Magika CLI 用。 |
| `markdown-docs` | README、技術文書、校閲、Markdown 整形。 | この README もこの skill の対象。 |
| `markitdown` | PDF / Word / PowerPoint / Excel / HTML などを Markdown に変換。 | MarkItDown CLI 用。 |
| `missing-tools` | 見つからないコマンドを global install なしで解決する。 | project env、mise、comma、Nix fallback を優先。 |
| `pr-code-review` | GitHub PR 差分を bug / risk / test gap 優先でレビュー。 | review finding 形式に寄せる。 |
| `prompt-tuner` | LLM prompt / system prompt / template の改善・評価。 | prompt tuning 作業用。 |
| `python-dev` | Python 実装、pytest、typing、Pydantic、packaging。 | `eng-practices` と連携。 |
| `retrospective-codify` | 作業終盤に学びを rule / skill / lint へ codify する。 | 繰り返しミスの恒久化向け。 |
| `security-check` | secret leak、injection、auth、OWASP 観点の security review。 | 高リスク変更では明示的に使う。 |
| `terraform-dev` | Terraform / OpenTofu の module、state、plan、security。 | infra 変更向け。 |
| `typescript-dev` | TypeScript / TSX、Vitest/Jest、Zod、ESLint/Biome。 | frontend / Node 実装向け。 |

## system skill

`skills/.system/` は bundled skill の置き場です。
通常の repo-local skill と同じ形式ですが、由来は Codex / OpenAI 側です。

| Skill | 用途 |
|---|---|
| `imagegen` | AI 生成画像や bitmap asset の生成・編集。 |
| `openai-docs` | OpenAI API / product の最新公式 docs を確認する。 |
| `plugin-creator` | Codex plugin directory と manifest を scaffold する。 |
| `skill-creator` | 新規 skill の作成・改善手順を案内する。 |
| `skill-installer` | curated skill や GitHub repo の skill を `$CODEX_HOME/skills` に導入する。 |

## vendored external skill

`upstreams.json` に登録された外部 skill です。
更新時は security review report を `dotfiles/.agent/work/skill-upstream-reviews/` に残します。

| Group | Upstream | Local path | 内容 |
|---|---|---|---|
| `empirical-prompt-tuning` | `mizchi/skills` | `empirical-prompt-tuning/` | agent 向け指示を実行者評価で反復改善する日本語 skill。 |
| `modern-web-guidance` | `GoogleChrome/modern-web-guidance` | `modern-web-guidance/` | HTML / CSS / client-side JS の最新 Web best practice 検索 skill。 |
| `mattpocock-skills` | `mattpocock/skills` | `mattpocock/` | 設計質問、diagnose、prototype、handoff、architecture review 系 skill。 |
| `superpowers` | `obra/superpowers` | `superpowers/` | TDD、parallel agent dispatch、skill writing の workflow skill。 |

### mattpocock group

| Skill | 用途 |
|---|---|
| `diagnose` | hard bug / performance regression を再現、最小化、仮説、計測、回帰テストで詰める。 |
| `grill-me` | 計画や設計を一問ずつ深掘りし、曖昧さを潰す。 |
| `grill-with-docs` | `CONTEXT.md` や ADR を踏まえて設計判断と言語を詰める。 |
| `handoff` | 会話を別 agent 向けの引き継ぎ文書にまとめる。 |
| `improve-codebase-architecture` | codebase の構造改善、deep module、testability を探す。 |
| `prototype` | throwaway prototype で状態設計や UI 案を試す。 |
| `zoom-out` | 不慣れなコード領域の上位構造を把握する。 |

### superpowers group

| Skill | 用途 |
|---|---|
| `dispatching-parallel-agents` | 独立した複数タスクを並列 agent に分ける判断を助ける。 |
| `test-driven-development` | feature / bugfix 実装前に TDD の進め方を固定する。 |
| `writing-skills` | skill 作成・編集・検証の workflow を支援する。 |

## local-only / ignored skill

| Path | 内容 |
|---|---|
| `hatch-pet/` | Codex pet の spritesheet / package を作る curated skill。`skill-installer` 経由で入るローカル導入物として扱い、現状は `.gitignore` で除外しています。 |
| `codex-primary-runtime/` | Codex runtime 系のローカル状態。現状は Git 管理対象外です。 |
| `.hub/`, `.curator_state` | skill hub / curator の cache・状態ファイル。Git 管理対象外です。 |

## support files

| Path | 内容 |
|---|---|
| `upstreams.json` | 外部 vendored skill の manifest。repository、branch、固定 commit、mapping、tree hash、security review metadata を持ちます。 |
| `review-prompts/skill-upstream-security.md` | 外部 skill 更新時に使う security review prompt template。 |

## 追加・更新の目安

- 新しい自作 skill は `skills/<name>/SKILL.md` として追加します。
- 外部 skill は `upstreams.json` に登録し、固定 commit と security review を残します。
- `references/` は長い補助資料、`scripts/` は再利用する検証・変換 script、`agents/` は agent 固有設定に使います。
- secret、cache、作業ログ、ローカル導入物は Git 管理しません。
- `dotfiles/.agent/skills` の構成を変えたら、必要に応じて Waza eval と `dotfiles/.agent/README.md` / `README_JA.md` の説明も更新します。

## よく使う確認コマンド

```bash
python3 scripts/agent_skill_upstreams.py check
find dotfiles/.agent/skills -name SKILL.md -print | sort
git status --short --ignored dotfiles/.agent/skills
```
