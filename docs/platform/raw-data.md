# Rawデータ契約

## System of record

GCSをsourceごとの保持期間内のRaw System of Recordとする。iPhoneとMacのScreen Time、Fitbit API Rawは90日である。
Rawはsourceから取得した内容をlosslessに保持し、長期分析履歴はMotherDuckが保持する。Lifecycle削除後のRawは
復元できず、MotherDuckの全損時に全期間をRawだけから再構築できるとは保証しない。

Raw objectは次の性質を持つ。

- applicationから上書きしないimmutable objectである。
- 保持起点からsource契約の保持日数でLifecycle Deleteの対象になる。通常はupload完了時刻、移行コピーは元の保持起点を使う。
- 圧縮や転送前のsource bytesに対するSHA-256を持つ。
- object keyだけからsource、schema version、logical scope、観測順、content identityを復元できる。
- Rawごとのsidecar metadata JSONと永続Parquet中間層は作らない。

取得処理の稼働確認用control objectはsourceが所有し、再構築の入力には使わない。Screen TimeではRaw prefix内の
予約済み`_control/`へstream別のmutableなdevice別scan receiptとactive-device manifestを置く。controlのkey、本文、更新権限と
稼働監査はsourceのデータモデル・運用で定義し、共通Raw repositoryには要求しない。
FitbitにはGCS receiptや端末同期checkpointを設けない。

GCS bucketの公開範囲、暗号化、credentialは[`security.md`](security.md)に従う。

## 保持期限

production bucketは`us-west1`のStandardを使う。source / streamごとにadapterで対応Raw prefix、suffix、
`retention_days`、`lifecycle_grace_days`を定義し、Terraformの設定を一致させる。Lifecycleの`age`は
`observed_at`ではなくGCS upload完了時刻から数える。移行したScreen Time RawはGCS Custom-Timeにも
元の保持起点を保存し、`days_since_custom_time=90`でコピーによる延長を防ぐ。保持中のstorage-class遷移は行わず、Soft Deleteと
Object Versioningは無効にするため、Lifecycle action後のobjectは復元できない。

両Screen Time streamは`raw/screen_time/v1/`または`raw/screen_time/v2/`配下の`.segb.gz`だけを`age=90`のDelete対象とし、control JSONを除外する。
Fitbitは`raw/fitbit/v3/`配下の`.json.gz`を`age=90`のDelete対象とする。
追加sourceはRaw namespaceとsuffixを指定する。control objectへ削除条件を重ねず、他streamとRaw領域を共有する場合は保持日数を一致させる。
複数schema版を保持する場合は、再生に対応する全prefixをadapterとTerraformの両方へ含める。

Lifecycle actionは非同期で、保持日数ちょうどの削除を保証しない。取込台帳はGCS作成時刻を
`storage_created_at`、Custom-Timeを`retention_started_at`へ記録する。Reconciliationは
`retention_started_at`があればそれを、なければ`storage_created_at`を保持起点とし、保持期限より前の欠損と、保持日数にgrace日数を加えた時点の
残存を失敗にする。両Screen Time streamでは90日と3日を使い、93日目から残存を異常とする。これは運用SLOであり、GCSの
削除時刻保証ではない。保持期限以降に消えた取込成功済みobjectだけを予定された期限切れとして記録し、
当該streamの他の監査項目が失敗した場合は`retention_expired_at`を確定しない。
日次処理はstreamごとの監査・期限切れ記録を先にcommitし、その後に全体の成功記録と外部heartbeatへ進む。
後続段階や外部送信が失敗しても、既に確定したstreamの期限切れ記録は巻き戻さない。

TerraformはJobへ`PDP_RAW_RETENTION_DAYS`と`PDP_LIFECYCLE_GRACE_DAYS`を設定し、Raw prefix・suffixは
source adapterから選ぶ。`PDP_RAW_PREFIXES_JSON`や`PDP_RAW_SUFFIXES_JSON`を明示した場合も、
Loader、Reconciliation、Rebuildの環境entrypointはadapter定義との不一致をcloudアクセス前に拒否する。この照合はGCS上の実際の
Lifecycle設定をqueryするものではなく、実適用の確認はTerraform plan / apply後に別途行う。

