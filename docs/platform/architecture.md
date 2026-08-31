# アーキテクチャ

## 初期core

```text
Macへ同期されたsource data
  -> Local Collector
  -> Backblaze B2 Raw
  -> Cloud Run Loader Job（1時間ごと）
  -> MotherDuck base
  -> dbt View
  -> MotherDuck Remote MCP（read-only）
  -> ChatGPT

Cloud Run Reconciliation Job（毎日）
  -> Collector / B2 / MotherDuck / martを照合
  -> 成功時だけHealthchecks.ioへheartbeat
```

初期データソースは、Macへ同期されたiPhoneの
[`App.InFocus`](../sources/screen-time/)だけとする。Local Collectorはsource固有のRawをB2へ
直接uploadし、GCPはB2以降の処理を担当する。

単一GCP project内で本番と検証を運用する。本番とは別のRawを使う検証ではB2 bucket、MotherDuck database、
Service Accountとcredentialを分離する。Screen TimeのRaw prefixは各bucket内で`raw/screen_time/v1/`に
固定し、prefixの変更で環境を切り替えない。接続確認用preflightは専用の`test/`配下と検証用databaseを使う。

## データ境界

- B2 Rawを再構築の正本とする。MotherDuckとdbt ViewはB2から再生成できる派生データである。
- CollectorはRawの保存までを担当し、interval生成や日次集計を行わない。
- LoaderはRawの検証、decode、型付きbaseへの書き込みまでを担当する。
- dbtはinterval、日境界、集計などの分析上の意味変換をViewとして提供する。
- ChatGPTは分析用Viewだけをread-onlyで参照する。

詳細な契約は[`raw-data.md`](raw-data.md)、[`analytics.md`](analytics.md)、
[`security.md`](security.md)を正本とする。

## Core guarantees

1. downstreamの処理が失敗しても、B2へ保存済みのRawは失われない。
2. 同一scopeの無意味な連続重複を省きつつ、`A -> B -> A`の観測順序を保持する。
3. 同じRaw objectは再実行しても分析行を重複生成しない。
4. 後着・訂正されたデータを現在の分析結果へ反映し、過去のRaw観測も保持する。
5. MotherDuckをB2だけからscratch databaseへ再構築し、本番と比較できる。
6. Reconciliationの全監査項目が成功した後だけ外部heartbeatを送信する。DB記録との確定順序と制約は
   [`analytics.md`](analytics.md)に従う。
7. B2へのupload完了から2時間以内に分析Viewへ反映できることを通常時のfreshness基準とする。

## 初期coreに含めないもの

- Webhook、Cloud Tasks、常駐Cloud Run Service
- 独自UI、独自MCP server、データ更新ごとのdbt実行
- RawのObject Lock、永続Parquet中間層

初期データソースの対象外項目は[`Screen Time仕様`](../sources/screen-time/)を正本とする。
