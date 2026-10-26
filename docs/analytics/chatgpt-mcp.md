# ChatGPTからのread-only分析

## 接続境界

MotherDuck公式Remote MCPへ接続する。

```text
https://api.motherduck.com/mcp
```

独自MCP server、Loader用token、dbt writer tokenは使用しない。ChatGPTで認証するMotherDuck userには、
分析へ公開してよいdatabase/shareだけをread-onlyで付与する。

個人の分析用接続には、本番ownerから同じ所有者の分析アカウントへrestricted read-onlyの
自動更新share `pdp_analytics`を付与する。公開範囲は本番DB全体で、base・opsも読める。
複数人向けのmarts限定公開には使わない。Raw bucketと取得用tokenはこの接続へ渡さない。
preflightには本番DB/shareへの接続を許可しない。

共有は元DBと同じ名前`personal_data_platform`でattachする。dbtのview・macroがこのDB名を参照するため、
share名を接続名にすると集計の参照が失敗する。shareを作成した際に返されるURLを使う。

```sql
ATTACH '<pdp_analyticsのshare URL>' AS personal_data_platform;
```

この別名の制約は[MotherDuckの共有仕様](https://motherduck.com/docs/key-tasks/sharing-data/sharing-overview/#views-and-fully-qualified-table-references)に従う。

## ChatGPT設定

接続の追加は[OpenAI公式手順](https://developers.openai.com/api/docs/guides/custom-mcp-server)に従う。
上記endpointへOAuthで分析アカウントとして接続し、`query`とcatalog参照用read toolだけを有効にする。
`query_rw`などのwrite toolは無効にする。

MotherDuck側の認証と権限は[Remote MCP接続仕様](https://motherduck.com/docs/sql-reference/mcp/)・
[read-only設定](https://motherduck.com/docs/key-tasks/ai-and-motherduck/securing-read-only-access/)を参照する。

## 受入確認

新しい会話で対象appだけを選び、次を確認する。

```text
成功すること:
- catalogからpersonal_data_platformとmarts.daily_screen_timeを発見できる
- SELECTで日次利用秒数を取得できる

拒否されること:
- 本番shareへINSERT / UPDATE / DELETE / CREATE / DROPできない
- preflight databaseやGCS Raw payloadを参照できない
```

SQL結果には必要な列と期間だけを含める。Raw bytesや全履歴を会話へ無条件に展開しない。
