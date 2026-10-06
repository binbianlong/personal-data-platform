# 西部リージョン移行記録

2026-10-06更新。Webhook・collector・分析/MCPを西部へ切り替え、最小構成をdeployした。
毎時・日次の手動成功後、2 Scheduler・native監視・西部Loggingを有効化した。

## 接続先

GCP projectは`health-data-pipeline-503813`。通常のcompute/Raw/Pub/Sub/Loggingは`us-west1`、
MotherDuckは`us-west-2`。

| 用途 | 通常構成 |
| --- | --- |
| Terraform state | `health-data-pipeline-503813-personal-data-platform-tfstate-west`、prefix `personal-data-platform/runtime` |
| Registry | us-west1の`personal-data-platform` |
| Raw | `health-data-pipeline-503813-pdp-raw-west`、90日、soft-delete 0。Screen Timeは元の保持起点を維持 |
| 通知 | `pdp-fitbit-west` / `pdp-fitbit-west-pull`、us-west1保管・transit強制、7日、未ackだけ保持 |
| Receiver | `pdp-fitbit-west`、通知認証とtopic publisherだけ。API取得・DB資格情報なし |
| Job | `fitbit-hourly-west`と`reconciliation-west`、共通runtime SA |
| Scheduler | 毎時15分と日次04:10 JST。最大50分/100分、共通lease125分 |
| Logging | pdp-west、30日。通常_Defaultを1経路で西部へ送る |
| MotherDuck production | `personal_data_platform_west`、owner `pdp_west_prod`、Pulse/Pulse・read scaling 1 |
| MotherDuck preflight | 独立DB/token/bucket、owner `pdp_west_preflight`、Pulse。常設Jobなし |
| 分析/MCP | restricted read-only自動更新share `pdp_analytics_west`、分析アカウントだけへgrant。接続名「MotherDuck 西部」 |
| 欠損監視 | Healthchecks daily 1件、POST、Period 24h＋Grace 24h。nativeはJob失敗・24h滞留・receiver ERRORの3件 |

通常secret payloadはMotherDuck・OAuth・Webhook・日次heartbeatの4件で、数値version 1。
手動preflight用の空のSecret Manager containerにpayloadを登録せず、管理用APIキーをruntimeへ渡さない。
production・preflight・分析アカウントはPulse/Pulse・read scaling 1へ揃えた。

GitHubのstate bucket・西部image・4 version pin・Scheduler/Loggingの有効状態を設定して再読した。
旧image/個別Fitbit設定のrepository variables4件を撤去した。Terraform Plan/Deployは
remoteの対応revisionが公開されるまで停止する。ローカルのCIは西部imageと2 Jobを管理し、
切替後のScheduler/Logging状態をrepository variablesから維持する。

## Releaseとデータ保全

現在のreleaseは`a9cd2f8`、image digestは
`sha256:6d20724f9e0fcbf7fced407a3fd75fa4712e104634fd80df8bb974367c959640`。
linux/amd64のRaw v3・west 5 migration・CLI smoke、clean wheelを確認した。
setuptoolsの以前のbuild treeに残った廃止moduleを除き、配布wheelにも廃止Fitbit台帳moduleが含まれないことを確認した。
Raw v3に非対応の旧imageをrollbackへ使わない。

- 本番/preflightにwest001〜005を適用。適用済み001〜004 checksumを維持し、005で空の通知・attempt・bundle・cursor等10表だけ撤去。
- 旧4 Jobの終了・旧2 Scheduler/queue停止・旧lease 0を確認してcollectorを停止。Raw・warehouse・collector/Biome SQLiteを最終backupし、SQLite integrity_checkを確認。
- source exportとtarget importを別processで行い、Screen Time9表243,898行の全値・Raw generation・保持起点を照合した。
- Raw53 object、圧縮6,787,430 bytesをコピーし、圧縮/展開後hashと保持起点を照合。control4件はbackupし、collector切替時に西部へ強制公開した。
- 同じsnapshotと保存Fitbit Raw v3を独立した空のlocal DBへ復元し、全業務値digestと39 dbt testを照合。
- preflightのGCS・DDL/DML roundtripを確認。本番DBおよびrestricted本番shareへのATTACH拒否を確認。
- 本番ownerから分析アカウントへrestricted shareを付与。分析SDKで読取と書込拒否を確認し、MCP西部接続からScreen Time・分心拍・migration件数を確認。

## 切替と実行

旧URLを最小receiverのbridgeへ更新し、認証付きverification201・認証なし401を確認した。
Google Health subscriberのendpointUri変更operation完了と西部URLを再読して確認。
最終コピー後にMac collectorを西部Raw bucketへ向けて再開した。

実通知の毎時実行`fitbit-hourly-west-cpqrh`は4範囲完了・15通知ack・失敗0。
制御した通知でもcommit後ackを確認した。Pub/Subの初回空応答を収集期限まで再試行し、
実行予算を越えない回帰テストを追加している。

最新版の毎時実行`fitbit-hourly-west-x7xkl`は5範囲完了・34通知ack・失敗0・保留0。
日次実行`reconciliation-west-zqvfx`は10:48:50〜10:51:00 JSTに成功した。
両Screen Time streamの監査2件、内部heartbeat3件、外部POST1回と復旧後のlease0を確認。
完了対象日は2026-10-05で、直近7日×5種別の35範囲をcoverageで照合した。
睡眠・日次指標のcivil dateと、物理時刻のTokyo日境界は別々に検証した。

