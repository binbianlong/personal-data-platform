# アーキテクチャ

## 構成

```text
Source固有の取得処理
  -> GCS Raw（source固有のkey、schema version、保持期限）
  -> source / stream別Cloud Run Loader Job
  -> MotherDuckの型付きbase
  -> dbt View
  -> MotherDuck Remote MCP（read-only）
  -> ChatGPT

source / stream別Cloud Run Reconciliation Job
  -> sourceの稼働状況 / GCS / MotherDuck / 必須relationを照合
  -> 成功時だけ対応する外部monitorへheartbeat
```

実データの取得まで実装しているのは、Macへ同期されたiPhoneの
[`Screen Time App.InFocus`](../sources/screen-time/)だけである。実行単位は
`source_id=screen_time`、`stream=app-in-focus`で、Raw schema v1/v2を扱う。
Mac自身のScreen TimeとFitbitの取得処理・decoder・分析modelは未実装である。
複数source、同じsource内の別stream、複数schema versionを扱う共通処理はsynthetic fixtureで検証する。

単一GCP project内で本番と検証を運用する。本番とは別のRawを使う検証ではGCS bucket、MotherDuck database、
Service Accountとcredentialを分離する。各sourceのRaw prefixは契約で固定し、prefixの変更で環境を切り替えない。
接続確認用preflightは専用bucketの`test/preflight/`と検証用databaseを使う。

## 共通処理とsourceの責任

| 境界 | 責任 | 実装 |
|---|---|---|
| Raw identity | source、stream、schema版、subject、logical key、観測時刻、内容hash、GCS generation | `raw/models.py` |
| Source adapter | key codec、対応schema版、decode、型付きbatch、稼働監査、必須relation、保持期限、dbt selector | `sources/contracts.py` |
| Registry | 明示登録されたsourceとstreamの組合せを解決 | `sources/registry.py` |
| GCS | 全page listing、選択namespaceの検証、generation指定read、create-only upload | `storage/gcs.py` |
| Loader / Warehouse | hash検証、順序制御、再試行、transaction、共通取込状態 | `loader/`、`storage/motherduck.py` |
| Reconciliation / Rebuild | 選択scopeの照合・期限切れ判定、固定inventoryのscratch再生 | `reconciliation/`、`recovery/` |
| 取得・型付きデータ | source固有の認証、取得state、control object、decoder、baseへの書込 | `sources/<source>/` |

実装pathは`src/personal_data_platform/`からの相対pathである。sourceの追加はregistryへの明示登録で行い、
外部pluginの自動探索や、未登録sourceを既存decoderへ流すfallbackは行わない。

Source adapterは一つの`source_id / stream`を担当し、そのstreamの複数schema versionを同時に受け付けられる。
Loader、Reconciliation、Rebuildは対応する全Raw prefixを走査し、source・stream・schema版とkeyから復元した
identityが一致することを確認する。別sourceまたは別streamの取込状態を、選択scopeの失敗数や期限切れ対象へ
混ぜない。schema版が増えても古いRawを再生できるよう、保持中のversionのdecoderを残す。

取得側のcheckpoint、cursor、pending upload、control objectの意味はsourceが持つ。共通repositoryはRawの
list / generation指定readだけを要求し、iPhoneのdevice manifestやscan receiptを他sourceへ要求しない。
source固有の稼働監査は`SourceHealth`として成功可否と詳細を共通Reconciliationへ返す。

## データ境界

- GCS Rawを保持期間内の再生可能な正本、MotherDuck baseを長期分析履歴とする。
- 取得処理はRawの保存までを担当し、分析のinterval生成や日次集計を行わない。
- Source adapterはRawを型付きbatchへdecodeし、Warehouseがobject単位のtransactionを管理する。
- Screen Timeは取り込み側で重複・削除を判定し、MotherDuckにはイベントを1件ずつ保存する。
  判定状態は原文を含まないSQLite差分状態としてGCSの専用control領域に保持する。
- dbtはinterval、日境界、集計、source横断JOINをViewとして提供する。
- ChatGPTは分析用Viewだけをread-onlyで参照する。

詳細は[`raw-data.md`](raw-data.md)、[`analytics.md`](analytics.md)、[`security.md`](security.md)を正本とする。
source間の処理分離はアプリケーションの契約である。同じRaw bucketとMotherDuck databaseを使うruntimeの
アクセス権自体がsourceごとに完全分離されることを意味しない。

## Core guarantees

1. downstreamが失敗しても、sourceの保持期間内に残るGCS Rawを再試行できる。iPhone Screen Timeは90日保持とする。
2. 取得処理は同一scopeの無意味な連続重複を省きつつ、`A -> B -> A`の観測順序を保持する。
3. 同じRaw identityとgenerationの再実行で分析行を重複生成しない。
4. 後着・訂正の分析上の扱いはsourceの型付きmodelで定義し、取込済み履歴をMotherDuckへ保持する。
5. GCSに残る選択source / streamの保持範囲を、明示的なpartial historyとしてscratch databaseへ再構築できる。
6. 選択scopeの全監査項目が成功した後だけ対応する外部heartbeatを送信する。確定順序と制約は
   [`analytics.md`](analytics.md)に従う。
7. iPhone Screen Timeは毎時Loaderを実行し、upload完了から2時間以内の分析View反映を通常時のfreshness基準とする。

## Source・stream追加手順

1. `docs/sources/<source>/`に取得対象、Raw identity、型付きmodel、運用契約を定義する。
   同じsourceの別streamでも、取得stateとcontrol objectの所有範囲を分ける。
2. `sources/<source>/`へ取得処理とadapterを実装し、`sources/registry.py`へ`source_id / stream`を登録する。
   対応schema versionと全prefix、decode、型付きbatch、稼働監査、保持期限、dbt selectorを定義する。
3. 新しい型付きbaseは既存SQLを書き換えずforward migrationで追加する。batchの書込はWarehouseが開始した
   transaction内で行い、batch自身でcommit / rollbackしない。
4. dbtにscope別modelとtestを追加し、adapterのselectorで対象のmodelとtestを実行できるようtagを付ける。
   source横断分析は各sourceのbaseが存在することを別途前提にする。
5. fixtureで別source・別stream・schema版の混入拒否、object単位rollback、監査の分離、rebuildのgeneration固定を
   検証する。実providerの認証、取得、更新、停止検出はfixture検証とは別に受け入れる。
6. [`Terraform runtime`](../../infra/terraform/)へ追加pipelineを設定し、取得identity、Raw create権限、保持期限、
   cron、専用heartbeatを用意する。source独自のcontrol更新権限やprovider認証は取得方式に合わせて追加する。
7. [`DBの初期化と更新`](operations.md#dbの初期化と更新)に従い、既存runtimeをすべて更新して旧実行の終了を確認した後に、
   新sourceの取得と定期実行を有効にする。

## 対象外

- Webhook、Cloud Tasks、常駐Cloud Run Service
- 独自UI、独自MCP server、データ更新ごとのdbt実行
- RawのObject Lock、永続Parquet中間層

Screen Time固有の対象範囲は[`Screen Time仕様`](../sources/screen-time/)を正本とする。
