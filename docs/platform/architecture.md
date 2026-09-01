# アーキテクチャ

## 初期core

```text
Macへ同期されたsource data
  -> Local Collector
  -> GCS Raw（us-central1 Standard、60日保持）
  -> Cloud Run Loader Job（1時間ごと）
  -> MotherDuck base
  -> dbt View
  -> MotherDuck Remote MCP（read-only）
  -> ChatGPT

Cloud Run Reconciliation Job（毎日）
  -> Collector / GCS / MotherDuck / martを照合
  -> 成功時だけHealthchecks.ioへheartbeat
```

初期データソースは、Macへ同期されたiPhoneの
[`App.InFocus`](../sources/screen-time/)だけとする。Local Collectorはsource固有のRawをGCSへ
直接uploadし、Cloud Run JobsがGCS以降の処理を担当する。

単一GCP project内で本番と検証を運用する。本番とは別のRawを使う検証ではGCS bucket、MotherDuck database、
Service Accountとcredentialを分離する。Screen TimeのRaw prefixは各bucket内で`raw/screen_time/v1/`に
固定し、prefixの変更で環境を切り替えない。接続確認用preflightは専用bucketの`test/preflight/`と
検証用databaseを使う。

## データ境界

- GCS Rawを直近60日間の再生可能な正本とし、MotherDuck baseを長期分析履歴とする。
- CollectorはRawの保存までを担当し、interval生成や日次集計を行わない。
- LoaderはRawの検証、decode、型付きbaseへの書き込みまでを担当する。
- dbtはinterval、日境界、集計などの分析上の意味変換をViewとして提供する。
- ChatGPTは分析用Viewだけをread-onlyで参照する。

詳細な契約は[`raw-data.md`](raw-data.md)、[`analytics.md`](analytics.md)、
[`security.md`](security.md)を正本とする。

## Core guarantees

1. downstreamの処理が失敗しても、60日の保持期間内はGCS Rawから再試行できる。
2. 同一scopeの無意味な連続重複を省きつつ、`A -> B -> A`の観測順序を保持する。
3. 同じRaw objectは再実行しても分析行を重複生成しない。
4. 後着・訂正されたデータを現在の分析結果へ反映し、取込済み分析履歴をMotherDuckへ保持する。
5. GCSに残る保持範囲だけを明示的なpartial historyとしてscratch databaseへ再構築できる。
6. Reconciliationの全監査項目が成功した後だけ外部heartbeatを送信する。DB記録との確定順序と制約は
   [`analytics.md`](analytics.md)に従う。
7. GCSへのupload完了から2時間以内に分析Viewへ反映できることを通常時のfreshness基準とする。

## 初期coreに含めないもの

- Webhook、Cloud Tasks、常駐Cloud Run Service
- 独自UI、独自MCP server、データ更新ごとのdbt実行
- RawのObject Lock、永続Parquet中間層

初期データソースの対象外項目は[`Screen Time仕様`](../sources/screen-time/)を正本とする。
