# Personal Data Platform — 設計仕様

日付: 2026-08-20

## 1. 目的

低コスト・高い復旧性・不要なインフラを増やさないことを重視し、長期間運用できる個人データ基盤を構築する。

主な目的:

- 正本となるRawデータを長期保存する。
- MotherDuckの公式連携を通じて、ChatGPTから分析データを容易に参照できるようにする。
- イベント通知に対応しているデータソースでは、準リアルタイムに近い取り込みを行う。
- 分析層が失われても再構築できるようにする。
- 現在の要件に見合った運用複雑性に抑える。
- YAGNIを徹底し、materialization・追加DB・追加イベント基盤などは、実測された負荷や遅延の問題が発生してから導入する。

初期スコープではUI / Webアプリを作らない。

## 2. 全体アーキテクチャ

```text
Google Health
    ↓ Webhook
Cloud Run Receiver
    ↓
Cloud Tasks
    ↓
Fetch Worker
    ↓
Backblaze B2 Raw
    ↓
Cloud Tasks
    ↓
Loader
    ↓
MotherDuck base
    ↓
MotherDuck marts（原則View）
    ↓
MotherDuck公式ChatGPT連携
    ↓
ChatGPT Chat

Pollingのみ対応するクラウドデータソース
    ↓
定期Collector Job
    ↓
B2 Raw

端末ローカルのデータソース
    ↓
Local Collector
    ↓
B2 Raw
```

整合性・復旧経路:

```text
Cloud Scheduler
    ↓
Daily Reconciliation Job
    ├─ Source ↔ B2 の照合
    ├─ B2 ↔ MotherDuck の取込状況照合
    ├─ 自動Backfill
    └─ 成功heartbeat → Healthchecks.io

Cloud Logging / Monitoring
    ↓
明示的な障害をEmail通知

Healthchecks.io
    ↓
予定されたReconciliation heartbeatが来なければEmail通知
```

## 3. 主要技術選定

- クラウド実行・オーケストレーション: Google Cloud Platform
- HTTP / 実行基盤: Cloud Run
- Work Queue: Cloud Tasks
- 定期実行: Cloud Scheduler
- Raw System of Record: Backblaze B2
- 分析DB: MotherDuck
- 分析変換: dbt
- Secret管理: GCP Secret Manager
- Infrastructure as Code: Terraform
- Terraform state: GCS remote backend
- CI/CD: GitHub Actions
- GitHub Actions → GCP認証: Workload Identity Federation
- Container Registry: Artifact Registry
- アプリケーション言語: Python
- 外部Heartbeat監視: Healthchecks.io
- 分析UI: MotherDuck公式ChatGPT連携
- 初期UI / Webアプリ: なし

## 4. Rawデータ層

### 4.1 B2をSystem of Recordとする

B2には、データソースから取得した正本Raw payloadを保存する。

MotherDuckは再構築可能な分析DBであり、System of Recordにはしない。

Raw保存の性質:

- Immutable
- Lossless
- Content-addressed
- 完全に同一のRaw payloadは重複保存しない
- 初期段階では永続Parquet中間層を作らない
- 初期段階ではRawごとのsidecar metadata JSONを作らない

### 4.2 RawのHash

圧縮前の元response bytesに対してHashを計算する。

```text
API response bytes
    ↓
SHA-256
    ↓
重複確認
    ↓
gzip
    ↓
B2
```

SHA-256が表すものは、

「このRaw bytesが完全一致しているか」

である。

分析行の同一性を表すstable keyとは明確に分離する。

### 4.3 Object keyと観測順序

Raw bytesだけでは、空responseやDELETE後の状態について「どの論理範囲を取得した結果なのか」を復元できない。
また、状態が `A → B → A` と戻る場合、純粋なcontent hashだけで永久重複排除すると最後のAが再び観測された順序を失う。

そのため、Raw object keyには以下を含める。

- source
- data type
- logical range
- `observed_at`
- operation
- content hash

Google Healthの例:

```text
raw/<source>/<data_type>/<range_kind>/<start>_<end>/
  <observed_at>/<operation>/<hash-prefix>/<sha256>.<ext>.gz
```

例:

```text
raw/google_health/sleep/physical/
  20260820T000000Z_20260820T080000Z/
  20260820T085012123456Z/
  DELETE/ab/abcdef1234....json.gz
```

`observed_at` は取得完了時点のUTC時刻であり、Rawの状態遷移をB2だけから正しい順序で再生するために使う。
HTTP statusやretry回数のような運用metadataとは異なり、再構築に必要な順序情報なので永続化する。

`hash-prefix` はobject listingを分散するためだけに使う。

### 4.3.1 重複排除

重複排除は「同じHashが過去に一度でも存在したか」ではなく、同一logical rangeの**直前の保存済み観測**と比較して行う。

