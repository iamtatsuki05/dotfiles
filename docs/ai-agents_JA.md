# AI エージェント設定

[English](ai-agents.md) · [ドキュメント一覧](README_JA.md)

共有 AI agent ファイルは `dotfiles/.agent/` で管理します。変更は canonical tree に
加え、同期を実行してから、管理ソースと代表的な展開先の両方を検証します。

## Canonical file と管理境界

- `dotfiles/.agent/AGENTS.md`: 共通の agent policy。
- `dotfiles/.agent/apps/`: アプリ別設定と hook。
- `dotfiles/.agent/skills/`: local skill と review 済みの vendored skill。
- `dotfiles/.agent/evals/`: Waza evaluation suite。
- `dotfiles/.agent/sync.sh`: 対応する agent home への同期処理。

リポジトリルートには、意図的に `AGENTS.md` symlink を置いていません。一部の
展開先は canonical tree を指す symlink です。展開済みファイルは別の source of
truth ではありません。

support matrix、ファイル対応、ignore、hook の挙動は、このリポジトリ全体の
説明より頻繁に変わります。編集前に
[AI agent ディレクトリの README](../dotfiles/.agent/README_JA.md)を確認してください。

## 同期して検証する

```sh
zsh dotfiles/.agent/sync.sh
zsh tests/test_agent_sync.sh
zsh tests/test_agent_support_matrix.sh
```

ファイルの同期が成功しても、起動中の agent process が設定を再読込したとは
限りません。対象 client の仕様に応じて再起動または reload し、live config は
別に確認してください。

## Skill と prompt を評価する

Waza の command routing を確認するときは、先に dry-run を使います。

```sh
mise run waza-eval-model -- --agent all --dry-run
```

focused command と suite の構成は `dotfiles/.agent/README_JA.md` に記載しています。
評価結果が示すのは、選択した suite と agent の結果です。sync test や live client
の確認を代替するものではありません。

## 外部 skill の由来と review を保つ

外部 skill は `dotfiles/.agent/skills/upstreams.json` に登録し、
`scripts/agent_skill_upstreams.py` で管理します。upstream の pinned commit、license、
attribution、local overlay、security review、focused validation を残してください。
review 済み tree にファイルを直接上書きして更新しないでください。

```sh
python3 scripts/agent_skill_upstreams.py check
```

## Claude Code のログインプロファイルを安全に使う

`claude-account` は、macOS Keychain 内の単一の full-scope
`claude auth login` credential を使います。macOS では profile ごとに独立した
full-login credential を同時保持できないため、切替時には browser 認証が必要です。

登録や切替の前に、すべての Claude Code session を終了します。
`claude-account` から起動した session は共有 lock を保持し、通常の `claude`
process も別に検出されます。

```sh
pgrep -fl claude
# Claude process が残っていないことを確認

claude-account auth-login <profile>
# browser で対象アカウントを選択
```

このコマンドは email と organization ID から作った SHA-256 fingerprint と
subscription 種別だけを、mode 600 の
`~/.config/claude-account/login-profiles.json` に保存します。email と
organization ID の値そのものは保存しません。

登録済み mapping を確認し、一致する profile から起動します。

```sh
claude-account list
claude-account <profile> --model fable
claude-account <profile> --resume <session-id> --model fable
```

wrapper は子 process から API key、custom endpoint、Bedrock、Vertex、Foundry の
selector を除外します。確認できる settings に `apiKeyHelper` や認証用 env override
があれば fail closed します。通常の `claude` と `claude-auto` は profile identity
検査を通らないため、この複数アカウント運用では使いません。
