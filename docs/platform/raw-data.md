# Rawデータ契約

## System of record

GCSを直近60日間のRaw System of Recordとする。Rawはsourceから取得した内容をlosslessに保持するが、
長期分析履歴はMotherDuckが保持する。60日を超えたRawは復元できず、MotherDuckの全損時に全期間を
Rawだけから再構築できるとは保証しない。

Raw objectは次の性質を持つ。

- applicationから上書きしないimmutable objectである。
- GCSへのupload完了時刻から60日でLifecycle Deleteの対象になる。
- 圧縮や転送前のsource bytesに対するSHA-256を持つ。
- object keyだけからsource、schema version、logical scope、観測順、content identityを復元できる。
- Rawごとのsidecar metadata JSONと永続Parquet中間層は作らない。

Collectorの稼働確認用control objectはsource Raw prefix内の予約済み`_control/` subprefixに置く。device別の
scan receiptとactive-device manifestはmutableであり、再構築の入力には使わない。source固有のkeyと本文は
sourceのデータモデルで定義する。

GCS bucketの公開範囲、暗号化、credentialは[`security.md`](security.md)に従う。

## 保持期限

production bucketは`us-central1`のStandardを使い、`raw/screen_time/v1/`で始まり`.segb.gz`で終わる
objectだけを`age=60`のLifecycle Delete対象にする。`age`は`observed_at`ではなくGCS upload完了時刻から
数える。60日間はStandardのまま保持し、ColdlineまたはArchiveへのstorage-class遷移は行わない。Soft Deleteは
無効にするため、Lifecycle actionが実行された後のobjectは復元できない。

Lifecycle actionは非同期であり、60日ちょうどでの削除を保証するprovider SLAはない。ReconciliationはGCS作成時刻を
`ops.ingestion_metadata.storage_created_at`へ記録し、60日より前の欠損と63日を超えた残存を失敗にする。63日は
このprojectが異常を検知するために置く運用SLOであり、GCSの削除時刻保証ではない。60日以降に消えた取込成功済み
objectは予定された期限切れとして`retention_expired_at`を記録する。control JSONは`.segb.gz`ではないため
Lifecycle Delete対象外である。

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

Collectorまたは取得処理は、upload予定のobject keyをlocal stateへ先に永続化する。GCSがupload成功を
返した後だけ、直前hashとwatermarkを進める。途中で停止した場合は、次回も同じobject keyで再開する。

同じkeyへのretryは同一bytesでなければならない。Collectorはlocal stateへ永続化した同じkeyとgzip bytesを
再送する。write-only credentialを使うため、upload前にGCSの既存objectをreadして比較することはない。
異なる内容を同じkeyへ保存してはならず、取込時のSHA-256不一致はLoaderで検出して成功取込を拒否する。

## 検証と再生

Loaderはlistingで得たGCS generationを指定して同じobject incarnationをdownloadし、object keyから得た期待
SHA-256と展開後bytesから再計算した値を照合する。不一致、対象generationの消失、不正な圧縮、decode不能の
objectはbaseへ書き込まず、失敗状態を記録する。Collectorはupload前に圧縮済みbytesのCRC32Cを計算してGCSへ渡す。

GCSのlistingは全pageを走査する。再生順は`(observed_at, object_key)`の昇順とし、同じ時刻の観測も
決定的に処理する。GCS listingの返却順へ依存してはならない。

Rawの存在と分析への取込成功は別の状態として扱う。GCS objectが存在しても、MotherDuckの
`ops.ingestion_metadata`が同じGCS generationで`succeeded`になるまでは取込完了とみなさない。
