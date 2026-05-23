# Codex Pets

English version: [README.md](README.md)

このディレクトリは、repo 管理する Codex pet package を置く場所です。
`dotfiles/.agent/sync.sh` により、Codex の live runtime path へ symlink されます。

## 構成

```text
pets/
└── <pet-name>/
    ├── pet.json
    └── spritesheet.webp
```

追跡するのは packaged runtime file だけにします。
作業用画像、prompt、QA contact sheet、preview video、生成途中 artifact は、この tree の外か local work log に置きます。

## 現在の pet

| Pet | Files |
|---|---|
| `mirai` | `pet.json`, `spritesheet.webp` |

## 更新ルール

- pet ごとに専用ディレクトリを作ります。
- 特別な理由がない限り、追跡するのは `pet.json` と `spritesheet.webp` だけにします。
- source image、prompt、credential、log、QA video はこのディレクトリに置きません。
- 新しい pet asset を追加する前に `.gitignore` の挙動を確認し、`pets/` 配下の無関係なファイルが ignore される状態を保ちます。

## よく使う確認コマンド

```bash
git check-ignore -v dotfiles/.agent/pets/<pet-name>/secrets.json
git check-ignore -v dotfiles/.agent/pets/<pet-name>/id_rsa
git status --short dotfiles/.agent/pets
```
