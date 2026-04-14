---
name: magika
description: "Google製AI駆動型ファイルタイプ検出ツール「Magika」を使ってファイルの種別を識別する。「このファイルの種類を調べて」「ファイルタイプを検出して」「拡張子が正しいか確認して」「magikaを使って」「ディレクトリ内のファイルを分類して」などのリクエストでトリガー。magikaがインストールされていない場合はインストール手順も案内する。"
---

# Magika

## Overview

Google製のAI駆動型ファイルタイプ検出ツール。従来のマジックバイト（ファイルシグネチャ）ベースと異なり、ディープラーニングモデルで200以上のコンテンツタイプを高精度（平均 ~99%）・高速（~5ms/ファイル）に識別する。

## インストール確認と手順

まず `magika` が利用可能かを確認する。

```bash
magika --version
```

**インストールされていない場合:**

```bash
# CLIツールとして使う場合（推奨）
pipx install magika

# Python ライブラリとしても使う場合
pip install magika
```

> `pipx` が使えない場合は `pip install magika` を使用する。`pip` が見つからない場合は `pip3` または `python3 -m pip` を試す。

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

```python
from magika import Magika

m = Magika()

# ファイルパスで識別
result = m.identify_path("./unknown_file")
print(result.output.label)        # 例: "python"
print(result.output.mime_type)    # 例: "text/x-python"
print(result.output.group)        # 例: "code"
print(result.score)               # 例: 0.99

# バイト列で識別（ファイルシステムに依存しない）
result = m.identify_bytes(b"#!/usr/bin/env python3\nprint('hello')")
print(result.output.label)

# 複数ファイルを一括識別（効率的）
results = m.identify_paths(["a.txt", "b.bin", "c.unknown"])
for r in results:
    print(r.path, r.output.label)
```

---

## ワークフロー

### 単一ファイルの種別を調べる

1. `magika --version` でインストールを確認
2. 未インストールなら `pipx install magika` を案内
3. `magika --json <ファイル>` で識別
4. 結果の `output.label` / `output.description` をユーザーにわかりやすく報告

### ディレクトリ内のファイルを分類する

1. `magika --recursive --jsonl <ディレクトリ>` で全ファイルを識別
2. `jq` でグループやラベルごとに集計・フィルタ
3. 結果をユーザーに整理して報告

### 拡張子と実態が一致しているか確認する

```bash
# 拡張子と実際のタイプを並べて確認
magika --format '%p\t%l' ./uploads/* | \
  awk -F'\t' '{ split($1, a, "."); ext=a[length(a)]; print $0 "\t" ext }'
```

---

## 詳細リファレンス

CLIオプション全一覧・JSON出力構造・Python API 詳細は [references/commands.md](references/commands.md) を参照。