```text
同一rangeで A
↓ 保存

同一rangeで A
↓ 直前とoperation + SHA-256が同一
↓ 保存しない

同一rangeで B
↓ 保存

同一rangeで A
↓ 直前はB
↓ 新しいobserved_atで保存
```

これにより、無意味な連続重複は削減しつつ、`A → B → A` のような有意味な状態遷移は失わない。

`operation`も比較対象に含める。
同じbytesでも `UPSERT` と `SNAPSHOT` は意味が異なるため、operationが異なれば別観測として保存する。

### 4.3.2 operationの意味

- `UPSERT`: Webhookの追加・修正通知。Loaderは取得できたrecordをupsertする。
- `DELETE`: Webhookの削除通知。Loaderはlogical rangeを現在の取得結果で置き換える。
- `SNAPSHOT`: Reconciliationが独立取得した現在状態。Loaderはlogical rangeを現在の取得結果で置き換える。

`DELETE`と`SNAPSHOT`をrange-replaceとして扱うことで、WebhookのDELETE通知自体を取りこぼした場合でも、後続Reconciliationで削除状態を復元できる。

Raw replay時はobject keyの`observed_at`で古い順に処理する。

### 4.4 Metadata方針

B2へ永続保存するmetadataは最小限にする。

object pathから以下を復元できればよい。

- source
- data type
- logical range
- observation order (`observed_at`)
- change operation
- content identity

以下のような運用情報は、Rawと一緒に永久保存しない。

- HTTP status
- retry回数
- Cloud Task ID
- trigger種別
- request latency

これらはCloud Logging等の運用ログとして扱う。

## 5. Ingestionアーキテクチャ

### 5.1 Google Healthのイベント経路

```text
Google Health
    ↓ Webhook
Cloud Run Receiver
    ↓
Cloud Tasks
    ↓
Fetch Worker
    ↓
B2 Raw
```

Receiverの責務は薄く保つ。

行うこと:

- 受信requestを検証する
- Cloud Taskを作成する
- 速やかにresponseを返す

行わないこと:

- Google Health APIからデータ取得
- B2への保存
- MotherDuckへの書き込み
- dbt実行

### 5.2 Fetch Worker

責務:

1. データソースAPIからデータを取得する。
2. 元response bytesを保持する。
3. SHA-256を計算する。
4. 同一logical rangeの直前観測とoperation + SHA-256を比較する。
5. 連続重複でなければ`observed_at`を付け、gzipして新規objectとして保存する。
6. Raw保存成功後にLoader Taskを作成する。

ここでは分析用の変換を行わない。

### 5.3 Pollingデータソース

Webhook非対応のデータソースは、sourceごとの定期Collectorで取得する。

Polling頻度は全source共通にせず、sourceごとに決める。

例:

- SwitchBot
- 天気・外部環境API

Google HealthでWebhookが利用できる場合、通常経路では高頻度Pollingを行わない。

### 5.4 Local Collector

Screen Timeなど端末ローカルにしかないデータはローカルで取得する。

```text
Local Collector
    ↓
B2 Raw
```

Local CollectorはB2へ直接uploadする。

初期段階では専用Ingestion APIを作らない。

## 6. Loader

LoaderはRaw source payloadを型付きの分析用base tableへ変換する。

```text
B2 object
    ↓
gzip展開
    ↓
Raw parse
    ↓
typed record
    ↓
stable key
    ↓
MotherDuck base UPSERT
```

Loaderが担当するのは、安全にSQLで扱うために必要な構造変換までとする。

Loaderでは以下を行わない。

- source横断JOIN
- source横断の意味的正規化
- 分析集計
- 特徴量生成
- 日次集約
- 相関分析

これらはdbtが担当する。

## 7. MotherDuck base層

source × data typeごとに型付き物理tableを作る。

例:

```text
base.google_health_sleep
base.google_health_heart_rate
base.switchbot_environment
base.screen_time
base.location
```

各base tableには必要に応じて以下を持たせる。

- stable analytical key
- 型付きsource field
- source側record ID
- provenance用のraw object key

### 7.1 Stable key

行の同一性は以下の優先順位で決める。

1. sourceが安定したIDを提供する場合はそのIDを使う。
2. IDがない場合は `source + data type + natural key fields` から決定的keyを生成する。

可変のmeasurement value自体はstable keyに含めない。

例:

```text
同じ測定:
10:00 heart rate 72
後から 74 に訂正

stable_keyは同一
valueだけ 72 → 74
```

### 7.2 Upsert方針

base tableには最新の分析状態を持たせる。

source側で既存recordが修正された場合:

```text
B2
├─ 古いRaw payloadを保持
└─ 新しいRaw payloadも保持

MotherDuck base
└─ 同じstable_keyの行を最新値へ更新
```

つまり、

