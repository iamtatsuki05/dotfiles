---
name: google-colab-cli
description: "Use when installing, authenticating, account switching, troubleshooting, or operating google-colab-cli for Colab VM execution, file transfer, Drive mount, GPU/TPU use, or compute-unit-sensitive sessions."
---

# google-colab-cli

## USE FOR:

- Verify/install `google-colab-cli`.
- Plan `colab new/run/exec`, transfer, Drive mount, teardown.
- Switch accounts or debug auth/scope failures.

## DO NOT USE FOR:

- Browser-notebook MCP workflows; use `colab-mcp`.
- GPU/TPU, OAuth, Drive mount, or session creation before account/resource confirmation.
- `colab update --install` unless explicitly requested.

## Install

Managed by mise: `"pipx:google-colab-cli" = "latest"`.

```bash
mise exec 'pipx:google-colab-cli' -- colab version
```

## Auth

Pass `--auth=oauth2` or `--auth=adc`; do not rely on defaults. Verify identity before paid or Drive-touching work:

```bash
colab --auth=oauth2 whoami
colab --auth=adc whoami
```

OAuth2 switch:

```bash
colab --auth=oauth2 sessions
colab --auth=oauth2 stop -s <session-name>
rm ~/.config/colab-cli/token.json
colab --auth=oauth2 whoami
```

ADC switch: re-run `gcloud auth application-default login` with `userinfo.email` and `colaboratory` scopes, then `colab --auth=adc whoami`.

`--config` separates session metadata, not the default OAuth2 token.

## Examples

Before provisioning, confirm account, accelerator, duration, Drive/GCP exposure, transferred files, and teardown.

```bash
colab --auth=oauth2 new -s work --gpu T4
colab --auth=oauth2 exec -s work -f script.py
colab --auth=oauth2 stop -s work
```

One-shot: `colab --auth=oauth2 run --gpu T4 script.py`. Treat downloads, logs, notebook outputs, tokens, and Drive content as sensitive.

## Troubleshooting

If scopes fail, re-run ADC login with `colaboratory`. If account is wrong, stop sessions and remove OAuth2 `token.json` before reauth.
