# Platform運用

データソースをまたいで共有する運用手順を管理する。

扱う内容:

- 実行環境の構築、更新、確認、rollback
- 共通の監視基盤、通知、稼働判定
- Rawの再処理と分析層の再構築
- credential更新と障害復旧の共通手順

source固有の収集間隔、watermark、補正、異常条件は[`sources/`](../../sources/)を正本とする。
