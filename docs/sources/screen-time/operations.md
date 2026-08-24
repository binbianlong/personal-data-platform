# 運用

## 差分取得

通常処理:

```text
FSEventsでsegment作成・更新を検知
→ 新しいrecordをdecode
→ transitionを保存
→ watermarkを更新
```

保持するwatermark:

```text
device identifier
stream
segment filename
最後に処理したrecord offset
最後に処理したevent timestamp
```

iCloud同期により過去時刻のeventが後から届く可能性があるため、watermark以降だけを永久に読む方式にはしない。

## 補正

- 毎日、存在する全segmentのhashを確認する。
- hashが変わったsegmentを先頭から再decodeする。
- `event_key`で重複排除する。
- 未完了intervalを後から到着したend eventで更新する。

実測では約4週間分のsegmentが残っていた。Appleの保証値ではないため、変更検知に加えて日次再走査を行う。

## 異常判定

次をエラーとして扱う。

```text
Biome directoryを読めない
sync.dbを開けない
SEGB containerをdecodeできない
protobuf payloadをdecodeできない
未知fieldまたはdecode失敗が急増する
24時間scanが成功しない
```

新しいeventがないことだけでは、端末未使用と障害を区別できない。directoryへのアクセス、scan完了、decode結果を使って稼働判定する。
