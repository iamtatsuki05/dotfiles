# SteerMouseの設定

English version: [README.md](README.md)

`steermouse/Device.smsetting`は、登録した全デバイスの設定本体です。
このディレクトリの`.smsetting_dev`は、手動で取り込むためのデバイス別
エクスポートです。設定本体を変更しても、エクスポートは自動更新されません。

macOSのセットアップでは、
`~/Library/Application Support/SteerMouse & CursorSense`から、
このcheckoutの`config/mouse/steermouse/`へディレクトリ単位でリンクを張ります。
保存時に`Device.smsetting`が置き換わっても、書き込み先はリポジトリ内に保たれます。
以後、SteerMouseで変更した設定はGitの差分になり、commitとpushは手動で行います。
このリポジトリは公開されています。設定を公開する前に、ボタンに割り当てた文字列・
コマンド・URLなどに非公開情報が含まれないか、SteerMouse上で確認してください。
設定本体はバイナリのため、Git差分では設定内容を読めません。

## セットアップ

```sh
bash scripts/setup_steermouse.sh --dry-run
bash scripts/setup_steermouse.sh
# miseのタスクを展開済みなら、次のコマンドでも実行できます。
mise run steermouse-setup
```

`features.macos=true`の場合、`zsh main.sh --profile full`と
`zsh main.sh --only config --profile full`からも実行されます。
CLI profileとLinuxでは適用しません。`--only config --dry-run`では確認のみ行います。

適用前に、SteerMouseとCursorSenseを常駐Managerも含めて終了してください。
スクリプトはアプリを停止しません。別の場所へのリンク、内容が異なる既存設定、
追加ファイルがあるフォルダは置き換えず、エラーで停止します。
これらは先に保全し、差分を整理してください。現在のデバイス設定を採用する場合は、
アプリの停止中に実環境の`Device.smsetting`を`config/mouse/steermouse/`へコピーし、
Git差分を確認してから再実行します。

内容が一致する既存ディレクトリは、
`~/Library/Application Support/SteerMouse-backup.*/settings`という一意な場所へ
丸ごと退避してからリンクします。確認が済むまではバックアップを残してください。
適用後は、[公式FAQ](https://plentycom.jp/inc/faq.php?style=j&type=sm)に従い、
SteerMouseを開いて「デバイス」タブの「再設定」を押してください。

移動・削除しないcheckoutから実行してください。checkoutを削除するとリンクが切れます。
既存のリンクを別のworktreeへ張り替える操作も、スクリプトは拒否します。
設定フォルダはCursorSenseと共用です。`~/Library/Preferences`のライセンス設定は管理しません。

## 元に戻す

アプリと常駐Managerを終了します。上記のパスが管理対象のシンボリックリンクであることを
確認してから、リンクだけを削除し、リポジトリの`steermouse/`を同じ場所へ実ディレクトリとして
コピーします。これで直近の変更を保持できます。セットアップ前の状態へ戻したい場合は、
退避先の`settings`を戻してください。SteerMouseを開き、「再設定」を押します。
