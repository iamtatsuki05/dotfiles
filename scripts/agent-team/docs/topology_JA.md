# Team topology domain

[English](topology.md)

`agent_team.topology` は、backendに依存しない不変な `TeamDefinition` を定義します。
Agentの構成と、委譲・レビュー・エスカレーションの関係を表現します。
Agentの起動、設定の読み込み、providerやOrcaの呼び出しは行いません。

## データモデル

Python 3.11の標準ライブラリだけを使います。

- `TeamId` と `NodeId` は型付きの文字列識別子です。
- `ProfileRef` は `provider`、`transport`、要求する `permission` を持ちます。
- `AgentNode` は `node_id`、`label`、`profile`、`is_main` を持ちます。
- `Edge` は `source` から `target` への関係です。種類は
  `delegates-to`、`reviewed-by`、`escalates-to` の3つに限定します。
- `TeamDefinition` は `team_id`、nodeのtuple、edgeのtupleで構成します。
  すべての値オブジェクトを frozen とします。

例:

```python
from agent_team.topology import (
    AgentNode,
    Edge,
    EdgeKind,
    NodeId,
    Permission,
    ProfileRef,
    TeamDefinition,
    TeamId,
)

team = TeamDefinition(
    TeamId("build-team"),
    (
        AgentNode(
            NodeId("main"),
            "Main",
            ProfileRef("claude", "direct", "orchestrator"),
            is_main=True,
        ),
        AgentNode(
            NodeId("worker"),
            "Worker",
            ProfileRef("codex", "direct", "workspace-write"),
        ),
    ),
    (Edge(NodeId("main"), NodeId("worker"), EdgeKind.DELEGATES_TO),),
)
```

## Profile resolverとの境界

検証時には、読み取り専用の `ProfileResolver` を渡します。

```python
class ProfileResolver(Protocol):
    def resolve(self, profile: ProfileRef) -> frozenset[Permission] | None: ...
```

resolverは、provider/transportの利用可否と、実際に認められたpermissionの集合を返します。
`None` はprovider/transportが未知であることを表します。要求したpermissionが
`permissions` に含まれなければ `permission-mismatch` です。
topology moduleは `agent_team.registry` をimportせず、provider commandも実行しません。
既存の検証済みregistryからProtocolへ変換する責任は呼び出し側にあります。

## 検証契約

`validate_team(definition, resolver)` の戻り値は不変な `ValidationResult` です。
`errors` はcodeとmessageの順にソートするため、同じ入力から常に同じ結果になります。
rendererは同じ順序のissueを持つ `TopologyValidationError` を送出し、出力を返しません。

検証ルールは次のとおりです。

- graphは空にできず、`is_main` nodeはちょうど1つです。
- Main nodeは`orchestrator`を使い、Main以外はそのpermissionを使えません。
- `is_main`は厳密なboolとし、edge endpointはnode IDの大文字小文字を含めて一致させます。
- 型注釈だけには依存しません。
- node IDは、大文字小文字と前後の空白による曖昧さを含めて重複できません。
- labelは空でなく、前後に空白を持たず、case-fold後に重複できません。
- edgeの種類と両端は既知でなければなりません。重複edgeも拒否します。
- 1つの `delegates-to` targetに入る委譲edgeは最大1本です。
- 1つのsourceから出る `reviewed-by` は最大1本です。
- 1つのsourceから出る `escalates-to` は最大1本です。
- self-edgeを拒否します。self `reviewed-by` は `self-review` として報告します。
- edge kindごとのdirected cycleを拒否します。異なる関係をまたぐpathはMainへ戻れます。
  たとえば、MainからWorkerへの委譲、WorkerからReviewerへのreview、ReviewerからMainへの
  escalationは、委譲やreview自体を再帰させないため有効です。
- すべてのnodeは、directed edgeをたどってMainから到達できなければなりません。

識別子、label、profile field、permissionの不正は検証時に失敗します。
C0/C1/DEL制御文字、Unicode line separator、lone surrogate、空文字列、曖昧な前後空白を受け付けません。
resolverがlookup・I/O・timeoutなどの想定したoperational errorを返した場合や、Protocol外の値を
返した場合もfail-closedで拒否します。予期しないprogramming errorは隠しません。

## 決定的なrenderer

`render_json`、`render_ascii`、`render_mermaid` と、formatを検査する
`render_topology` を提供します。入力tupleの順番に依存せず、nodeとedgeをそれぞれソートします。

- JSONはUTF-8 JSONです。object key、配列を安定化し、最後に改行を1つ付けます。
  topology dataだけを含みます。
- ASCIIはJSON形式で値をquoteした行指向の表現です。Markdownの箇条書きやlink、
  実行可能な文字列を生成しません。
- Mermaidのnode IDは `n_<sha256-prefix>` として生成します。入力IDはescape済みlabel内に
  だけ現れ、Mermaid構文の識別子には使いません。labelのquote、backslash、HTML delimiter、
  Markdownで意味を持つ記号、制御文字をescapeします。edge labelは固定enumからだけ生成します。

受け付けるformatは `json`、`ascii`、`mermaid` のみです。未知の名前や大文字小文字違いは
`TopologyFormatError` になり、暗黙のfallbackはありません。不正なdefinitionをrenderしようと
した場合も、出力を返す前に `TopologyValidationError` になります。

version 4のinspectionでは、このmoduleを `agent-team teams`、`agent-team graph`、
`agent-team start --team ... --dry-run` へ接続済みです。これらの経路はproviderを起動せず、
runtime resourceも作らずに検証・描画します。config version 3、runtime state、MCP、
Orca lifecycle、default teamには接続していません。後続の統合では、この純粋なcontractへ
明示的に適応させる必要があります。