- B2 = 履歴・原本
- MotherDuck base = 現在分析すべき最新状態

と責務を分ける。

## 8. dbtとmarts

dbtは意味的な分析変換を担当する。

例:

- timezone統一
- source横断JOIN
- 派生指標
- 日次集約
- 特徴量生成
- 分析用途のschema生成

marts例:

```text
marts.daily_health
marts.sleep_analysis
marts.activity_analysis
```

### 8.1 Materialization方針

martsは原則Viewにする。

```text
base更新
    ↓
View query
    ↓
常に最新結果
```

Viewは現在のbaseをquery時に参照するため、データ取り込みごとにdbtを起動しない。

dbtはmodel定義をdeploy・変更するときに実行する。

### 8.2 将来のTable materialization

Viewとしてのqueryが重くなったmartだけ、将来tableまたはincremental tableへ変更する。

その段階で初めて、データ更新に連動したrefresh処理を導入する。

将来の推奨方式:

```text
base更新
    ↓
10秒固定coalescing window
    ↓
refresh taskを1件
    ↓
dbt job
```

この10秒windowは初期アーキテクチャでは使用しない。

これにより不要なdbt実行を避けつつ、将来の拡張経路を明確に残す。

## 9. 処理状態の管理

Orchestration stateのためだけにFirestoreやPostgreSQLを追加しない。

以下で管理する。

```text
B2
→ 正本Rawデータ

MotherDuck ingestion_metadata
→ Analyticsへ正常に取り込まれたRaw object

Cloud Tasks
→ 一時的な実行待ちworkとretry
```

`ingestion_metadata` の例:

- object_key
- object_hash
- source
- ingested_at
- status

MotherDuckのingestion metadata自体もB2から再構築可能とする。

## 10. Cloud Run実行モデル

1 repository・1 Python project・1 shared Docker imageを使い、役割別にCloud Runへ展開する。

Cloud Run Services:

```text
health-webhook
health-fetch
health-loader
```

Cloud Run Jobs:

```text
dbt-runner
reconciliation
```

Serviceはrequest-driven処理に使う。

Jobは開始から終了までが明確なbatch処理に使う。

shared imageにすることで依存管理を共有しつつ、runtimeを分離することで以下を維持する。

- 独立scale
- IAM分離
- 障害分離
- componentごとのCPU / Memory調整

## 11. Cloud Tasks

Cloud Tasksを主なwork queueとして使う。

用途:

- Webhook → Fetch Worker
- Raw保存 → Loader
- request-driven処理のretry

初期段階ではfan-out event busが不要なためPub/Subを導入しない。

将来table materializationを採用してdata-triggered dbt refreshが必要になった場合、Cloud Tasksで固定coalescing windowを実装できる。

## 12. Reconciliationと復旧

低頻度の独立Reconciliation Jobを毎日実行する。

目的はfreshnessではなくcorrectnessである。

実行順:

```text
1. Source ↔ B2
   → 直近範囲を`SNAPSHOT`として独立取得
   → Raw欠損・修正・削除を検出
   → 自動Backfill / range-replace

2. B2 ↔ MotherDuck ingestion metadata
   → 未処理Raw objectを検出
   → Loader再実行

3. 分析状態のfreshness / consistency確認
   → 必要なら修復

4. 全処理成功後のみ
   → Healthchecks.io heartbeat送信
```

### 12.1 Reconciliationが必要な理由

Webhookだけでは、そもそも届かなかったイベントを検知できない。

独立Reconciliationにより以下を検出する。

- Webhook subscription障害
- silent missed event
- Loader Task取りこぼし
- 分析状態のstale

### 12.2 MotherDuck全損時

MotherDuckは完全再構築可能にする。

```text
B2全走査
    ↓
gzip展開
    ↓
Loader
    ↓
base再構築
    ↓
ingestion metadata再構築
    ↓
dbt deploy/run
    ↓
marts再作成
```

Manual Backfillは通常運用ではなく、最終的な緊急手段として残す。

## 13. 監視

独立した2つの監視層を使う。

### 13.1 GCP内監視

Cloud Logging / Cloud Monitoringで以下を検知する。

- Cloud Run error
- Job failure
- Task retryの継続失敗
- infrastructure error

通知はEmailで送る。

### 13.2 外部Heartbeat

Daily Reconciliationが完全成功した後にのみHealthchecks.ioへheartbeatを送る。

設定したschedule + grace period内にheartbeatが来なければ、Healthchecks.ioからEmail通知する。

これにより、GCP pipeline自体が実行されなかったケースも検知できる。

## 14. SecretとIAM

本番SecretはGCP Secret Managerに保存する。

ローカル開発では`.env`を利用してよいが、Gitへcommitしない。

例:

