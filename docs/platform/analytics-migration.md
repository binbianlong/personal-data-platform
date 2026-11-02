# 分析スキーマの移行

この移行は、旧 warehouse と iPhone・Mac の旧 SQLite を保持したまま、一時的なステージへ新しい基礎表、分析ビュー、ローカル状態を作る。通常の収集処理や API の再取得で履歴を作り直さない。永続的な database は `personal_data_platform`、share は `pdp_analytics` のまま使う。

## 開始前

- 本番へ適用する前に、使い捨ての MotherDuck database で後述の clone と overwrite を検証する。制約、インデックス、ビュー、マクロ、sequence、migration 履歴、値と share 経由の照会を検査する。ローカル DuckDB の検証だけでは MotherDuck のメタデータ置換や share の挙動を保証できない。
- DB writer、Scheduler、loader、両 collector、手動操作をすべて停止し、実行中の処理と lease が終了したことを確認する。受信キューと Raw は保持する。切替と受入が終わるまで停止を維持する。
- 接続先の組織・リージョン、database の UUID・transient/retention 設定、share の UUID・URL・自動更新設定・reader 権限、Terraform state を記録する。hold と stage は本番と互換の所有権・設定を持つことを確認する。
- 旧 SQLite は両ソースの既存 `collector.db` を使う。未処理の `-wal` または `-journal` がある場合は読み取らず停止するため、内容を保全して状態を確認する。作業先の親ディレクトリに十分な空き容量と適切な権限を用意する。stage の DB と SQLite 出力は新規の名前とし、途中で失敗した出力に再実行しない。確認後に別の空の stage を作って最初からやり直す。

## 旧データの保持と準備

次の名前は例であり、未使用の一時名を選ぶ。`OR REPLACE` や `CASCADE` は使わない。

```sql
CREATE DATABASE pdp_legacy_hold_20261102 FROM personal_data_platform;
CREATE DATABASE pdp_schema_stage_20261102 (TRANSIENT);
```

hold と canonical の全スキーマ・表・列型・既定値・NULL 制約・キー・インデックス・ビュー・マクロ・sequence を比較する。旧 migration 履歴12件、全表の行数とキー、全列値を比較し、欠落や差異があれば停止する。旧 SQLite と実行中のコード/config も受入まで保存する。clone の存在だけを復旧可能性の証拠にしない。

`MOTHERDUCK_TOKEN` と `PDP_FITBIT_SUBJECT_KEY` は環境変数に設定する。値をコマンド行やログに出さない。以下のパスは各環境の実パスに置き換える。デフォルトは読取専用の事前検査で、warehouse や SQLite の出力を作らない。stage は事前に存在する空 database を指定する。

```sh
uv run --locked python -m scripts.migrate_analytics_schema \
  --remote --source-db personal_data_platform --target-db pdp_schema_stage_20261102 \
  --iphone-state /path/to/iphone/collector.db --mac-state /path/to/mac/collector.db \
  --iphone-output /path/to/prepared/iphone/collector.db \
  --mac-output /path/to/prepared/mac/collector.db
```

旧12 checksum、旧 base/ops の列とキー、単一 Fitbit account、日次の重複、睡眠詳細の親、lease、Screen Time の参照・snapshot・parser・pending payload を検査する。想定外があれば先に原因を解決する。特に後の sent、または warehouse で確定済みの snapshot より古い pending は順序を安全に保てないため停止する。warehouse への保存だけ済んで ACK が失われた pending は、成功 receipt の parser 版を引き継ぐ。再送には同じ parser 版が必要となり、異なる版では停止する。旧 source と対象 stage が同一、target が canonical、あるいは stage に既存のオブジェクトがある場合も停止する。

事前検査に成功したら、同じコマンドに `--apply` を追加する。新 baseline の実際の履歴を適用し、SQL で全履歴を stage へ投影する。投影と stage の全行を双方向に比較し、dbt が lease を取得して7ビューを構築する。両 SQLite は別々に生成し、旧 ops の DISTINCT 参照、pending の圧縮 bytes と hash、最新 sent metadata を検証してから有効化済み marker を設定する。旧 SQLite は変更しない。完了後も source/stage の履歴、全表と参照、全ビューの結果、stage の定義に一時 catalog への依存がないことを確認する。失敗や応答消失後は状況を調べ、部分 stage を完成品として使わない。

旧処理は削除通知（tombstone）を送信済みにしても warehouse の segment を作らない。その記録は保存済み payload の hash・削除通知の形式・成功 receipt を確認できる場合だけ、参照イベントを持たない sent 状態として引き継ぐ。通常イベントの segment 欠落は引き続き拒否する。

MotherDuckのdbt処理が残す空の`dbt_temp`スキーマは許容する。この領域を含め、予定外のテーブル・ビュー・マクロ・sequenceがある場合は拒否する。

## SQLite の配置準備

collector が停止したまま、各 OS について次を実行する。初回は `--apply` を省略して検査し、成功後に追加する。canonical の旧 SQLite はこの操作では変更しない。pending の payload は canonical と同じ親ディレクトリの `collector.payloads` にコピーし、パスを書き換えた `collector.db.ready` を作る。既存の ready や payload ディレクトリがあると停止する。

```sh
uv run --locked python -m scripts.migrate_analytics_schema \
  --prepare-install /path/to/prepared/iphone/collector.db /path/to/iphone/collector.db \
  --source-id iphone_screen_time --apply
uv run --locked python -m scripts.migrate_analytics_schema \
  --prepare-install /path/to/prepared/mac/collector.db /path/to/mac/collector.db \
  --source-id mac_screen_time --apply
```

受入前に canonical SQLite を別名で保持し、同じ filesystem 上で ready を `collector.db` に移す。新しい SQLite の pending が参照する全 payload の存在・gzip・hash・順序を collector 再開前に確認する。stage の payload ディレクトリを削除しても canonical の pending パスが解決することを確認し、旧 SQLite と stage は受入まで残す。

## 公開と受入

hold の完全性、stage の全比較、SQLite の参照と payload、runtime/config と停止状態を再確認し、接続先と名前を照合する。自動 share は overwrite の commit 時点で新しい内容を公開し得るため、全 stage ビューが catalog 非依存であることを事前に別接続・別名コピー・reader で確かめる。

```sql
COPY FROM DATABASE pdp_schema_stage_20261102 (OVERWRITE) TO personal_data_platform;
```

応答が不明な場合は再実行せず canonical の履歴とデータを調べる。fresh な canonical のみの接続と reader 接続で7ビューすべてを照会し、列・値・履歴、DB/share の識別情報・URL・権限、Terraform plan を確認する。SQLite の配置と参照を確認し、対応 runtime で writer を再開する。再開後は新規取得とキューの drain を確認する。

writer 再開前に戻す場合は、保持した旧 SQLite と旧 runtime を戻し、検証済み hold を使う。

```sql
COPY FROM DATABASE pdp_legacy_hold_20261102 (OVERWRITE) TO personal_data_platform;
```

writer 再開後は hold だけを上書きすると再開後の書込みが失われる。Raw/キューの再適用と SQLite/runtime の整合を含む復旧計画が必要。受入後、canonical や consumer から stage/hold への依存がないことを確認してから一時 database と旧 SQLite を個別に片付ける。
