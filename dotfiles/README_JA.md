# Managed Dotfiles

English version: [README.md](README.md)

このディレクトリは、通常の chezmoi home source state ではなく、repo-level dotfile や runtime asset として管理する file を置く場所です。

## 構成

| Path | 用途 |
|---|---|
| `.agent/` | 共有 AI agent prompt、app config、hook、skill、eval、pet asset。 |
| `.tmux.conf` | tmux configuration source。 |

通常の home file は `home/` が chezmoi source state です。
chezmoi source tree の外で意図的に管理する file、または共有 AI agent runtime に属する file はこのディレクトリに置きます。

## 更新ルール

- `.agent/` の document と sync behavior は `dotfiles/.agent/README_JA.md` と揃えます。
- chezmoi で render するべき file は `home/` に置きます。
- local secret や generated cache はここに置きません。

## よく使う確認コマンド

```bash
zsh dotfiles/.agent/sync.sh
zsh tests/test_agent_sync.sh
git diff --check -- dotfiles
```
