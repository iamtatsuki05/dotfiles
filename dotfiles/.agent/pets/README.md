# Codex Pets

Japanese version: [README_JA.md](README_JA.md)

This directory stores repository-managed Codex pet packages.
`dotfiles/.agent/sync.sh` links this tree to the live Codex runtime path.

## Layout

```text
pets/
└── <pet-name>/
    ├── pet.json
    └── spritesheet.webp
```

Only the packaged runtime files should be tracked.
Working files, prompts, QA contact sheets, preview videos, and generated intermediate artifacts should stay outside this tree or under local work logs.

## Current Pets

| Pet | Files |
|---|---|
| `mirai` | `pet.json`, `spritesheet.webp` |

## Update Rules

- Keep each pet in its own directory.
- Track only `pet.json` and `spritesheet.webp` unless there is a specific reason to add more runtime files.
- Do not put source images, prompts, credentials, logs, or QA videos in this directory.
- Verify `.gitignore` behavior before adding new pet assets so unrelated files under `pets/` stay ignored.

## Common Checks

```bash
git check-ignore -v dotfiles/.agent/pets/<pet-name>/secrets.json
git check-ignore -v dotfiles/.agent/pets/<pet-name>/id_rsa
git status --short dotfiles/.agent/pets
```