最初の日次Screen Time取込では行ごと送信によりiPhone streamの未取込Rawが16分15秒かかった。
入力を同じtransaction内の列配列1回送信へ変更し、2,001行と削除・再実行の回帰を確認した。
独立した実MotherDuck preflightで20,001行・NULLを1回のINSERTで保存し、0.656秒だった。
この数値を日次全体の実行時間や定常費用へ外挿しない。
最新版の約2分10秒は、初回の53 Rawを取り込んだ後に追加3 Rawを処理した実行であり、
初回の未取込量と同じ条件の速度比較ではない。

Healthchecksの制御試験はup→grace→down→upを確認して24h＋24hへ復元。
native3警報も制御した失敗・滞留・ERRORで発火と復旧を確認し、probe閾値を戻した。
メール受信箱への到達は未確認。

## 旧資産の整理

旧Fitbitの限定inventoryは11 table scopeの320,689行、Raw/receipt 1,060 object、queue task 0。
変更行・active lease・generation変更を拒否するmanifestで整理した。
Screen Time・共通台帳・新Pub/Sub/Raw・旧組織を対象から外す。
旧Raw/stateのbucketを維持し、Screen Timeの最終snapshot・Raw・SQLite・復元proofをprivate backupへ保全した。

限定manifestの適用後、旧Fitbit対象行・Raw/receiptが0であることを確認した。
Screen Time9表は整理前後で全値が一致した。

通常runtimeのlegacy worker/decoder/mode・通知台帳・永続cursor・Cloud Tasks依存を撤去。
Cloud Tasks SDKは一度だけの整理script用のoptional migration dependencyに限定した。
適用済み旧SQLと旧DBのmigration台帳は維持する。
旧DBの最終台帳は7件だった。初回snapshot時の4件に加え、切替前の
2026-10-06 02:20 JSTに005〜007のforward migrationが適用されていた。
元の4件のchecksumが一致することを確認し、追加済み3件も巻き戻さず保持した。

旧4 Job・receiver・2 Scheduler・Cloud Tasks queue・旧13警報・5 log metric・不要SA/IAMを撤去した。
旧共有secret container3件とPDP固有の旧Fitbit secret4件を削除し、新runtimeの4つの数値pinを維持した。
共有のGoogle OAuth refresh secretは、Cloud Run外の利用元を除外できないため保持した。
旧組織・Screen Timeのbackup bucket・collector/rebuild用の権限を削除しない。
旧us-central1 registryは、全Job/Serviceにimage参照がないことを確認して削除した。
西部registryと互換性のあるRaw v3 rollback imageを保持し、Cloud Tasks向けのdeploy権限を撤去した。
最終inventoryはJob2件・Service1件・有効Scheduler2件・有効native警報3件・registry1件。
通常logの西部30日保持と、runtimeが4 secretのversion1だけを使うことを確認した。

検証・backup・secret・健康データはGit対象外の`var/west-migration/2026-10-05/`へ保存する。
7日・30日のGCS容量/Class A/B、Pub/Sub滞留・再配信、Cloud Run実行時間/送信、MotherDuck容量/CUhと
実請求は継続運用で確認し、専用計測Jobや自動最適化を追加しない。

## MotherDuck向け通信の料金と移行前計測

2026-10-05にCloud Billing Catalog APIでCloud Run専用のインターネット転送SKUを確認した。米国から東京への大陸間通信は最初から$0.12/GiB（SKU `DBAA-7594-B9FA`）、北米内の通信は月1GiBまで無料、その後は$0.105/GiB（SKU `DDFD-AE42-E219`）。いずれも最初の有料帯のUSD単価であり、北米内でも同じOregonだから無制限に無料とは扱わない。[Cloud Run料金](https://cloud.google.com/run/pricing)、[大陸間転送SKU](https://cloud.google.com/skus?currency=USD&filter=DBAA-7594-B9FA)、[北米内転送SKU](https://cloud.google.com/skus?currency=USD&filter=DDFD-AE42-E219)

同projectの`run.googleapis.com/container/network/sent_bytes_count`を`kind=internet`で集計した。JobとServiceを含むCloud Run全体の監視値であり、MotherDuckだけの通信量や請求明細ではない。

| 計測期間（JST） | 外向きインターネット送信 | 全量を東京向けと仮定した概算 |
| --- | ---: | ---: |
| 2026-09-01 00:00〜2026-10-01 00:00 | 418,676,667 bytes（0.390 GiB） | 約$0.047 |
| 2026-10-01 00:00〜2026-10-05 20:56 | 144,666,580 bytes（0.135 GiB） | 約$0.016 |

9月は旧構成の計測で、新Fitbitの定常処理を含まない。10月は停止中の処理と検証実行を含むため、通常月の通信量へ外挿しない。監視値からの概算を実際の課金額として記録せず、運用再開後の送信量と請求SKUを同じ期間で照合する。MotherDuck自体の保存・計算無料枠に収まっていても、Cloud Runから東京へ送る料金は別に発生し得る。