```text
health-fetch
├─ Google Health credentials
└─ B2 credentials

health-loader
├─ B2 credentials
└─ MotherDuck token

dbt-runner
└─ MotherDuck token

reconciliation
├─ source credentials
├─ B2 credentials
├─ MotherDuck token
└─ Healthchecks.io ping URL
```

各Cloud Run Service / Jobは専用Service Accountを持ち、必要な権限だけ付与する。

## 15. Infrastructure as Code

Terraformを使う。

管理対象:

- Cloud Run Services
- Cloud Run Jobs
- Cloud Tasks queues
- Cloud Scheduler
- Secret Manager resources
- IAM
- Artifact Registry
- Monitoring / alerts
- Workload Identity Federation

Terraform stateはGCS remote backendに保存する。

GCS bucketはTerraform state専用であり、Rawデータ保存先は引き続きB2とする。

## 16. CI/CD

GitHub Actionsを使う。

GCP認証には長期Service Account KeyではなくWorkload Identity Federationを使う。

Pull Request時:

```text
lint / format
unit tests
contract tests
dbt compile / tests
terraform validate
terraform plan
選択したintegration tests
```

main branchへのdeploy時:

```text
Docker build
Artifact Registry push
Terraform apply
Cloud Run deploy
dbt model定義が変わった場合のみdbt deploy/run
```

## 17. テスト戦略

### Unit

- stable_key生成
- SHA-256
- gzip round-trip
- object key生成
- Raw → typed record parse
- Reconciliation差分判定

### Contract

- Google Health Webhook payload
- Google Health API response
- SwitchBot API response
- その他source固有payload

### Integration

- B2 upload / read
- MotherDuck insert / upsert
- ingestion metadata
- dbt model実行・query

### E2E

E2Eは少数にする。

代表例:

- fixture Webhook → Raw → base → mart query
- Loader Task欠落 → Reconciliation → 自動復旧

テスト構成は以下を基本とする。

```text
多数の高速Unit / Contract Test
    ↓
少数の実サービスIntegration Test
    ↓
ごく少数のE2E
```

## 18. Repository構成

Repository:

```text
personal-data-platform
```

推奨構成:

```text
src/
├─ receiver/
├─ fetch/
├─ collectors/
├─ loader/
├─ reconciliation/
├─ sources/
├─ storage/
└─ shared/

dbt/
├─ models/
└─ tests/

infra/
└─ terraform/

tests/
├─ unit/
├─ contract/
├─ integration/
└─ e2e/

docs/
└─ superpowers/
   └─ specs/
```

最初からplugin frameworkや深い抽象化レイヤーを作らない。

同じパターンが実際に複数回現れてから共通interfaceを抽出する。

## 19. 初期スコープから除外するもの

初期段階では以下を作らない。

- Web UI
- Custom ChatGPT API
- Custom MCP Server
- Firestore orchestration state database
- PostgreSQL orchestration state database
- Pub/Sub event bus
- 永続Parquet cache
- Apache Iceberg
- martsの標準materialization
- data-triggered dbt refresh
- Google Health高頻度Polling

これらは具体的な実測要件が出た場合のみ追加する。

## 20. Core Guarantees

初期設計では以下を保証目標とする。

1. downstreamの分析処理が壊れても、正本Rawは失われない。
2. 同一logical rangeで連続する同一operation・同一Raw responseは重複保存されず、状態が変化後に同じ内容へ戻った場合は新しい観測として保存される。
3. Loaderを再実行しても分析行が重複しない。
4. source側の訂正でMotherDuckを更新しつつ、過去Rawは保持できる。
5. MotherDuckをB2だけから完全再構築できる。
6. Webhook取りこぼしをReconciliationで最終的に検出できる。
7. Daily Reconciliationが失敗・未実行ならEmail通知される。
8. ChatGPTがMotherDuck公式連携を通じて最新の分析Viewを参照できる。
9. martsがViewである間は、データ取り込みごとに不要なdbtを実行しない。
10. TerraformとGitHub Actionsからインフラ・deployを再現できる。

## 21. 将来拡張のトリガー

以下のような実測問題が出た場合のみ永続Parquet層を追加する。

- small file数が非常に多くなる
- full rebuildに数時間かかる
- Raw全件再処理を頻繁に行う
- 複数engineが繰り返しRawを読む

以下の場合のみmartをmaterializeする。

- Viewの反復queryが実測上遅い・高コストになる

以下の場合のみ10秒data-triggered dbt coalescingを追加する。

- 少なくとも1つのmartがtable / incremental modelになり、base更新後のrefreshが必要になる

以下の場合のみOperational State DBを追加する。

- Cloud Tasks + MotherDuck ingestion metadataではworkflow stateが複雑すぎる

以下の場合のみCustom API / UIを追加する。

- ChatGPT + MotherDuck連携だけでは実際の利用フローを満たせなくなる
