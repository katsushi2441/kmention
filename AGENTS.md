# AIエージェントへの指示（kmention）

`README.md` を最初に読む。

- 判定（自社が挙がったか）に生成AIを使わない。結果が回ごとにぶれるため。
- 質問に自社の名前を入れない。
- Codex は1問ずつ呼ばない（9問ずつまとめる）。Codex の週の枠を守る。
- chatgpt.com の画面を自動操作しない（OpenAI の利用規約で禁止）。
- 公開先は config/settings.json の publish（既定は noindex）。settings.json・.env・data/（トークン data/.token を含む）はリポジトリに入れない。
