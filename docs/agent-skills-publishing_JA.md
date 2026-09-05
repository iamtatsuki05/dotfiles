# skillの変更をmergeした後に公開する

[English](agent-skills-publishing.md) · [ドキュメント一覧](README_JA.md)

公開workflowは、skill treeの変更が`main`へ入った後に限り、レビュー済みのskillを
`iamtatsuki05/skills`へ同期します。正本は引き続き
`dotfiles/.agent/skills/`です。公開処理がこのディレクトリを移動、改名、更新する
ことはありません。

## skillを変更したmergeだけが公開処理を起動する

`.github/workflows/publish-agent-skills.yml`は、変更パスが
`dotfiles/.agent/skills/**`に一致する`main`へのpushを受け取ります。最初のjobで、
pushされたcommitに紐づくPull RequestをGitHubへ問い合わせます。head commitが
`main`向けPull Requestのmerge結果である場合だけ、準備と公開へ進みます。
直接pushでgate jobが起動することはありますが、exportと公開は行いません。
`pull_request`、`pull_request_target`、`workflow_dispatch`、`schedule`のtriggerは
ありません。

workflowは次の処理を行います。

1. source commitが`main`へmergeされたPull Requestの結果であることを確認する。
2. 公開先tokenを持たないjobでallowlistを読み、Git管理された通常ファイルを
   書き出す。
3. `skills/`、Claude plugin manifest、日英README、mirror所有markerを短期artifactへ
   まとめる。
4. 別jobでsource commitのskill treeを最新`main`と比較し、artifactと公開先が
   正しいことを確認して、exportに差分がある場合だけpushする。後続がskill外の
   commitなら公開を続け、後続にskill変更がある場合は古い公開をskipする。

`scripts/export_agent_skills.py`は、Git管理されたsymlink、空でない出力先、
source repository内の出力先を拒否します。公開先を置き換えられるのは、空の
repositoryか、想定した`.agent-skills-mirror.json`を持つrepositoryだけです。
既定branchが`main`でない場合も停止します。生成repositoryのrootにはlicenseを
置きません。READMEにはlicenseを付与しないことを明記し、個別ファイルにlicense
表記がある場合は、その条件を維持します。

生成するClaude pluginでは、semantic versionを意図的に省略します。Git管理の
marketplaceではsource commitがversionになるため、公開のたびに別のversion更新を
加えなくても、mirror commit単位で更新できます。

## skillの更新をmergeする前に公開先を設定する

次の設定は初回だけ必要です。tokenをrepositoryやworkflowへ直接書かないでください。

1. 既定branchが`main`の空のpublic repository `iamtatsuki05/skills`を作る。
   README、license、その他のファイルで初期化しない。
2. `iamtatsuki05/dotfiles`にGitHub Actions environment
   `skills-publishing`を作る。
3. `iamtatsuki05/skills`だけを対象とし、Repository contentsのread/write権限を
   持つfine-grained personal access tokenを作る。
4. environment secret `SKILLS_REPO_TOKEN`としてtokenを登録する。

source側workflowの権限は`contents: read`と`pull-requests: read`だけです。
公開先用tokenは最後のpublish stepだけに渡し、merge gate、exporter、artifact準備からは
読めません。外部のworkflow dependencyはすべてcommitへpinします。公開先repository、
branch、marker、secretのいずれかが不正なら、公開先を置き換える前に停止します。

## 公開対象の変更もPull Requestでレビューする

`config/agent-skills-publish.json`の`skills`配列を名前順で編集します。
記載するディレクトリには、Git管理された`SKILL.md`が必要です。
frontmatterの`name`はディレクトリ名と一致させてください。allowlistだけを変更しても
公開は行いません。次に`dotfiles/.agent/skills/**`の変更をmergeしたとき、新しい
公開対象が反映されます。

Pull Requestを作る前に、次のfocused testを実行します。

```bash
python3 tests/test_agent_skill_publish.py
export_dir="$(mktemp -d)"
python3 scripts/export_agent_skills.py --output "$export_dir"
claude plugin validate "$export_dir"
npx skills@latest add "$export_dir" --list
```

確認後は一時出力を削除してください。repository全体のtest runnerも、macOSとUbuntuで
`tests/test_agent_skill_publish.py`を実行します。
