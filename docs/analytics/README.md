# Analytics

データソースを横断するmartとmetricを管理する。

扱う内容:

- source横断JOINとtimezone統一
- 日次集約と分析用mart
- 複数sourceを比較・統合するmetric
- metricの単位、計算方法、欠損時の意味

source単独で完結する派生値と品質定義は[`sources/`](../sources/)を正本とする。
