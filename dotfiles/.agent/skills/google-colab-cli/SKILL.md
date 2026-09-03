---
name: google-colab-cli
description: "Use when installing, authenticating, switching accounts, or operating google-colab-cli (colab new/run/exec/upload/download/stop) for Colab VM execution, file transfer, GPU/TPU runs, or compute-unit-sensitive sessions. Do not use for the Colab browser UI, local Jupyter, or colab update --install unless the user asks."
---

# google-colab-cli

Operate Google Colab VMs from the terminal with `colab` (0.6.0 verified). A session is a billable VM plus a live kernel: `new` allocates, `stop` releases, and nothing else reclaims it.

## Install and identity

Managed by mise (`"pipx:google-colab-cli" = "latest"`).

```bash
colab version                                   # or: mise exec 'pipx:google-colab-cli' -- colab version
colab --auth=oauth2 whoami                      # email, scopes, expiry
colab --auth=oauth2 sessions                    # live VMs on this account
```

Always pass `--auth=oauth2` or `--auth=adc` before the subcommand. `colab --help` reports `oauth2` as default while the bundled guide says `adc`; never rely on it. Run `whoami` and `sessions` before anything that costs compute units or touches Drive, and confirm with the user: account, accelerator, expected duration, files transferred, Drive/GCP exposure, and who stops the VM.

## Command forms

```bash
colab --auth=oauth2 new -s work --gpu T4                        # CPU if --gpu/--tpu omitted
colab --auth=oauth2 exec -s work -f train.py --timeout 3600     # default --timeout is 30 seconds
echo 'import torch; print(torch.cuda.is_available())' | colab --auth=oauth2 exec -s work
colab --auth=oauth2 upload -s work ./data.csv /content/data.csv
colab --auth=oauth2 download -s work /content/out.txt ./out.txt
colab --auth=oauth2 install -s work pandas                     # or: -r requirements.txt
colab --auth=oauth2 status -s work
colab --auth=oauth2 log -s work -n 20                          # structured events; read on failure
colab --auth=oauth2 stop -s work

# one-shot: new + exec + stop; script args after the path are passed verbatim
colab --auth=oauth2 run --gpu T4 --timeout 3600 train.py --epochs 3 > out.txt
```

- `--gpu`: `T4`, `L4`, `G4`, `H100`, `A100`. `--tpu`: `v5e1`, `v6e1`. An unknown `--gpu` value silently becomes `A100`; a `400` on `new` with an accelerator means no entitlement, so fall back to `--gpu T4` or CPU.
- Always name sessions with `-s`; auto-generated names make later commands ambiguous.
- `exec` takes `-f <file>` or stdin only; there is no inline code argument. Kernel state persists across `exec` calls until `stop` or `restart-kernel`. Working directory is `/content`; use absolute paths.
- Set `--timeout` explicitly for anything longer than a smoke test; the 30-second default kills training runs.
- `run` writes its own `[colab] ...` lines to stderr and the script's stdout to stdout, and propagates the script's exit code. Add `--keep` to keep the VM for follow-up `exec`.
- `repl`, `console`, `auth`, and `drivemount` need a TTY. Never run them from an agent; ask the user to run `colab drivemount -s work` themselves when Drive is needed.
- For anything else, read `colab skill` (the bundled operator guide) instead of walking every `--help`.

## Account switching

OAuth2 (token at `~/.config/colab-cli/token.json`):

```bash
colab --auth=oauth2 sessions
colab --auth=oauth2 stop -s <session-name>      # for every listed session
rm ~/.config/colab-cli/token.json
colab --auth=oauth2 whoami                      # browser consent for the new account
```

ADC: re-mint credentials with all four scopes, then verify.

```bash
gcloud auth application-default login \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,https://www.googleapis.com/auth/colaboratory
colab --auth=adc whoami
```

`--config <path>` isolates session state (parallel agents), not the credential.

## Failures

- `403` against `colab.pa.googleapis.com` or `keep_alive_stopped reason=consecutive_4xx_errors`: missing `colaboratory` scope. Re-auth as above; do not retry blindly.
- Wrong email in `whoami`: stop sessions, remove `token.json`, re-auth.
- `Session not found` / `404` / `401` on `exec`: the VM was pruned. `colab sessions`, then `new` again.
- Hung kernel or timeout: `colab restart-kernel -s work` keeps the VM; otherwise `stop` then `new`.

## Teardown

Finish every job with `colab --auth=... stop -s <name>` and confirm with `sessions`. Treat downloads, logs, notebook outputs, tokens, and Drive content as sensitive. `colab update --install` is a Linux-only pip upgrade; run it only on explicit request.
