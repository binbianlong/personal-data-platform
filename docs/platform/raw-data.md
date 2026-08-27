# Rawデータ契約

## System of record

Backblaze B2をRawのSystem of Recordとする。Rawはsourceから取得した内容をlosslessに保持し、
MotherDuckの全損時にもB2だけから分析層を再構築できなければならない。

Raw objectは次の性質を持つ。

- applicationから上書きしないimmutable objectである。
- 保持期限は設けない。削除は通常処理から分離した明示的な管理操作だけで行う。
- 圧縮や転送前のsource bytesに対するSHA-256を持つ。
- object keyだけからsource、schema version、logical scope、観測順、content identityを復元できる。
- Rawごとのsidecar metadata JSONと永続Parquet中間層は作らない。

Collectorの稼働確認用control objectはsource Raw prefix内の予約済み`_control/` subprefixに置く。この
scan receiptはmutableであり、再構築の入力には使わない。source固有のkeyと本文はsourceのデータモデルで
定義する。

B2 bucketの公開範囲、暗号化、credentialは[`security.md`](security.md)に従う。

## Observation

Raw objectは「contentそのもの」ではなく、あるlogical scopeをある時点で観測した事実を表す。
`observed_at`は取得完了時点のUTC時刻とし、object keyへ含める。

重複排除は、同じlogical scopeで直前に保存を完了した観測と比較して行う。

```text
A -> 保存
A -> 直前と同一なのでskip
B -> 保存
A -> 直前はBなので新しいobserved_atで保存
```

比較にはcontent SHA-256に加えて、sourceがoperationを持つ場合はoperationも含める。
過去のどこかに同じhashがあることだけを理由にskipしてはならない。

source固有のlogical scope、object key、圧縮形式は各sourceのデータモデルで定義する。
Screen Timeは[`data-model.md`](../sources/screen-time/data-model.md)を正本とする。

## 永続化境界

Collectorまたは取得処理は、upload予定のobject keyをlocal stateへ先に永続化する。B2がupload成功を
返した後だけ、直前hashとwatermarkを進める。途中で停止した場合は、次回も同じobject keyで再開する。

同じkeyへのretryは同一bytesでなければならない。既存objectのbytesまたはSHA-256が異なる場合は
上書きせず、整合性エラーとして停止する。

## 検証と再生

Loaderはobject keyから期待SHA-256を取得し、展開後bytesから再計算した値と照合する。不一致、
不正な圧縮、decode不能のobjectはbaseへ書き込まず、失敗状態を記録する。

B2のlistingは全pageを走査する。再生順は`(observed_at, object_key)`の昇順とし、同じ時刻の観測も
決定的に処理する。B2 listingの返却順へ依存してはならない。

Rawの存在と分析への取込成功は別の状態として扱う。B2 objectが存在しても、MotherDuckの
`ops.ingestion_metadata`が`succeeded`になるまでは取込完了とみなさない。
