---
name: magika
description: "Use when the user asks to identify file types, verify whether an extension matches file content, classify files in a directory, or explicitly asks to use Magika."
---

# Magika

## Overview

Google 製の deep learning ベースのファイルタイプ識別 CLI。従来のマジックバイト（ファイルシグネチャ）ベースと異なり、ファイル内容からコンテンツタイプを識別する。

## インストール確認と手順

まず `magika` が利用可能かを確認する。

```bash
magika --version
```

**インストールされていない場合:**

global install はせず、まず `missing-tools` skill の方針に沿って ad-hoc 実行を試す。

```bash
# mise の pipx backend で一時実行（推奨）
mise exec 'pipx:magika' -- magika --version

# nixpkgs の ad-hoc 実行
nix run nixpkgs#magika -- --version
```

永続インストール（`pipx install magika` / `pip install magika`）は環境変更を伴うため、ユーザーに確認してから実行する。

---

## 基本的な使い方

### 単一ファイルの識別

```bash
# デフォルト出力（説明 + グループ）
magika ./unknown_file

# シンプルなラベルだけ出力（自動化に推奨）
magika --label ./unknown_file

# MIMEタイプを出力
magika --mime-type ./unknown_file

# スコア（信頼度）も表示
magika --output-score ./unknown_file
```

### 複数ファイル・ディレクトリ

```bash
# 複数ファイルを一括識別
magika file1 file2 file3

# ディレクトリを再帰的に識別
magika --recursive ./target_dir/

# ワイルドカード
magika ./uploads/*
```

### 標準入力から識別

```bash
cat unknown_file | magika -
```

### JSON / JSONL 出力（パース・自動化向け）

```bash
# JSON 出力（全ファイルをまとめて配列）
magika --json ./file

# JSONL 出力（1ファイル1行、ストリーム処理向け）
magika --jsonl ./file

# jq と組み合わせてラベルだけ取り出す
magika --json ./file | jq '.[].result.value.output.label'
```

### カスタムフォーマット出力

```bash
# パスとラベルだけ
magika --format '%p: %l' ./file

# パス・ラベル・スコア
magika --format '%p\t%l\t%s' ./uploads/*
```

---

## Python API での利用

バッチ処理や pipeline 組み込みで CLI が不向きなときは Python API を使う。例と API 詳細は [references/commands.md](references/commands.md) を参照。

---

## ワークフロー

### 単一ファイルの種別を調べる

1. `magika --version` でインストールを確認
2. 未インストールなら `mise exec 'pipx:magika' -- magika` などの ad-hoc 実行を優先し、永続インストールはユーザー承認後に行う
3. `magika --json <ファイル>` で識別
4. 結果の `output.label` / `output.description` をユーザーにわかりやすく報告

### ディレクトリを分類する

1. 対象ディレクトリ、再帰の有無、除外したい機密ファイルや巨大ファイルを確認する
2. 必要なら `rg --files` などで対象件数を概算する
3. `magika --jsonl --recursive <dir>` を実行する
4. 種別ごとの件数、拡張子不一致、低 confidence の候補を整理して報告する

### 低 confidence / 不一致の扱い

- confidence が低い、または拡張子と内容が矛盾する場合は、Magika の結果だけで断定しない。
- 必要に応じて `file <path>`、MIME type、先頭数行の安全な確認を併用する。
- 機密性が高そうなファイル内容は本文を再掲せず、パスと判定概要だけを報告する。

### 拡張子と実態が一致しているか確認する

```bash
# 拡張子と実際のタイプを並べて確認
magika --format '%p\t%l' ./uploads/* | \
  awk -F'\t' '{ split($1, a, "."); ext=a[length(a)]; print $0 "\t" ext }'
```

---

## 詳細リファレンス

CLIオプション全一覧・JSON出力構造・Python API 詳細は [references/commands.md](references/commands.md) を参照。
