# Kurage AI言及トラッカー（kmention）

ChatGPT などの AI に「名古屋でシステム開発を頼める会社は？」と聞かれたとき、自社が勧められているかを毎週記録する。
あわせて、ホームページから強化すべきキーワードを抽出し、キーワードと会社の事実から AI に引用されやすいページの下書きを作る。

- デモ: https://proto.exbridge.jp/kmention/
- 商品ページ: https://kappstore.exbridge.jp/

| 機能 | 入口 | 要るもの |
|---|---|---|
| 1. AI の答えに自社が挙がるかを記録 | `kmention.py` | Codex CLI（ChatGPT の有料プランでログイン） |
| 2. 強化すべきキーワードの抽出 | `keywords.py` | Ollama・Google 広告 API（キーワードプランナー）・Search Console（任意） |
| 3. キーワードと事実からページの下書き | `pagegen.py` | Ollama・playwright（画面幅の点検） |

## 考え方

- OpenAI の API キーは使わない。Codex CLI の `--search`（ChatGPT の契約で動く）で聞く。
  記録するのは OpenAI のモデルが Web を検索して答えたもので、chatgpt.com の画面の答えそのものではない（画面の自動操作は OpenAI の利用規約で禁止なのでしない）。
- 自社が挙がったかの判定に生成AIは使わない（`config/brands.json` の名前の語と、URL のホスト名の一致）。結果が回ごとにぶれないように。
- 質問（`config/questions.json`）に自社の名前は入れない。答えを自社に寄せないため。
- 質問ごとに Google の月間検索数（`vol`）を書き、「検索数÷順位」で重みを付けた割合を出す。誰も聞かない質問で1番になっても数字は上がらない。
- Codex は1問ずつ呼ばず、9問ずつまとめて聞く（27問で1回およそ18万トークン・2026-11 実測）。

## 設置

```
git clone https://github.com/katsushi2441/kmention.git && cd kmention
cp config/settings.example.json config/settings.json    # 公開先・メール・Ollama・Google 広告 API を書く
codex login                                              # ChatGPT のアカウントで
```

1. `config/brands.json` に自社の名前の言い方（aliases）とドメイン（domains）を書く。店の名前など、自社名を含むが別物の語は `exclude` に。
2. `config/questions.json` に質問を書く。`kw` は同じことを Google で探すときの語、`vol` はその月間検索数（キーワードプランナーで調べる）。
3. パスワードは `settings.json` に書かず、`*_env` に書いた名前で環境変数か `.env` に置く（例: `FTP_PASS=...`、`SMTP_PASSWORD=...`）。

## 使い方

```
python3 kmention.py run [--only q01,q16] [--batch 9]   # 質問を投げて data/runs/<日時>.json に残す
python3 kmention.py build                              # outputs/index.html
python3 kmention.py deploy                             # settings.json の公開先へ（既定は noindex）
python3 kmention.py mail                               # 最新の回の要約をメール
python3 kmention.py weekly                             # run → build → deploy → mail
```

cron の例（毎週月曜7時）:

```
0 7 * * 1  cd /opt/kmention && /usr/bin/python3 kmention.py weekly >> data/weekly.log 2>&1
```

## 強化すべきキーワードの抽出（keywords.py）

```
pip install google-ads                       # キーワードプランナー
python3 keywords.py run <site> [--reuse]     # config/sites.json の範囲を読み、data/keywords/<site>-<日時>.json
python3 keywords.py build <site>             # outputs/keywords-<site>.html（kmention.py deploy で一緒に置く）
```

サイトマップの範囲のページを読み（題名・見出し・説明文・質問・AEO の判定）、ページごとに検索語の候補をローカルの LLM で出し、
キーワードプランナーで検索数と、より具体的で検索の多い言い方を調べる。Search Console の順位、kmention の結果と突き合わせ、
語ごとに「新しいページを作る／題名と冒頭を直す／足りている」を決めて、検索数×足りなさで並べる（判定に生成AIは使わない）。
言い方違い（在庫 管理／在庫 の 管理）は1つにまとめ、助詞・1文字の語は照合で必須にしない。他社の社名の語は外す。

- `config/sites.json` にサイトマップ・読む URL の範囲（正規表現）・Search Console のプロパティを書く。
- Search Console は任意。`settings.json` の `search_console.authorized_user_json` に、client_id・client_secret・refresh_token を持つ JSON の場所を書く。

## キーワードと要点からページを作る（pagegen.py）

```
pip install playwright && python3 -m playwright install chromium   # 画面幅の点検
python3 pagegen.py make specs/<名前>.json   # 下書き → 点検 → outputs/pages/<slug>.html と .check.json
python3 pagegen.py deploy <slug>            # 公開先の pages/<slug>.html（既定は noindex・確認用）
```

spec に keyword・slug・company（名前・URL・問い合わせ先・ロゴ）・facts（事実）・sources（根拠のページ）を書く（見本 `specs/zaiko-kanri-system.json`）。
点検: 事実に無い数字／断定の言葉／題名の長さと先頭のキーワード／定義文／AEO の判定／320・390・1280px のはみ出し。
**言い過ぎ（「動かせる」→「環境を選ばない」）は自動では止めきれないので、公開の前に人が読む。**

AEO の判定は内蔵の簡易判定（`aeo.py`: 定義文・質問の見出し・根拠のない断定）。Kurage Track & GEO & SEO をお持ちなら、
`settings.json` の `aeo_php` に `ktrackgeoseo_aeo.php` の場所を書くと、そちらの点数を使う。

## ファイル

- `kmention.py` 質問・記録・画面・公開・メール ／ `keywords.py` キーワード抽出 ／ `pagegen.py` ページの下書き
- `kmconf.py` 設定と秘密の読み込み・公開・メール ／ `aeo.py` AEO の判定 ／ `kmention_jobs.py` 毎時起こす型の定期実行の入口
- `config/` 設定（`settings.json` は git に入れない） ／ `data/` 記録（git に入れない） ／ `outputs/` 画面

## ライセンス

MIT License（`LICENSE`）。© 2026 EXBRIDGE, Inc.
