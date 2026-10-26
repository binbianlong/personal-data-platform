# OwnTracks運用

Cloud Run・Pub/Sub・IAM・日次Jobの共通操作は[Platform運用](../../platform/operations.md)を参照する。

## 接続と資格情報

receiver URLはTerraformの`runtime_resources.owntracks_url` output（末尾`/webhooks/owntracks`）を使う。OwnTracksアプリにはHTTPS URLとHTTP Basic認証のuser・passwordを設定する。Secret Managerの`owntracks_config`には`username`、`password`、`device_id`、64桁の疑似化`device_key`をJSONで登録し、`PDP_OWNTRACKS_CONFIG`へ渡す。Secretのversion固定と権限は[Runtime Terraform](../../../infra/terraform/README.md#secretと資格情報)に従う。

## 取込と補修

receiverがRawを保存すると、GCSのfinalize通知がPub/Subからworkerへ届く。workerは通知のbucket・object・generationを検証し、共通LoaderでDBへのcommitを確認してから204を返す。失敗やlease競合は503で再配信を受ける。日次JobもRawと取込台帳を照合し、未取込分を補修する。

parserは`motionactivities`も保存する。日次Jobは旧parser版で取り込み済みのRawも再処理して既存行を補完する。Rawに項目がない行は`NULL`のまま保持する。

確認には`base.owntracks_location`の`received_at`と`loaded_at`を比較する。Rawは無期限に保持する。scratch DBへの再生は[Rebuild](../../platform/operations.md#rebuild)、復旧・監視・停止操作は[Platform運用](../../platform/operations.md#停止と復旧)を参照する。
