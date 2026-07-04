# 言語別セキュリティチェックリスト

言語ごとの危険な関数・パターンの検索用正規表現。ヒット行は文脈（入力元がユーザー制御か、安全化されているか）を目視確認する。

## Python

```
eval\(
exec\(
compile\(
__import__\(
pickle\.load
yaml\.load\(   # SafeLoader 指定の有無はヒット行を目視確認 (look-ahead を使う場合は rg -P)
subprocess\..*shell=True
os\.system\(
os\.popen\(
```

## JavaScript/TypeScript

```
eval\(
new Function\(
innerHTML\s*=
outerHTML\s*=
document\.write\(
\.html\(  # jQuery
dangerouslySetInnerHTML
child_process\.exec\(
```

## Go

```
fmt\.Sprintf.*%s.*SQL
exec\.Command\(.*\+
template\.HTML\(
template\.JS\(
```

## Java

```
Runtime\.getRuntime\(\)\.exec\(
ProcessBuilder.*\.command\(
ObjectInputStream
XMLDecoder
Statement.*execute.*\+
```

## Ruby

```
eval\(
system\(
exec\(
`.*#{
send\(.*params
constantize
```

## PHP

```
eval\(
exec\(
system\(
passthru\(
shell_exec\(
\$_GET\[
\$_POST\[
\$_REQUEST\[
include\s*\$
require\s*\$
unserialize\(
```
