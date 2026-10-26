# OwnTracks運用

Cloud Run・Pub/Sub・IAM・日次Jobの共通操作は[Platform運用](../../platform/operations.md)を参照する。

## 接続と資格情報

receiver URLはTerraformの`runtime_resources.owntracks_url` output（末尾`/webhooks/owntracks`）を使う。OwnTracksアプリにはHTTPS URLとHTTP Basic認証のuser・passwordを設定する。Secret Managerの`owntracks_config`には`username`、`password`、`device_id`、64桁の疑似化`device_key`をJSONで登録し、`PDP_OWNTRACKS_CONFIG`へ渡す。Secretのversion固定と権限は[Runtime Terraform](../../../infra/terraform/README.md#secretと資格情報)に従う。

## 取込と補修

workerはRawのDBへのcommitを確認してから204を返し、失敗やlease競合は503で再配信を受ける。日次JobはRawと取込台帳を照合し、未取込分と旧parser版で取り込んだRawを再処理する。

取込遅延は`base.owntracks_location`の`received_at`と`loaded_at`で確認する。scratch DBへの再生は[Rebuild](../../platform/operations.md#rebuild)、復旧・監視・停止操作は[Platform運用](../../platform/operations.md#停止と復旧)を参照する。
