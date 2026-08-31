# ChatGPTからのread-only分析

## 接続境界

接続先はMotherDuck公式Remote MCPの次のendpointとする。

```text
https://api.motherduck.com/mcp
```

独自MCP server、Loader用token、dbt writer tokenは使用しない。ChatGPTで認証するMotherDuck userには、
分析へ公開してよいdatabase/shareだけをread-onlyで付与する。

MotherDuckのaccess controlは契約によってdatabase単位、またはtable/schemaを限定したshareになる。後者を
利用できない場合、`marts`だけを含む専用databaseを用意するまで本番接続を有効にしない。同じdatabaseに
`base`と`ops`が見えている状態で「`marts`だけを参照する」という指示をsecurity boundaryにしてはならない。

## ChatGPT設定

ChatGPT workspaceでcustom MCP appを作成できる管理者または許可済みdeveloperが次を行う。

1. SettingsのSecurity and loginでDeveloper modeを有効にする。
2. ChatGPT Pluginsの＋から上記endpointを登録する。
3. OAuthで専用MotherDuck readerとして認証し、tool scanを完了する。
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
- query_rwを選択または実行できない
- INSERT / UPDATE / DELETE / CREATE / DROPを実行できない
- 非公開database、base、ops、Raw payloadを参照できない
```

SQL結果には必要な列と期間だけを含める。Raw bytesや全履歴を会話へ無条件に展開しない。
