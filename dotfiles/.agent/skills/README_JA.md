# Agent Skills

English version: [README.md](README.md)

このディレクトリは、Codex 互換 agent と Waza eval で使う skill の共通置き場です。
各 agent home へは `dotfiles/.agent/sync.sh` から symlink されます。

## 全体像

```text
skills/
├── .system/                 # OpenAI bundled / system skill
├── <skill>/                 # repo-local / vendored skill
├── upstreams.json           # external skill manifest
└── review-prompts/          # upstream review prompt templates
```

`SKILL.md` を持つディレクトリが、agent に読み込ませる 1 skill の単位です。
通常の skill は discovery 互換性のため、由来にかかわらず必ず `skills/<name>/SKILL.md` のフラットな配置にします。
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
| `agent-cli-consult` | Codex CLI / Claude Code CLI に stdin 経由の prompt で読み取り専用のレビュー・調査を依頼する。 | ユーザーが外部 CLI を明示した時だけ使う。 |
| `agent-job-scheduler` | 10 種の agent CLI の長時間・非対話ジョブを queue / retry / cancel し、allowlist、stale recovery、launchd も扱う。 | app 本体、README、pytest を同梱。 |
| `alphaxiv-paper-lookup` | alphaxiv の overview と全文 Markdown で arXiv 論文を要約・比較・実装抽出する。 | 数値は全文で裏取りしてから報告する。 |
| `auto-debugger` | 1 コマンドで再現するエラー・失敗テストの原因特定と修正、リグレッションテスト追加。 | 再現が作れない、flaky、性能退行の場合は `diagnosing-bugs` に引き継ぐ。 |
| `ci-cd` | CI/CD workflow(GitHub Actions、GitLab CI、CircleCI)の作成・修正・調査。 | 権限と本番影響を先に確認し、実際の CI 実行を確認してから報告する。CI で落ちる app コード自体は対象外。 |
| `compatibility-safety` | 根拠のない alias、silent fallback、default 値 fallback、legacy path、runner / backend の黙った差し替えを退ける。 | 書く・レビューする差分にそれらが含まれた時点で読む。実装開始時には読まない。 |
| `database-dev` | EXPLAIN による実測、expand-contract の migration、共有環境への承認手順を含む schema / index / query / migration の設計・レビュー。 | session で 1 回だけ読む。SQL / NoSQL 両方。 |
| `eng-practices` | PR/CL のタイトル・説明、small PR への分割、reviewer コメントへの返答。 | PR description を書く段階でだけ読む。review の実施には使わず、出力形式は依頼文で決める。 |
| `go-dev` | go.mod のバージョンに合わせた Go 実装・テスト・レビュー。テーブル駆動テスト、errgroup / context、-race 確認を含む。 | session で 1 回だけ読む。CI YAML や Go 以外のサービスは対象外。 |
| `git-github-flow` | Git/GitHub 作業を確認・範囲内 write・readback で進める。owner/repo と login は remote から解決、PR は Draft + 明示 assignee / labels、CI gate 後だけ Ready、force-push 禁止。 | fork PR、review 投稿、履歴整理、gh-stack は references。 |
| `goal-prompt-builder` | 依頼を、範囲・checkpoint・検証可能な停止条件を持つ Codex `/goal` prompt に変換する。 | `$goal-prompt-builder` で呼ぶ。本番・課金・権限判断を委ねる goal は拒否する。 |
| `gws` | gws CLI の helper と低レベル API で Google Calendar / Drive / Gmail / Tasks を扱う。 | 読み取りは即実行、書き込みは dry-run か下書きで確認し、承認後に実行する。 |
| `html-preview-review` | ユーザーが preview を求めたときだけ、検証済み結果を private な local HTML review board にして 1 つの presenter で表示する。 | OS ブラウザへの fallback 禁止。未表示は未達として報告する。 |
| `markdown-docs` | Markdown 文書そのもの(README、docs/、ガイド、リリースノート)の構成・記法・リンク・表を作成・編集・レビューする。 | コード変更に付随する README 小修正やスライド・PDF・LaTeX には使わない。日本語の自然さは `natural-japanese`。 |
| `markitdown` | markitdown CLI で PDF / Office / HTML / URL を Markdown に変換する。PDF 失敗時は uvx 経由の fallback。 | 失敗した PDF は再試行せず markitdown[pdf] か pdftotext に切り替える。 |
| `missing-tools` | 未導入コマンドを project env、mise、Nix、comma 経由で global install なしに実行する。 | 解決した実行形は checkpoint.md に記録する。 |
| `prompt-tuner` | モデル API に送る prompt(system prompt、template、few-shot)を実行・評価・診断・修正の反復で改善する。 | agent 向け指示は `empirical-prompt-tuning`、Codex `/goal` は `goal-prompt-builder`。 |
| `python-dev` | pyproject / ruff / mypy / pytest の規約に合わせた Python 実装・テスト・デバッグ。テスト先行と fail fast の規則を含む。 | session で 1 回だけ読む。notebook、Slurm / env script、文書は対象外。 |
| `retrospective-codify` | ユーザーの依頼で session の学びを rule / skill / lint に固定する。自発提案は 1 session 1 回・3 行以内。 | agent 発の候補は session 横断の再発確認が条件。 |
| `security-check` | 攻撃者視点の review(secret 露出、injection、認証・認可、脆弱な依存)。 | security が明示された依頼だけで使う。launcher や破壊的操作の「安全性レビュー」は通常レビュー。Phase 1 の secret grep は単独で使える。 |
| `shaping-japanese-longform` | 事実・因果・ドラマを作らず、日本語の長文記事、論考、解説の構成を整える。 | 文書進行の実況を削り、主張と根拠をつなぐ。文レベルの自然さは `natural-japanese`。 |
| `terraform-dev` | plan 優先の手順、moved / import ブロック、state と秘匿値の扱い、apply の承認手順を含む Terraform / OpenTofu の実装・検証・レビュー。 | session で 1 回だけ読む。既定は plan まで。 |
| `typescript-dev` | tsconfig、lint、テストランナーに合わせた TypeScript / TSX の実装・テスト・デバッグ。Zod、型ガード、公開 API 変更時の同期を含む。 | session で 1 回だけ読む。HTML / CSS レイアウトは `modern-web-guidance`。 |

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
flat layout 用の局所的な参照変更は `local_text_replacements` に宣言します。
更新元の一致件数が `expected_count` と異なる場合、上書き前に失敗します。

