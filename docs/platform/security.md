# セキュリティ契約

## データ保護

- B2 bucketはprivateとし、SSE-B2を有効にする。
- Raw object key、B2 metadata、ログに、端末identifierやBundle IDなどの直接値を含めない。
- ログへ出してよい識別情報は疑似化key、object hash、件数、error codeに限定する。
- Rawの通常削除権限をCollector、Loader、Reconciliationへ与えない。
- productionと別のRawを使うtestはB2 bucketとMotherDuck databaseを分離し、credentialも共有しない。
  preflightは専用の`test/` prefixと検証用databaseに権限を限定する。

疑似化方法とsource固有のkeyはsource packageで定義する。Screen Timeは
[`data-model.md`](../sources/screen-time/data-model.md)に従う。

## B2 credential

| 実行主体 | capability |
|---|---|
| Local Collector | 対象bucketの固定Raw prefixとscan receiptへのwriteだけ |
| Loader | 対象Raw prefixへのlist / readだけ |
| Reconciliation / rebuild | 対象Raw prefixとscan receiptへのlist / readだけ |
| Preflight | test prefixへのwrite / read / listと作成したobject versionのdeleteだけ |

application keyはbucket全体や別environmentへ権限を広げない。uploadに必要な最小capability以外を
Collectorへ付与せず、read、list、deleteを許可しない。

## Secret

MacではCollectorのB2 application keyと疑似化secretをmacOS Keychainへ保存する。設定ファイル、
SQLite state、shell履歴、Git管理ファイルへ平文で保存しない。

GCPではLoader用B2 read key、MotherDuck writer token、Healthchecks.io ping URLをSecret Managerへ
保存する。secret値をTerraform variable、Terraform state、container image、GitHub Actions outputへ
含めない。

ChatGPT接続にはMotherDuckのread-only user/shareを使い、Loader / dbt writer tokenを再利用しない。toolの
`query_rw`無効化はwrite防止の追加境界であり、database visibilityの代わりにはならない。公開範囲は
[`chatgpt-mcp.md`](chatgpt-mcp.md)に従う。

## GCP IAMとdeploy

- GitHub ActionsからGCPへはWorkload Identity Federationを使用し、長期Service Account keyを作らない。
- deploy用federationは対象repository、`main` ref、対象workflowへattribute conditionで限定する。
- Terraform plan用とdeploy用のService Accountを分離する。
- Cloud Run JobごとにService Accountを分け、Secret Manager accessと実行権限を必要なresourceだけへ与える。
- Cloud Schedulerには対象Cloud Run Jobを起動する権限だけを与える。

## インシデント時

credential漏えいが疑われる場合は、該当keyをrevokeして新しいkeyへ差し替える。Raw objectを一括削除したり、
疑似化secretだけを先に変更したりしない。疑似化secretの変更はdevice / segment keyが変わり、同一性比較と
rebuildへ影響するため、移行手順と対応表を用意した独立migrationとして扱う。
