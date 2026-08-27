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

1. SettingsのAppsでdeveloper modeを有効にする。
2. AppsのCreateから上記endpointを登録する。
3. OAuthで専用MotherDuck readerとして認証し、tool scanを完了する。
4. Action controlで`query`とcatalog参照に必要なread actionだけを有効にする。
5. `query_rw`を無効のままpublishする。

ChatGPTの対象plan、設定画面、承認手順は変更される可能性があるため、作業時点の
[OpenAI公式手順](https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt)を確認する。
MotherDuck endpointとOAuthの現行仕様は
[MotherDuck Remote MCPの説明](https://motherduck.com/blog/dev-diary-building-mcp/)を確認する。

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
