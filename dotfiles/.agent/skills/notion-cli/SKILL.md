---
name: notion-cli
description: >-
  Use the Notion CLI (`ntn`) to interact with the Notion API, manage workers,
  and upload files. Use when the user asks to "call the Notion API", "deploy a
  worker", "upload a file to Notion", "create a page", "query a database", or
  any task involving the `ntn` command.
---

# Notion CLI

## Look things up before answering

The CLI is self-documenting. Always prefer running these commands over guessing
syntax or relying on memorized knowledge:

- `ntn api ls` — list every public API endpoint.
- `ntn api <path> --help` — show methods, doc links, and usage for an endpoint.
- `ntn api <path> --docs` — print the full official docs for an endpoint.
- `ntn api <path> --spec` — print a reduced OpenAPI fragment (useful for
  understanding request/response schemas).
- `ntn pages get <page-id>` — retrieve a page as Markdown. Use this to read page
  content.
- `ntn <command> --help` — help for any command or subcommand.

## Install

This dotfiles setup manages `ntn` through mise (`"npm:ntn" = "latest"`). If `ntn` is missing, run `mise install npm:ntn`. Do not run the upstream installer (`curl -fsSL https://ntn.dev | bash`) without a separate explicit request.

## Authentication

- The CLI automatically uses `NOTION_API_TOKEN` when it is set.
- Check whether `NOTION_API_TOKEN` is set without printing its value (for
  example `[ -n "${NOTION_API_TOKEN:-}" ] && echo set`). Never echo, log, or
  write the token, and do not run commands that print it, such as `ntn auth token`.
  If it is set, prefer using it instead of telling the user to run `ntn login`.
- `ntn login` / `ntn logout` — log the CLI in or out (only use if not using
  `NOTION_API_TOKEN`). `ntn login` requires the user to visit a URL in a web
  browser.

## `ntn api`

Run `ntn api --help` for full syntax. Quick summary:

```bash
# GET with query param
ntn api v1/users page_size==100

# POST with inline body fields
ntn api v1/pages parent[page_id]=abc123

# POST with JSON body
ntn api v1/pages -d '{"parent":{"page_id":"abc123"}}'
```

The method is inferred (GET by default, POST when a body is present). Override
with `-X METHOD`.

### Markdown for pages and comments

Prefer `ntn pages create` / `ntn pages edit` for Markdown page content. `ntn pages edit` replaces the whole page. For a partial edit, send `update_content` with `old_str`/`new_str` pairs through `ntn api v1/pages/<page-id>/markdown -X PATCH` (check the body shape with `--spec -X PATCH`). Unless the user explicitly asked for that deletion, do not pass `--allow-deleting-content` or `allow_deleting_content: true` (they allow deleting child pages and databases), and do not add `--yes` to `ntn pages trash` or other commands (it skips confirmation). Use
the `markdown` field when creating or updating comments via `ntn api`.

```bash
# Comment with markdown
ntn api v1/comments -d '{"parent":{"page_id":"abc123"},"markdown":"Here is a [link](https://example.com) and **bold text**."}'

# Page with markdown body
ntn pages create --parent page:abc123 --content '## Heading\n\nSome *formatted* content.'
```

The `markdown` field supports inline formatting (bold, italic, code, links, etc.).
Only fall back to `rich_text` if you need features that Markdown cannot express (e.g. mentions, custom emoji, or colors).

## `ntn files`

Convenience wrapper around the File Uploads API.

```bash
ntn files create < image.png
ntn files create --external-url https://example.com/photo.png
ntn files list
ntn files get <upload-id>
```

## `ntn workers`

Manage Notion workers (deploy, list, execute, etc.). Run `ntn workers --help`
for subcommands.

```bash
ntn workers new my-worker        # scaffold a new project
ntn workers deploy               # deploy from current directory
ntn workers ls                   # list workers
ntn workers exec <capability>    # execute a capability
```
