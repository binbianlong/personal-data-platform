# OwnTracks の設定と運用

iPhone の Significant モードで記録した位置を HTTPS で送信する。Raw は GCS に無期限保存し、保存完了通知から MotherDuck へ随時取り込む。

## iPhone の設定

1. [OwnTracks](https://apps.apple.com/app/owntracks/id692424691) をインストールして起動する。
2. iPhone の「設定 → プライバシーとセキュリティ → 位置情報サービス → OwnTracks」で「常に」と「正確な位置情報」を有効にする。初回は使用中の許可を求められる場合があるため、その後に設定を確認する。
3. 「設定 → 一般 → アプリのバックグラウンド更新」で OwnTracks を有効にする。
4. OwnTracks の Map 画面左上「i」から「Status Info」を開き、Connection 欄の「Settings」を選ぶ。Settings 画面で Mode、URL、UserID、Password、DeviceID、TrackerID を設定する。
5. Map 画面へ戻り、上部の「Quiet / Manual / Significant / Move」から「Significant」を選ぶ。以下は iOS アプリ27.0.1で確認した表示名。画面上の名称は言語・版によって異なる。[iOS の公式説明](https://owntracks.org/booklet/features/ios/)

| 項目 | 値 |
|---|---|
| 接続モード / Mode | HTTP |
| URL | `https://pdp-owntracks-west-thu4eucqba-uw.a.run.app/webhooks/owntracks` |
| 認証 / Authentication | 有効 |
| UserID / Username | `owntracks` |
| Password | Secret Manager の `pdp-west-owntracks-config` に保存した `password` |
| DeviceID | `iphone` |
| TrackerID | `IP` |
| 監視モード / Monitoring | Significant（`|>`） |
| Payload encryption / Secret encryption key | 空欄 |

URL にユーザー名・パスワードを埋め込まず、認証欄へ入力する。HTTPS が通信を暗号化する。

Mac のターミナルからパスワードをクリップボードへ取得する場合:

```bash
gcloud secrets versions access 1 \
  --project=health-data-pipeline-503813 \
  --secret=pdp-west-owntracks-config | jq -r '.password' | pbcopy
```

Secret を読む権限のあるアカウントで実行する。iPhone と Mac のユニバーサルクリップボードが利用できる場合は、そのまま Password 欄へ貼り付けられる。パスワードや設定のエクスポートは Git・ログ・共有文書へ保存しない。

Significant は iOS が大きな位置変化を検知したときに送信する。一定間隔の記録を保証せず、静止中は新しい位置が来ないことがある。[位置取得の公式仕様](https://owntracks.org/booklet/features/location/)・[HTTP の公式仕様](https://owntracks.org/booklet/tech/http/)

## 最初の送信と確認

1. OwnTracks の「現在位置を送信 / Publish location」から「Send location now」を選んで手動送信する。
2. アプリの送信待ちが解消され、接続エラーがないことを確認する。
3. Raw 保存を確認する。

```bash
gcloud storage ls --recursive \
  gs://health-data-pipeline-503813-pdp-raw-west/raw/owntracks/v1/
```

4. MotherDuck の `personal_data_platform` で取込を確認する。

```sql
SELECT count(*) AS locations,
       max(received_at) AS last_received_at,
       max(loaded_at) AS last_loaded_at
FROM base.owntracks_location;

SELECT event_at_jst, latitude, longitude, accuracy_m, trigger,
       date_diff('second', received_at, loaded_at) AS ingestion_seconds
FROM marts.owntracks_location_time_series
ORDER BY received_at DESC
LIMIT 10;
```

手動送信の `trigger` は通常 `u`。観測時刻は `tst`、受信時刻は GCS の作成時刻で、通信断からの再送では異なる。通知は通常数秒で届くが、Cloud Run の起動・DB の書込競合・再試行により取込が遅れる場合がある。[GCS 通知仕様](https://docs.cloud.google.com/storage/docs/pubsub-notifications)

5. アプリをバックグラウンドにし、実際に移動した後に位置が追加されることを確認する。短距離の移動や静止中の無更新を障害と判断しない。

## 保存・分析の仕様

- source / stream は `owntracks / location`。
- `_type=location` の全フィールドを保持した正規化 JSON を gzip 保存する。端末の疑似化 key・イベント時刻・SHA-256 を object key に含め、同じメッセージの再送を重複保存しない。
- 手動送信、Significant、ping などの位置メッセージを保存する。位置以外の通常メッセージと空本文は正常応答して読み捨てる。暗号化された payload は受け付けない。
- `base.owntracks_location` は観測した点を保存する。`marts.owntracks_location_time_series` は日本時間を加えた View で、点の間の位置・滞在時間を補間しない。
- Raw と MotherDuck の位置履歴は無期限保存する。既存の日次 Job が未取込 Raw を補修し、Raw の欠損を検出する。
- 既存の restricted read-only share `pdp_analytics` に位置履歴も含まれる。接続方法は [分析接続](../../platform/chatgpt-mcp.md)を参照する。

## 送信・取込できない場合

| 状態 | 確認すること |
|---|---|
| HTTP 401 | UserID、Password、認証の有効化。Secret version を変更した場合は iPhone の Password も更新する |
| HTTP 403 | DeviceID が `iphone` で、UserID が設定値と一致すること |
| HTTP 400 / 413 | Payload encryption が空欄であること。壊れた JSON や 1 MiB を超える payload は拒否される |
| HTTP 503 / 通信エラー | 回線と receiver の状態。アプリは送信待ちを保持し、再送する |
| Raw はあるが DB にない | worker のエラー、Pub/Sub の未処理通知、共通 writer lease、メンテナンス停止状態 |
| バックグラウンドで位置が増えない | Significant の選択、位置情報の常時許可、バックグラウンド更新、実際の移動。アプリを開いて手動送信も確認する |

アプリからの再送・Pub/Sub の重複配信でも取込台帳が同じ Raw の二重取込を防ぐ。通知は未処理分を7日間保持し、それを過ぎても保存済み Raw から日次 Job が補修する。監視は worker/receiver のエラーと15分を超える通知滞留を対象にする。

Macで保存済み Raw を再処理する場合は、専用read-only Service AccountのADCを用意する。初回はブラウザで認証し、impersonation権限のあるアカウントを選ぶ。

```bash
export GOOGLE_CLOUD_PROJECT=health-data-pipeline-503813
export GCS_BUCKET=health-data-pipeline-503813-pdp-raw-west
export MOTHERDUCK_DATABASE=personal_data_platform
export PDP_REBUILD_SERVICE_ACCOUNT_EMAIL=raw-rebuild-operator@health-data-pipeline-503813.iam.gserviceaccount.com
export PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS="$HOME/Library/Application Support/personal-data-platform/gcloud-rebuild/application_default_credentials.json"

# 初回だけ実行する。通常のgcloud設定とは別のディレクトリに保存する。
CLOUDSDK_CONFIG="$(dirname "$PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS")" \
  gcloud auth application-default login \
  --impersonate-service-account="$PDP_REBUILD_SERVICE_ACCOUNT_EMAIL"
chmod 600 "$PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS"

export GOOGLE_APPLICATION_CREDENTIALS="$PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS"
export MOTHERDUCK_TOKEN="$(gcloud secrets versions access 1 \
  --project=health-data-pipeline-503813 --secret=pdp-west-motherduck-token)"
```

上記のDB tokenの数値versionは、runtimeが参照する`west_secret_versions.motherduck_token`と合わせる。Tokenの値を表示せず、処理後に`unset MOTHERDUCK_TOKEN`する。資格情報の管理は [Platform 運用](../../platform/operations.md)・[セキュリティ](../../platform/security.md)に従う。

準備後、次のコマンドで未取込・失敗分を再処理する。

```bash
pdp loader --source owntracks
pdp dbt --source owntracks
```

別の空DBへ復元する場合:

```bash
pdp rebuild --source owntracks --dry-run
pdp rebuild --source owntracks --target-db owntracks_rebuild --allow-partial-history
```

Rebuild は専用 read-only Service Account の impersonated ADC を使う。復元先は本番と別の空 DB にし、`--allow-partial-history` は OwnTracks 以外の source を復元しないことを明示する。

## 認証情報の更新と本番反映

Secret Manager の `pdp-west-owntracks-config` は次の4文字列を保持する。

```json
{
  "username": "owntracks",
  "password": "<ランダムなパスワード>",
  "device_id": "iphone",
  "device_key": "<固定した疑似化用の64桁hex key>"
}
```

Password 更新では `device_key` を保持する。新しい secret version を登録して `west_secret_versions.owntracks_config` をその数値に固定し、receiver を更新してから iPhone の Password を更新する。参照中の古い version は切替確認前に無効化しない。

初回導入は secret container を Terraform で先に作り、payload を外部登録した後に runtime 全体を反映する。Secret の値を Terraform の variable や state へ含めない。

DB 更新を伴う反映は、処理を停止して既存の書込終了を確認し、互換 image、migration、dbt を反映してから再開する。`002_owntracks.sql` の適用後は、この migration を含む image で復旧する。配信停止中も receiver が Raw を保存し、再開後に取込を行う。停止・再開は Fitbit と日次処理にも作用する。[Runtime Terraform](../../../infra/terraform/README.md)

## 導入確認

2026-10-09 に本番反映した。

| 確認項目 | 結果 |
|---|---|
| receiver の認証 | 未認証は401、認証済みの空本文は200・Raw保存なし |
| worker の公開範囲 | 未認証は403、Pub/Sub の OIDC 認証のみ許可 |
| DB・分析モデル | `002_owntracks.sql` と OwnTracks の dbt run/test が成功 |
| 日次処理 | Fitbit補修・OwnTracks照合・Screen Time監査を含む本番Jobが成功。dbtは11モデル・44テストが成功 |
| インフラ | push配信・Scheduler・監視を再開し、Terraform planに差分なし |
| iPhone の手動送信 | 実機での送信待ち。GCS・MotherDuck到着と取込時間は未確認 |
| Significant の更新 | 実機をバックグラウンドにして移動した後の到着確認待ち |

本番imageは `sha256:52506747c43c5d252d74872c7bb10f0a260ce6236d6258980ac76c3f95ccbe12` を使用する。
