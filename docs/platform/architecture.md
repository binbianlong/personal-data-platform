# アーキテクチャ

Screen TimeはMac、OwnTracksはGCP `us-west1`でRawを保存し、FitbitはAPIから直接取り込む。分析履歴はMotherDuck `us-west-2`へ保存する。

```text
iPhone Screen Time Collector -> 専用SQLite Raw -> Loader -> MotherDuck -> source heartbeat
Mac Screen Time Collector -> 専用SQLite Raw -> Loader -> MotherDuck -> source heartbeat
OwnTracks -> receiver -> GCS Raw -> Pub/Sub push -> 非公開worker -> Loader -> MotherDuck
Google Health Webhook -> receiver -> Pub/Sub queue -> 毎時workerがpull
  -> API完全取得 -> MotherDuckへ直接書込 -> DB commit後ACK

MotherDuck base -> dbt View -> read-only share / Remote MCP
日次Job -> Fitbit補修 -> dbt -> OwnTracks Raw補修 -> Macの取込heartbeat・DB監査 -> Healthchecks
```

| source / stream | 取得元 | Raw・復旧元 |
|---|---|---|
| `iphone_screen_time / app-in-focus` | Macへ同期されたiPhoneのApp.InFocus | SEGBをMacのSQLiteへ保存 |
| `mac_screen_time / app-usage` | Mac自身のScreenTime.AppUsage | SEGBをMacのSQLiteへ保存 |
| `fitbit / health` | Google Health API | アプリ側データをAPIで再取得。PDP内のRaw保存なし |
| `owntracks / location` | iPhone OwnTracks HTTP | 正規化JSON gzipをGCSへ保存 |

receiverは認証と通知・Raw保存までを担当し、API取得やDB書込はworkerが行う。
時刻・lease・監視・復旧は[Platform運用](operations.md)、アクセス境界は[セキュリティ](security.md)を参照する。

## 共通処理とsourceの責任

共通処理はRawの検証・取込台帳・transaction・再試行を、各`src/personal_data_platform/sources/<source>/`は
取得・wire format・正規化・source固有の保存ルールを所有する。日境界・集計・source横断JOINはdbt Viewへ置く。

取込・監査・ジョブの制御は`storage/warehouse.py`のWarehouse契約を使い、接続や任意SQLを扱わない。
batchは型付きデータのみを持ち、source固有のSQLは各sourceの`motherduck.py`へ置く。
MotherDuck実装がsourceの保存処理を呼び出し、transactionの開始・rollback・commitを管理する。
接続生成とdbtの認証設定は`storage/factory.py`へ集約し、接続とdbtに同じ設定を渡す。

Raw adapterは一つのsource/streamを担当し、保持中の全Raw schema版を再生できるようにする。
FitbitはRaw adapterへ登録せず、API取得したsnapshotをWarehouseの操作で保存する。dbt selectorはRaw adapterから独立して解決する。
Raw schema版とparser versionは別で、parser versionを更新すると保持中の旧parser成功分も再解析する。
共通処理は他sourceへCollectorのscan記録を要求せず、未知のsource/streamを既存decoderへ流すfallbackを設けない。

iPhone・Macのsourceはそれぞれ設定、取得、decoder、Adapter、writer、CLIとLaunchAgentを所有する。
`biome/`はSEGB、Raw v2 envelope、疑似化、SQLite archive、収集engineなどsource名を判断しない共通処理を持つ。
相互sourceへの依存はなく、一方のscan・取込・監査・heartbeat失敗は他方の状態を変更しない。
書込は他sourceと同じ`loader` leaseで直列化する。横断分析はトップレベルの`dbt/models/cross_source/`へ置く。

## Rawと保持期限

- Rawはcreate-onlyの決定的gzip。展開後bytesのSHA-256と観測時刻をkeyへ含め、同じkeyのretryには同じbytesを使う。
- 連続する同一内容は省くが、`A -> B -> A`を過去のhashだけで除外しない。
- Screen Timeは各元ファイルの最新成功版と未取込分をSQLiteへ保持する。保存期限はなく、Biomeから消えたファイルも残す。
- Fitbitはアプリ側のデータをAPIで取得する。PDP内ではAPIレスポンスをメモリだけで扱い、Rawを永続保存しない。
- OwnTracksの位置RawはGCSに無期限保存する。

MotherDuckはRaw整理後も分析履歴を保持し、OwnTracks以外の全期間のDB復元をRawだけで保証しない。
key・payload・重複排除条件は[iPhone Screen Time](../sources/iphone-screen-time/data-model.md)、
[Mac Screen Time](../sources/mac-screen-time/data-model.md)、
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
3. baseの変更は新しいforward migrationへ追加し、MotherDuck実装からsourceの保存処理を呼び出す。適用済みSQLを書き換えず、sourceの保存処理ではcommitしない。
4. dbtのmodel/testへselector用tagを付け、scope混入拒否、再実行、rollback、generation固定を検証する。
5. Terraformで取得identity・実行経路・監視と、必要な場合だけRaw権限・Lifecycleを設定し、[DB更新手順](operations.md#dbの初期化と更新)で有効にする。

## 保存先の追加

新しい保存先はWarehouse契約の実装、sourceごとの保存処理、接続設定とdbtの対応を追加する。
現在のDDLとdbt SQLはMotherDuck用であり、新しい保存先での排他・原子的な確定・訂正と削除の検証が必要となる。
移行する履歴は、保持中のRaw・Fitbitのアプリ側データから再取得できる範囲と、既存DBの履歴を確認して決める。