| Group | Upstream | Local path | 内容 |
|---|---|---|---|
| `empirical-prompt-tuning` | `mizchi/skills` | `empirical-prompt-tuning/` | agent 向け指示を実行者評価で反復改善する日本語 skill。 |
| `modern-web-guidance` | `GoogleChrome/modern-web-guidance` | `modern-web-guidance/` | HTML / CSS / client-side JS の最新 Web best practice 検索 skill。 |
| `mattpocock-skills` | `mattpocock/skills` | `grilling/`、`diagnosing-bugs/`、`domain-modeling/` など | deprecated alias を除いた現行の設計、diagnosis、handoff、architecture 系 skill。各 skill に upstream LICENSE を同梱。 |
| `superpowers` | `obra/superpowers` | `brainstorming/`、`dispatching-parallel-agents/`、`software-development/systematic-debugging/`、`test-driven-development/`、`writing-skills/` | 5つの workflow 領域を選択導入。3 skill は直接 vendor し、systematic debugging は既存の詳細版へ固定 upstream の条件待ち資料を接続、brainstorming は Three Paths だけの最小 local router とする。 |
| `natural-japanese` | `coji/natural-japanese` | `natural-japanese/` | 日本語の業務文書を、決定的 lint、文書型別の指針、local safety overlay で作成・推敲する skill。 |
| `herdr` | `ogulcancelik/herdr` | `herdr/` | Herdr の pane / workspace 制御 skill。local safety overlay と Apache-2.0 license を同梱。 |
| `stop-slop` | `hardikpandya/stop-slop` | `stop-slop/` | 英語の AI pattern を strict checklist で除く。voice matching は `humanizer`。 |

### mattpocock group

| Skill | 用途 |
|---|---|
| `codebase-design` | deep module 設計の共通語彙と原則を提供する。 |
| `diagnosing-bugs` | red-capable な feedback loop、最小化、仮説、計測、回帰テストで hard bug / performance regression を詰める。 |
| `domain-modeling` | project 用語を明確にし、`CONTEXT.md` や ADR の更新案を作る。 |
| `grilling` | 依存関係が解決済みの質問を round 単位で提示し、frontier ごとに feedback を待つ。 |
| `grill-with-docs` | `grilling` と `domain-modeling` を組み合わせる。 |
| `handoff` | 会話を別 agent 向けの引き継ぎ文書にまとめる。 |
| `improve-codebase-architecture` | codebase の構造改善、deep module、testability を探す。 |

### superpowers group

| Skill | 用途 |
|---|---|
| `brainstorming` | software の依頼を spike、bounded change、architectural design に振り分け、path ごとの次のユーザー判断と durable artifact を示す。承認 gate、server、telemetry は追加しない。 |
| `dispatching-parallel-agents` | 独立した複数タスクを並列 agent に分ける判断を助ける。 |
| `systematic-debugging` | 既存の root-cause workflow を維持し、flaky な非同期テストでは固定 upstream の条件待ち資料を読む。 |
| `test-driven-development` | feature / bugfix 実装前に TDD の進め方を固定する。 |
| `writing-skills` | skill 作成・編集・検証の workflow を支援する。 |

`brainstorming` は upstream の Three Paths の考え方だけを local 向けに調整しています。upstream の visual companion、background server、telemetry、設計文書の自動 commit、全作業への一律承認 gate は導入しません。

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
