# ChatGPTからのread-only分析

## 接続境界

接続先はMotherDuck公式Remote MCPの次のendpointとする。

```text
https://api.motherduck.com/mcp
```

独自MCP server、Loader用token、dbt writer tokenは使用しない。ChatGPTで認証するMotherDuck userには、
分析へ公開してよいdatabase/shareだけをread-onlyで付与する。

個人の分析用接続には、本番ownerから同じ所有者の分析アカウントへrestricted read-onlyの
自動更新share `pdp_analytics_west`を付与する。公開範囲は本番DB全体で、base・opsも読める。
複数人向けのmarts限定公開には使わない。Raw bucketと取得用tokenはこの接続へ渡さない。
preflightには本番DB/shareへの接続を許可しない。

2026-10-06に「MotherDuck 西部」接続でshareの読取と本番shareへの書込拒否を確認した。
同じ分析userの所有するmy_dbなどへの権限と、本番shareのread-only権限を区別する。

## ChatGPT設定

新規接続が必要な場合、ChatGPT workspaceの管理者または許可済みdeveloperが次を行う。

1. SettingsのSecurity and loginでDeveloper modeを有効にする。
2. ChatGPT Pluginsの＋から上記endpointを登録する。
3. OAuthでshareを付与した分析アカウントとして認証し、tool scanを完了する。
4. app設定の詳細画面で`query`とcatalog参照に必要なread toolだけを有効にする。
5. `query_rw`などのwrite toolが無効であることを確認し、会話のDeveloper modeから対象appを選ぶ。

ChatGPTの対象plan、設定画面、承認手順は変更される可能性があるため、作業時点の
[OpenAI公式手順](https://developers.openai.com/api/docs/guides/developer-mode)を確認する。
MotherDuck endpointとOAuth、tool制限の現行仕様は
[Remote MCP接続仕様](https://motherduck.com/docs/sql-reference/mcp/)と
[read-only設定](https://motherduck.com/docs/key-tasks/ai-and-motherduck/securing-read-only-access/)を確認する。

## 受入確認

新しい会話で対象appだけを選び、次を確認する。

```text
成功すること:
- catalogから公開対象databaseとmarts.daily_screen_timeを発見できる
- SELECTで日次利用秒数を取得できる

拒否されること:
- 本番shareへINSERT / UPDATE / DELETE / CREATE / DROPできない
- preflight databaseやGCS Raw payloadを参照できない
```

SQL結果には必要な列と期間だけを含める。Raw bytesや全履歴を会話へ無条件に展開しない。
