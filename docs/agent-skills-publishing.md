# Publish Agent Skills After Merges

[日本語](agent-skills-publishing_JA.md) · [Documentation index](README.md)

The publishing workflow mirrors reviewed skills to `iamtatsuki05/skills` only
after a skill-tree change reaches `main`. The canonical source remains
`dotfiles/.agent/skills/`; publishing does not move, rename, or write into that
directory.

## Only merged skill changes trigger publishing

`.github/workflows/publish-agent-skills.yml` listens for pushes to `main` whose
changed paths match `dotfiles/.agent/skills/**`. Its first job asks GitHub which
pull requests are associated with the pushed commit. Preparation and publishing
run only when the head commit is the merge result of a pull request targeting
`main`. A direct push can start the gate job, but cannot export or publish.
There is no `pull_request`, `pull_request_target`, `workflow_dispatch`, or
`schedule` trigger.

The workflow performs these operations:

1. Confirm that the source commit came from a pull request merged into `main`.
2. In a job without the destination secret, read the explicit allowlist in
   `config/agent-skills-publish.json` and export tracked regular files.
3. Package `skills/`, Claude plugin manifests, bilingual READMEs, and the mirror
   ownership marker as one short-lived artifact.
4. In a separate job, compare the source commit's skill tree with the latest
   `main`, validate the artifact and destination, and push only when the export
   differs. A newer non-skill commit does not suppress a valid publication, but
   a newer skill change does.

`scripts/export_agent_skills.py` rejects tracked symlinks, non-empty output
directories, and output paths inside this source repository. Destructive mirror
sync is allowed only for an empty destination or one containing the expected
`.agent-skills-mirror.json`; a non-`main` default branch also fails closed. The
generated repository contains no root license. Its README states that no license
is granted, while any license notices carried by individual files remain in
force.

The generated Claude plugin intentionally omits a semantic version. For a
Git-hosted marketplace, the source commit identifies the version, so every
published mirror commit remains updateable without a separate version bump.

## Configure the destination before merging a skill update

This setup is required once. Do not place a token in the repository or workflow.

1. Create an empty public repository `iamtatsuki05/skills` whose default branch
   is `main`. Do not initialize it with a README, license, or other files.
2. In `iamtatsuki05/dotfiles`, create a GitHub Actions environment named
   `skills-publishing`.
3. Create a fine-grained personal access token restricted to
   `iamtatsuki05/skills` with repository Contents read and write permission.
4. Store the token as the `SKILLS_REPO_TOKEN` environment secret.

The source workflow has only `contents: read` and `pull-requests: read`. The
destination token is scoped to the final publish step; the merge gate, exporter,
and artifact preparation cannot read it. Each third-party workflow dependency
is pinned to a commit. If the repository, branch, marker, or secret is invalid,
publishing fails before the destination is replaced.

## Change the public selection through review

Edit the sorted `skills` array in `config/agent-skills-publish.json`. Every
listed directory must contain a tracked `SKILL.md` whose frontmatter `name`
matches the directory name. Adding a skill to the allowlist does not publish by
itself; the next merged change under `dotfiles/.agent/skills/**` publishes the
new selection.

Run the focused checks before opening a pull request:

```bash
python3 tests/test_agent_skill_publish.py
export_dir="$(mktemp -d)"
python3 scripts/export_agent_skills.py --output "$export_dir"
claude plugin validate "$export_dir"
npx skills@latest add "$export_dir" --list
```

Delete the temporary export after inspection. The repository test runner also
executes `tests/test_agent_skill_publish.py` on macOS and Ubuntu.
