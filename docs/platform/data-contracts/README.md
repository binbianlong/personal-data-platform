# データ契約

複数のデータソースに共通するRawとbaseの規約を管理する。

扱う内容:

- Rawの保存単位、識別方法、metadata
- event time、observed time、ingested timeの使い分け
- stable key、provenance、nullabilityの共通規約
- base tableで共通して維持するcolumnと品質表現

具体的なpayload field、natural key、table schema、変換規則は[`sources/`](../../sources/)を正本とする。
