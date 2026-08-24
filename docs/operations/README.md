# 運用

Screen Timeの収集からRaw保存、分析層への反映、障害復旧まで、実際に実行する手順を管理する。

## このディレクトリの責務

- Local Collectorの導入、更新、起動、停止
- Full Disk Accessと実行主体の確認
- 定期scan、watermark、B2 uploadの稼働確認
- Collector停止、decode失敗、upload失敗の監視と切り分け
- 遅延eventや欠損区間の再取得・補正
- 未処理Rawの再load
- Rawから分析tableを再構築する手順
- credential更新や端末移行時の確認事項

データ形式と取得アルゴリズムは[`data-collection/`](../data-collection/)、設計上の保証は[`architecture/`](../architecture/)、tableとmetricの意味は[`data-model/`](../data-model/)を正本とする。

デプロイ、監視、復旧を別ディレクトリへ分けず、運用文書としてこの階層でまとめて管理する。
