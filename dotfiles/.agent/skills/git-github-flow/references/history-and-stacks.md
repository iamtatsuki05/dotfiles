# History Cleanup and Stacked Pull Requests

履歴変更、revert、またはgh-stackが必要な場合だけ読む。

## Force-push boundary

- 明示指示なしでは `git push --force` / `--force-with-lease` を実行しない。
- 内部でforce-pushし得る `gh stack sync` も同じ扱いにする。`sync` はfetch、cascade rebase、pushを行い、rebase後は `--force-with-lease` を使う。
- force-push許可は対象branchまたはstackを特定する。別branchへ許可を拡張しない。
- 許可がない場合、公開済みbranchをrebase/squashしてremoteと乖離させない。必要なら新しいfixup commitを追加し、merge自体が許可された時点でrepositoryのsquash-mergeを選ぶ。

## Commit cleanup

まず直接base、dirty state、commit一覧、remoteでの公開有無を確認する。

```bash
git status --short --branch
git log --oneline --reverse "$target_base"..HEAD
git ls-remote --exit-code --heads "$head_remote" "$branch"
```

- remoteに存在しない未公開branch: 変更を意味のあるcommitへ分け、必要に応じてinteractive rebaseの `reword` / `fixup` / `squash` / `edit` を使う。別の責務を1 commitへ押し込まない。
- 公開済みbranch: force-pushの明示許可がなければ履歴を書き換えない。
- cleanup前のheadを記録し、完了後に `git diff --exit-code "$old_head" HEAD`、commit一覧、対象テストを確認する。treeが変わった場合はcleanupとして扱わない。
- cleanupの前後で `git status --short` が空であることを確認し、staged・unstaged・untracked fileを残さない。

## Undo after merge

merge済み・共有済みの履歴はreset/rebase/force-pushで消さず、clean worktreeの専用branchでrevertする。

- 通常commitまたはsquash-merge commit: `git revert <oid>`
- merge commit: 親とmainlineを確認してから `git revert -m <mainline-parent-number> <merge-oid>`
- 複数commitを戻す場合: 依存関係を確認し、新しい順にrevertする。
- conflict時に中止する場合: `git revert --abort`

revert commitを検証してrevert PRを作る。push、PR作成、mergeはそれぞれ依頼された範囲だけ行い、revert PRを自動mergeしない。

進行中の操作が失敗しただけでまだ共有されていない場合は、revert commitを作らず `git rebase --abort`、`git revert --abort`、または `gh stack rebase --abort` で元へ戻す。

## When to use gh-stack

GitHub公式gh-stackは、trunkから上へ積む直線状の依存PRに使う。各layerが独立したレビュー単位で、下のlayerなしでは上を実装できない場合に有効。独立に並列化できる作業や複数parentを持つ構造には使わない。

```text
main <- feature/schema <- feature/repository <- feature/api
```

gh-stackは現在の環境に導入済みの場合だけ使う。未導入なら、extensionの目的、global環境へ追加されること、取消方法を示して個別に許可を求める。許可後の導入と取消は次のとおり。

```bash
gh extension install github/gh-stack
gh extension remove gh-stack
```

公式agent skillは、このrepo-local skillとforce-push方針が重複・競合するため自動インストールしない。別途導入を求められた場合は、`gh skill install` のproject/user scope、配置先、pin、既存skillとの重複、取消方法を確認してから扱う。

stackは専用のclean worktree内で扱い、同じstackを複数agent/worktreeから同時操作しない。`gh stack init` はrepositoryの `rerere.enabled` を有効化するため、stack作成依頼がある場合だけ実行する。検出済みの直接baseをtrunkに指定し、branch名は通常の命名規約に合わせる。複数remoteがある場合は `stack_remote` を明示し、`push` / `submit` / `sync` / `rebase` に `--remote "$stack_remote"` を付ける。bare commandを使うのは `remote.pushDefault` が正しいremoteに設定済みと確認できた場合だけにする。

```bash
gh stack init --base "$target_base" feature/schema
gh stack add feature/repository
gh stack add feature/api
gh stack view --json
```

## Command authorization

- `gh stack view --json`: read-only。状態確認に使ってよい。
- `gh stack init` / `add`: local branchとstack metadataを変更する。stack作成が依頼された場合だけ使う。
- `gh stack submit --auto --remote "$stack_remote"`: 全branchをbranchごとの `--force-with-lease` でpushし、Draft PR群を作る非atomic操作。stackの公開・PR作成と、対象stack全branchへのforce-pushが明示的に許可された場合だけ使う。作成後は各PRを `gh pr edit` し、このskillのtitle、英語見出し、本文言語、assignee、labels、Links規則へ合わせる。`--open` は明示的にReady作成を求められた場合だけ使う。
- `gh stack push --remote "$stack_remote"`: 全active branchをbranchごとの `--force-with-lease` でpushする非atomic操作。対象stack全branchへのforce-pushが明示的に許可された場合だけ使う。
- `gh stack rebase --remote "$stack_remote"`: 未公開stackのcommit整理、または明示的なrebase依頼に限定する。公開済みstackでは後続pushがforce-pushになるため、force-push許可がなければ実行しない。
- `gh stack sync --remote "$stack_remote"`: rebase＋push＋PR同期をまとめて行い、force-with-leaseを使い得る。対象stackへのforce-push許可がない限り絶対に実行しない。
- `gh stack sync --remote "$stack_remote" --prune`: 上記に加えてlocal branchを削除する。force-push許可とbranch削除許可の両方が必要。
- `gh stack merge <target> --yes`: 指定PRだけでなく、そのPR以下の未merge layerも対象になる。`gh stack view --json` で実際にmergeされる全PRを列挙し、その一括mergeとmerge methodが明示承認された場合だけ使う。
- `gh stack modify`: TUI-onlyでdrop/fold/reorder/renameを行うため、自動agent操作には使わない。

gh-stackが使えない、repositoryでStacked PRsが有効でない、または安全条件を満たさない場合は、既存のbranch/PRを黙って組み替えず、通常の明示base付きPRへ戻す。

## Verification

```bash
gh stack view --json
gh pr view "$pr" -R "$base_repo" \
  --json url,title,body,isDraft,baseRefName,headRefName,assignees,labels
gh pr checks "$pr" -R "$base_repo"
```

各layerのbase/head、差分範囲、Draft/Ready、title/body、assignee、labels、Links、CIを確認する。

## Sources

- GitHub changelog: https://github.blog/changelog/2026-07-30-stacked-pull-requests-are-now-in-public-preview/
- Official repository: https://github.com/github/gh-stack
- User-provided overview: https://zenn.dev/ubie_dev/articles/gh-stack-introduction
