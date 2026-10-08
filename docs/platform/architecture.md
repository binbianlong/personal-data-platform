# アーキテクチャ

Screen TimeはMac、FitbitはGCP `us-west1`で取得・Raw保存を行い、MotherDuck `us-west-2`へ長期分析履歴を保存する。

```text
Screen Time Collector -> SQLite Raw -> Loader -> MotherDuck -> stream heartbeat
Google Health Webhook -> receiver -> Pub/Sub -> 毎時Job
  -> API完全取得 -> 変更時だけGCS Raw -> Loader -> DB commit後ack

Loader -> MotherDuck base -> dbt View -> read-only share / Remote MCP
日次Job -> Fitbit補修 -> dbt -> Macの取込heartbeat・DB監査 -> daily heartbeat
```

| source / stream | 取得元 | Raw |
|---|---|---|
| `screen_time / app-in-focus` | Macへ同期されたiPhoneのApp.InFocus | v1/v2 SEGB |
| `screen_time / app-usage` | Mac自身のScreenTime.AppUsage | v1/v2 SEGB |
| `fitbit / health` | Google Health API | v3 JSON |

receiverは通知の認証とPub/Sub発行だけを担当し、API取得やDB書込を行わない。
毎時・日次Jobは同じimageとruntime Service Accountを共有する。時刻・lease・監視・復旧は
[Platform運用](operations.md)、アクセス境界は[セキュリティ](security.md)に従う。

## 共通処理とsourceの責任

実装は`src/personal_data_platform/`内に置く。

| 境界 | 責任 |
|---|---|
| `sources/contracts.py`・`sources/registry.py` | 登録済みsource/stream、Raw codec、decode、型付きbatch、監査、保持期限、dbt selector |
| `storage/gcs.py` | 全page listing、namespace検証、generation指定read、create-only upload |
| `loader/`・`storage/motherduck.py` | hash検証、再試行、object単位transaction、共通取込台帳 |
| `reconciliation/`・`recovery/` | Rawと台帳の照合、期限切れ判定、scratchへの再生 |
| `sources/<source>/` | 認証・取得、control/pending状態、wire format、正規化、baseへの書込 |
| `dbt/` | interval、日境界、日次集計、source横断JOINをViewとして提供 |

adapterは一つのsource/streamを担当し、保持中の全Raw schema版を再生できるようにする。
Raw schema版とparser versionは別で、parser versionを更新すると保持中の旧parser成功分も再解析する。
取得状態・controlの意味はsourceが所有し、共通処理は他sourceへCollectorのreceiptを要求しない。
未知のsource/streamを既存decoderへ流すfallbackは設けない。

## Rawと保持期限

- Rawはcreate-onlyの決定的gzip。展開後bytesのSHA-256と観測時刻をkeyへ含め、同じkeyのretryには同じbytesを使う。
- 連続する同一内容は省くが、`A -> B -> A`を過去のhashだけで除外しない。source固有の比較条件は各データモデルに従う。
- Screen Timeは各元ファイルの最新成功版と未取込分だけを既存SQLiteに保持する。保存期限はなく、Biomeから消えたファイルも残す。
- Fitbit v3の`.json.gz`はGCSで90日保持する。control JSONはLifecycle削除対象へ混ぜない。
- GCSの保持起点は`retention_started_at`、なければ`storage_created_at`。Soft DeleteとObject Versioningは無効。
- GCSの期限前欠損、保持起点不明、93日を超える残存は監査失敗。90日ちょうどの削除は保証しない。

Rawは保存中の版の再生用正本で、MotherDuckはRaw整理後も分析履歴を保持する。
全期間のDB復元をRawだけで保証しない。keyとpayloadは
[Screen Timeデータモデル](../sources/screen-time/data-model.md)と[Fitbitデータモデル](../sources/fitbit/data-model.md)を参照する。

## Loaderと永続化

Loaderは選択scopeの全prefix・全pageを走査し、重複key、namespace外、未対応schema、keyとmetadataの不一致を拒否する。
`(observed_at, object_key)`順にgenerationを固定して取得し、gzip展開・SHA-256検証後にdecodeする。
`ops.ingestion_metadata`のRaw identity・generation・parser versionが一致する成功だけをskipする。

型付き行、sourceの補助状態、取込成功はWarehouseが所有する1 transactionでcommitする。
書込失敗はrollbackして別transactionに失敗記録を残す。commit結果不明・rollback失敗では接続を閉じ、
後続Rawや失敗記録を書き込まず、再接続後の台帳から再試行を判断する。
Fitbitは手元の保存bytesをLoaderへ渡し、保存済みRawの再生で取得後の障害から回復する。

Macは全対象のローカル保存後に既存Loaderを同じprocessで実行し、DB成功確認後に旧成功Rawを整理する。
pending・失敗・不完全snapshotがある回はstream heartbeatを更新しない。日次JobはMacの成功記録とDBを監査する。
DBと外部HTTPはatomicではないため、外部送信失敗はJob失敗として扱う。

## Source・stream追加手順

1. `sources/<source>/`へ取得・codec・型付きbatch・監査を実装し、`sources/registry.py`へ登録する。
2. 保持中の全Raw prefix/schema、parser version、必須relation、保持期限、dbt selectorを定義する。
3. baseの変更は新しいwest forward migrationへ追加する。適用済みSQLを書き換えず、batch内でcommitしない。
4. dbtのmodel/testへselector用tagを付け、scope混入拒否、再実行、rollback、generation固定を検証する。
5. Terraformで取得identity・Raw権限・Lifecycle・定期処理・監視を設定し、[DB更新手順](operations.md#dbの初期化と更新)で有効にする。

独自UI/MCP server、ZIP取り込み、RawのObject Lock、永続Parquet中間層は設けない。