## Observation

共通`RawObject`は`source_id`、`stream`、`schema_version`、`subject_key`、`logical_key`、`observed_at`、
`sha256`、`key`、`storage_created_at`、`storage_generation`と任意の`retention_started_at`を持つ。subjectとlogical keyの意味はsourceが定義する。

Raw objectは「contentそのもの」ではなく、あるlogical scopeをある時点で観測した事実を表す。
`observed_at`はsourceが定義する観測順位のUTC時刻で、object keyへ含める。
Screen Timeは取得完了時点、Fitbitの各取得単位は全ページ取得の開始時点を使う。
Fitbit Rawに複数単位をまとめた場合、objectの`observed_at`は含まれる単位の取得開始時刻の最大値となる。

重複排除は、同じlogical scopeで直前に保存を完了した観測と比較して行う。Fitbitは取得範囲の現在の
完全なcoverageと元API項目を含む内容を照合する。保存済みの未取込Rawを先に再試行し、
現在の内容と取得範囲が一致すると確認できた場合だけ新Rawを省略する。

```text
A -> 保存
A -> 直前と同一なのでskip
B -> 保存
A -> 直前はBなので新しいobserved_atで保存
```

比較にはcontent SHA-256に加えて、sourceがoperationを持つ場合はoperationも含める。
過去のどこかに同じhashがあることだけを理由にskipしてはならない。

logical scopeはsource / streamとsubject / logical keyで分離する。Raw schema版とdecoderのparser versionは
区別し、対応schema版の判定とkeyの復元はadapterが行う。共通保存形式はgzipとし、元のbytes、logical scope、
object keyは各sourceのデータモデルで定義する。
Screen Timeは[`data-model.md`](../sources/screen-time/data-model.md)を正本とする。
Fitbitのenvelopeと範囲更新は[`data-model.md`](../sources/fitbit/data-model.md)を正本とする。

## 永続化境界

Screen Time Collectorは、upload予定のobject keyをsource所有のstateへ先に永続化する。GCSがupload成功を
返した後だけ、直前hashとwatermarkを進める。途中で停止した場合は、次回も同じobject keyで再開する。

同じkeyへのretryは同一bytesでなければならない。GCSのcreate-only uploadは`if_generation_match=0`を使う。
Screen Time Collectorはlocal stateへ永続化した同じkeyとgzip bytesを
再送する。write-only credentialを使うため、upload前にGCSの既存objectをreadして比較することはない。
異なる内容を同じkeyへ保存してはならず、取込時のSHA-256不一致はLoaderで検出して成功取込を拒否する。

Fitbitは完全取得した単位をGCSへcreate-onlyで書き、確定したkey/generationと同じRaw bytesをLoaderへ渡す。
DB commitを確認した単位だけackする。取得予定やRaw保存前のintentは永続化せず、保存前の失敗はPub/Subから再取得する。
Raw保存後の障害はGCSと共通取込台帳を照合し、未取込Rawの再生で復旧する。
変更なしと確認してRawを省略した期間は、元のRawが期限切れになればRawだけでは完全再構築できない。

## 検証と再生

Loaderはlistingで得たGCS generationを指定して同じobject incarnationをdownloadし、object keyから得た期待
SHA-256と展開後bytesから再計算した値を照合する。不一致、対象generationの消失、不正な圧縮、decode不能の
objectはbaseへ書き込まず、失敗状態を記録する。Collectorはupload前に圧縮済みbytesのCRC32Cを計算してGCSへ渡す。

GCSのlistingは選択scopeの対応prefixごとに全pageを走査する。keyの重複、要求prefix外の返却、未対応schema版、
source / streamまたはkey由来のidentityとmetadataの不一致は処理前に拒否する。再生順は`(observed_at, object_key)`の昇順とし、同じ時刻の観測も
決定的に処理する。GCS listingの返却順へ依存してはならない。

Rawの存在と分析への取込成功は別の状態として扱う。GCS objectが存在しても、MotherDuckの
`ops.ingestion_metadata`が同じRaw identityとGCS generationで`succeeded`になるまでは取込完了とみなさない。
