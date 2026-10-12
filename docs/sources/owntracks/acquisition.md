# OwnTracks取得仕様

OwnTracksの位置メッセージをHTTPSの`POST /webhooks/owntracks`で受け取る。receiverはHTTP Basic認証を行い、リクエストに`X-Limit-U`または`X-Limit-D`がある場合は設定済みのuser・device IDとの一致を確認する。認証情報とendpointの設定は[運用](operations.md)を参照する。

`_type=location`のJSONから時刻`tst`、緯度・経度を検証する。空bodyと他のメッセージ種別は保存しない。暗号化payloadは受け付けず、通信はHTTPSで保護する。bodyは最大1 MiB。認証不一致は401/403、形式不正は400、超過は413、Raw保存失敗は503を返す。

receiverは位置情報を[保存対象フィールド](data-model.md#rawと分析行)に絞って正規化し、決定的なgzipをGCSへcreate-onlyで保存する。成功時はOwnTracks向けにJSON配列`[]`を返す。GCSのobject finalize通知をPub/Sub経由で非公開workerへpushし、workerがDBへ取り込む。receiver自身はMotherDuckへ接続しない。
