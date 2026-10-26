# アーキテクチャ

Screen TimeはMac、OwnTracksはGCP `us-west1`でRawを保存し、FitbitはAPIから直接取り込む。分析履歴はMotherDuck `us-west-2`へ保存する。

```text
Screen Time Collector -> SQLite Raw -> Loader -> MotherDuck -> stream heartbeat
OwnTracks -> receiver -> GCS Raw -> Pub/Sub push -> 非公開worker -> Loader -> MotherDuck
Google Health Webhook -> receiver -> Pub/Sub queue -> 毎時workerがpull
  -> API完全取得 -> MotherDuckへ直接書込 -> DB commit後ACK

MotherDuck base -> dbt View -> read-only share / Remote MCP
日次Job -> Fitbit補修 -> dbt -> OwnTracks Raw補修 -> Macの取込heartbeat・DB監査 -> Healthchecks
```

| source / stream | 取得元 | Raw・復旧元 |
|---|---|---|
| `screen_time / app-in-focus` | Macへ同期されたiPhoneのApp.InFocus | v1/v2 SEGBをMacのSQLiteへ保存 |
| `screen_time / app-usage` | Mac自身のScreenTime.AppUsage | v1/v2 SEGBをMacのSQLiteへ保存 |
| `fitbit / health` | Google Health API | Rawなし。API再取得 |
| `owntracks / location` | iPhone OwnTracks HTTP | 正規化JSON gzipをGCSへ保存 |

receiverは認証と通知・Raw保存までを担当し、API取得やDB書込はworkerが行う。
時刻・lease・監視・復旧は[Platform運用](operations.md)、アクセス境界は[セキュリティ](security.md)を参照する。

## 共通処理とsourceの責任

共通処理はRawの検証・取込台帳・transaction・再試行を、各`src/personal_data_platform/sources/<source>/`は
取得・wire format・正規化・baseへの書込・control状態を所有する。日境界・集計・source横断JOINはdbt Viewへ置く。

Raw adapterは一つのsource/streamを担当し、保持中の全Raw schema版を再生できるようにする。
FitbitはRaw adapterへ登録せず、API取得と直接Writerで処理する。dbt selectorはRaw adapterから独立して解決する。
Raw schema版とparser versionは別で、parser versionを更新すると保持中の旧parser成功分も再解析する。
共通処理は他sourceへCollectorのscan記録を要求せず、未知のsource/streamを既存decoderへ流すfallbackを設けない。

## Rawと保持期限

- Rawはcreate-onlyの決定的gzip。展開後bytesのSHA-256と観測時刻をkeyへ含め、同じkeyのretryには同じbytesを使う。
- 連続する同一内容は省くが、`A -> B -> A`を過去のhashだけで除外しない。
- Screen Timeは各元ファイルの最新成功版と未取込分をSQLiteへ保持する。保存期限はなく、Biomeから消えたファイルも残す。
- FitbitのAPIレスポンスはメモリだけで扱い、Rawを永続保存しない。
- OwnTracksの位置RawはGCSに無期限保存する。

MotherDuckはRaw整理後も分析履歴を保持し、OwnTracks以外の全期間のDB復元をRawだけで保証しない。
key・payload・重複排除条件は[Screen Time](../sources/screen-time/data-model.md)、
[Fitbit](../sources/fitbit/data-model.md)、[OwnTracks](../sources/owntracks/data-model.md)のデータモデルを参照する。

## Loaderと永続化

Loaderは選択scopeのRawをgeneration固定で読み、gzip展開・SHA-256検証後にdecodeする。
Raw identity・generation・parser versionが一致する成功だけをskipする。
型付き行、sourceの補助状態、取込成功はWarehouseが所有する1 transactionでcommitする。
書込失敗はrollbackして別transactionに失敗記録を残す。commit結果不明・rollback失敗では接続を閉じ、
後続Rawや失敗記録を書き込まず、再接続後の台帳から再試行を判断する。
Fitbitは完全取得範囲を直接commitし、結果不明では接続を破棄して再配信時にAPIを再取得する。

Macはローカル保存後に同じprocessでLoaderを実行し、DB成功確認後に旧成功Raw・対応する旧成功台帳を整理する。
pending・失敗・不完全snapshotがある回はstream heartbeatを更新しない。
Raw単位の再試行には取込台帳、Macの鮮度確認にはstream heartbeat、日次結果には`ops.job_run`を使う。
DBと外部HTTPはatomicではないため、外部送信失敗はJob失敗として扱う。

## Source・stream追加手順

1. `sources/<source>/`へ取得・型付きbatch・監査を実装する。Rawを保存するsourceはcodecとRaw adapterを`sources/registry.py`へ登録する。
2. dbt selectorと必須relationを定義し、Rawを保存する場合は全prefix/schema、parser version、保持条件と保存先種別も定義する。
3. baseの変更は新しいforward migrationへ追加する。適用済みSQLを書き換えず、batch内でcommitしない。
4. dbtのmodel/testへselector用tagを付け、scope混入拒否、再実行、rollback、generation固定を検証する。
5. Terraformで取得identity・実行経路・監視と、必要な場合だけRaw権限・Lifecycleを設定し、[DB更新手順](operations.md#dbの初期化と更新)で有効にする。
