# セキュリティ契約

## データ保護

- GCS bucketはUniform Bucket-Level AccessとPublic Access Preventionを有効にし、Google管理鍵で暗号化する。
- Raw object key、GCS metadata、ログに、端末identifierやBundle IDなどの直接値を含めない。
- ログへ出してよい識別情報は疑似化key、object hash、件数、error codeに限定する。
- Rawの通常削除権限をCollector、Loader、Reconciliationへ与えない。
- productionと別のRawを使うtestはGCS bucketとMotherDuck databaseを分離し、権限も共有しない。
  preflightは専用bucketの`test/preflight/`と検証用databaseに権限を限定する。

疑似化方法とsource固有のkeyはsource packageで定義する。Screen Timeは
[`data-model.md`](../sources/screen-time/data-model.md)に従う。

## GCS IAM

| 実行主体 | capability |
|---|---|
| Local Collector | 固定Raw prefixへのcreateとscan receipt prefix・固定manifest keyへのcreate / deleteだけ |
| 共通runtime Service Account | 両sourceのRaw list / readとFitbit v3 Rawのcreate。毎時・日次Jobで共有 |
| Preflight | preflight bucketへのwrite / read / listと作成generationのdeleteだけ |
| Rebuild operator | 専用read-only Service Accountでproduction bucketへのlist / read |

CollectorへRawのread、list、deleteを許可しない。control JSONの同名上書きに必要なdeleteはreceipt prefixと
両streamの固定manifest keyだけへIAM conditionで限定する。keyの正本は
[Screen Timeデータモデル](../sources/screen-time/data-model.md#collector-scan-receipt)を参照する。bucket IAM policyは
Terraformでauthoritativeに管理し、projectの
Viewer / Editor / Owner convenience valueによるobject accessを残さない。

ローカルrebuildはCollector ADCを再利用せず、別のRebuild Service Accountをimpersonateする専用ADCを使う。
このService AccountはRaw bucketのobject Viewerだけを持ち、write / delete権限を持たない。

## Secret

Macでは疑似化secretをmacOS Keychainへ保存する。GCSには専用Collector Service Accountをimpersonateする
project専用ADCを使い、Service Account keyを発行しない。ADCはmode `0600`で保存し、LaunchAgentの
`GOOGLE_APPLICATION_CREDENTIALS`からだけ参照する。疑似化secretやcredentialをSQLite state、shell履歴、
Git管理ファイルへ平文で保存しない。

Cloud Runの毎時・日次Jobは共通runtime Service AccountのADCでGCSへ接続する。
receiverは別Service Accountで、Webhook検証secretとPub/Sub publisherだけを持つ。
MotherDuck、OAuth、Webhook、日次heartbeatの4 payloadはSecret Managerへ保存し、正の数値versionへ固定する。
各Jobへ渡す環境変数とsecret pinは[Runtime Terraform](../../infra/terraform/README.md#secretと資格情報)を正本とする。
secret値をTerraform variable、Terraform state、container image、GitHub Actions outputへ含めない。

ChatGPT接続にはMotherDuckのread-only user/shareを使い、Loader / dbt writer tokenを再利用しない。toolの
`query_rw`無効化はwrite防止の追加境界であり、database visibilityの代わりにはならない。公開範囲は
[`chatgpt-mcp.md`](chatgpt-mcp.md)に従う。

## GCP IAMとdeploy

- GitHub ActionsからGCPへはWorkload Identity Federationを使用し、長期Service Account keyを作らない。
- deploy用federationは対象repository、`main` ref、対象workflowへattribute conditionで限定する。
- Terraform plan用とdeploy用のService Accountを分離する。
- 毎時・日次Jobは共通Service Accountを使う。receiver、Collector、Rebuild operatorは別identityとし、
  Secret Manager accessと実行権限を必要なresourceへ限定する。共有runtime SAの権限は2 Jobの合計であり、Job間のIAM分離を保証しない。
- Cloud Schedulerには対象Cloud Run Jobを起動する権限だけを与える。

## インシデント時

credential漏えいが疑われる場合は、該当するIAM bindingまたはADCをrevokeして再認証する。Raw objectを一括削除したり、
疑似化secretだけを先に変更したりしない。疑似化secretの変更はdevice / segment keyが変わり、同一性比較と
rebuildへ影響するため、移行手順と対応表を用意した独立migrationとして扱う。
