#!/usr/bin/env python3
"""Kurage AI言及トラッカー（kmention）。決めた質問を、ChatGPT と同じ OpenAI のモデルに Web 検索つきで投げ、
回答に自社が挙がったか・何番目か・根拠にされたページ・一緒に挙がった他社を記録して、推移のページを作る。
  python3 kmention.py run [--only q01,q02] [--batch 9]   # 質問を投げて data/runs/<日時>.json に残す
  python3 kmention.py build                              # outputs/index.html を作る
  python3 kmention.py deploy                             # outputs/ を settings.json の公開先に置く（既定は noindex）
  python3 kmention.py mail                               # 最新の回の要約をメールで送る
  python3 kmention.py weekly                             # run → build → deploy → mail（cron から週1回）

2026-11-01 ユーザー指示「このやり方で GEGO みたいなのつくろう」（名前は Kurage AI言及トラッカー（kmention））。
- OpenAI の API キーは無い。Codex CLI の `--search`（ChatGPT の契約で動く・OpenAI のモデルが Web を検索して答える）を使う。
  chatgpt.com の画面の答えそのものではない（画面の自動操作は OpenAI の利用規約で禁止なのでしない）。
- Codex の週の枠を守るため、1問ずつ投げずに batch 問ずつまとめて投げる（既定8問・24問で3回）。
- 質問に当社の名前は入れない。答えを当社に寄せないため。
- 挙がったかの判定は生成AIを使わず、回答の名前・URL に設定の語やドメインが出るかで数える（結果がぶれないように）。
"""
import argparse
import datetime
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time

import kmconf

ROOT = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(ROOT, "config")
RUNS = os.path.join(ROOT, "data", "runs")
# Gemini（Google 検索つき）の記録は別の場所に置く。ChatGPT 側（Codex）の推移と混ぜない（2026-11-02）
RUNS_BY = {"codex": RUNS, "gemini": os.path.join(ROOT, "data", "runs-gemini")}
PAGE_BY = {"codex": "index.html", "gemini": "gemini.html"}
LABEL_BY = {"codex": "ChatGPT（OpenAI のモデル・Web 検索つき）", "gemini": "Gemini（Google 検索つき）"}
OUT = os.path.join(ROOT, "outputs")
MODEL = os.environ.get("KMENTION_MODEL") or kmconf.get("codex.model", "")   # 空なら Codex の既定のモデル
CODEX = os.environ.get("CODEX_BIN") or kmconf.get("codex.bin") or shutil.which("codex") or "codex"
JST = datetime.timezone(datetime.timedelta(hours=9))

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answers"], "properties": {"answers": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "recommendations"],
                               "properties": {"id": {"type": "string"}, "recommendations": {"type": "array", "items": {
                                   "type": "object", "additionalProperties": False, "required": ["name", "url", "source_url"],
                                   "properties": {"name": {"type": "string"}, "url": {"type": "string"},
                                                  "source_url": {"type": "string"}}}}}}}}}

PROMPT = """あなたは、日本の利用者から質問を受けて答える AI アシスタントです。下の質問それぞれについて、
ふだん利用者に答えるときと同じように Web を検索し、利用者に勧める会社・サービス・サイトを、勧める順に最大5つ挙げてください。
- 実在し、いま使えるものだけを挙げる。見つからなければ空でよい
- name は会社名・サービス名・サイト名、url はその公式のページ、source_url は根拠にしたページ（同じでもよい）
- 特定の会社を優遇しない。検索で見つかった中から、質問に合うものを選ぶ

# 質問
{questions}

answers に、質問の id ごとに recommendations（勧める順）を入れて返してください。"""


def load(name):
    return json.load(open(os.path.join(CONF, name), encoding="utf-8"))


def ask(batch, workdir):
    """batch の質問を codex に1回で投げる。{id: [推薦…]} を返す"""
    sp, op = os.path.join(workdir, "schema.json"), os.path.join(workdir, "out.json")
    json.dump(SCHEMA, open(sp, "w", encoding="utf-8"))
    qs = "\n".join(f"- {q['id']}: {q['q']}" for q in batch)
    env = dict(os.environ, PATH=os.path.dirname(CODEX) + ":" + os.environ.get("PATH", ""))   # codex は node で動く
    r = subprocess.run([CODEX, "--search", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only"] + (["-m", MODEL] if MODEL else [])
                       + ["--output-schema", sp, "-o", op, "-"], input=PROMPT.format(questions=qs),
                       capture_output=True, text=True, timeout=1800, cwd=workdir, env=env)
    if r.returncode != 0 or not os.path.exists(op):
        raise RuntimeError(f"codex が失敗: {r.stderr[-500:]}")
    tok = re.search(r"tokens used\s*\n\s*([\d,]+)", r.stderr or "")
    ans = json.loads(open(op, encoding="utf-8").read())["answers"]
    return {a["id"]: a["recommendations"] for a in ans}, int(tok.group(1).replace(",", "")) if tok else None


GEMINI_PROMPT = """あなたは、日本の利用者から質問を受けて答える AI アシスタントです。下の質問について、
ふだん利用者に答えるときと同じように Google 検索を使い、利用者に勧める会社・サービス・サイトを、勧める順に最大5つ挙げてください。
- 実在し、いま使えるものだけを挙げる。見つからなければ空でよい
- name は会社名・サービス名・サイト名、url はその公式のページ、source_url は根拠にしたページ（同じでもよい）
- 特定の会社を優遇しない。検索で見つかった中から、質問に合うものを選ぶ

質問: {q}

次の形の JSON だけを返してください（説明の文は書かない）:
{{"recommendations": [{{"name": "...", "url": "https://...", "source_url": "https://..."}}]}}"""


class DailyLimit(Exception):
    """Gemini の1日の上限に当たった"""


def ask_gemini(q):
    """Gemini API（generateContent・google_search ツール）に1問。推薦の一覧と、根拠にした検索結果のサイト名を返す。
    検索つきでは JSON の型を指定できないので、文中の JSON を取り出す。読めなければ空"""
    import urllib.request
    key = kmconf.secret(kmconf.get("gemini.key_env", "GEMINI_API_KEY"))
    if not key:
        raise RuntimeError("GEMINI_API_KEY がありません")
    model = kmconf.get("gemini.model", "gemini-2.5-flash")
    body = {"contents": [{"parts": [{"text": GEMINI_PROMPT.format(q=q)}]}], "tools": [{"google_search": {}}]}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    import urllib.error
    for wait in (0, 30, 60, 90):   # 無料の枠の「1分あたりの回数」に当たったら待って取り直す
        time.sleep(wait)
        try:
            r = json.load(urllib.request.urlopen(req, timeout=180))
            break
        except urllib.error.HTTPError as ex:
            body_ = ex.read().decode("utf-8", "replace")
            # 1日の上限（無料枠は1モデル1日20回）に当たったら、待っても戻らないのでその回を止める
            if ex.code == 429 and "PerDay" in body_:
                raise DailyLimit(body_[:200])
            if ex.code != 429 or wait == 90:
                raise
    c = (r.get("candidates") or [{}])[0]
    txt = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []))
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        recs = json.loads(m.group(0)).get("recommendations", []) if m else []
    except ValueError:
        recs = []
    recs = [{"name": str(x.get("name", "")), "url": str(x.get("url", "")), "source_url": str(x.get("source_url", ""))}
            for x in recs if isinstance(x, dict) and x.get("name")][:5]
    gm = c.get("groundingMetadata") or {}
    srcs = [(ch.get("web") or {}).get("title", "") for ch in gm.get("groundingChunks", [])]
    # 根拠のサイト名が返らない応答もある。そのときは Gemini が Google で検索した語を残す（検索を使ったことの記録）
    srcs = [x for x in srcs if x] or [f"検索: {w}" for w in gm.get("webSearchQueries", [])]
    return recs, srcs, (r.get("usageMetadata") or {}).get("totalTokenCount")


def host(u):
    m = re.match(r"https?://([^/:?#]+)", (u or "").strip().lower())
    return m.group(1).removeprefix("www.") if m else ""


def match(rec, brands):
    """推薦1件が、どの自社の名前に当たるか（生成AIを使わない）。URL はホスト名の一致で見る
    （kurage.exbridge.jp を exbridge.jp に数えないため）。名前の語は name の欄だけで見る"""
    hosts = {host(rec.get("url")), host(rec.get("source_url"))} - {""}
    name = (rec.get("name") or "").lower()
    for b in brands:   # 店（Kurage App Store）を kurage の「Kurage 」に数えない
        for x in b.get("exclude", []):
            name_b = name.replace(x.lower(), "")
            b["_name"] = name_b
    return [b["key"] for b in brands if hosts & set(b["domains"]) or
            any(a.lower() in b.get("_name", name) for a in b["aliases"])]


def run(a):
    qs = load("questions.json")["questions"]
    if a.only:
        qs = [q for q in qs if q["id"] in a.only.split(",")]
    now = datetime.datetime.now(JST)
    stamp = now.strftime("%Y%m%d-%H%M")
    work = os.path.join(ROOT, "data", "work", stamp)
    os.makedirs(work, exist_ok=True)
    os.makedirs(RUNS, exist_ok=True)
    engine = getattr(a, "engine", "codex") or "codex"
    got, tokens, srcs = {}, [], {}
    if engine == "gemini":
        # 無料の枠は1モデル1日20回。検索数の多い順に max_questions 問だけ聞く（2026-11-02）
        mx = int(kmconf.get("gemini.max_questions", 18))
        if not a.only:
            qs = sorted(qs, key=lambda q: -(q.get("vol") or 0))[:mx]
        for i, q in enumerate(qs, 1):
            try:
                recs, sv, t = ask_gemini(q["q"])
            except DailyLimit:
                print(f"  {i}/{len(qs)} で1日の上限。ここで止めます", flush=True)
                qs = qs[:i - 1]
                break
            except Exception as ex:   # 1問の失敗で止めない
                print(f"  {i}/{len(qs)} 失敗: {str(ex)[:120]}", flush=True)
                recs, sv, t = [], [], None
            got[q["id"]], srcs[q["id"]] = recs, sv
            tokens.append(t)
            print(f"  {i}/{len(qs)} 問（{t or '?'} トークン）", flush=True)
            time.sleep(float(kmconf.get("gemini.interval_sec", 8)))   # 無料の枠は1分あたりの回数に上限がある
    else:
        for i in range(0, len(qs), a.batch):
            part = qs[i:i + a.batch]
            res, t = ask(part, work)
            got.update(res)
            tokens.append(t)
            print(f"  {i + len(part)}/{len(qs)} 問（{t or '?'} トークン）", flush=True)
    if not qs:
        print("1問も聞けなかったので、この回は記録しません")
        shutil.rmtree(work, ignore_errors=True)
        return None
    brands = load("brands.json")["brands"]
    rows = []
    for q in qs:
        recs = got.get(q["id"], [])
        for r in recs:
            r["own"] = match(r, brands)
        rows.append({**q, "recommendations": recs, **({"sources": srcs.get(q["id"], [])} if engine == "gemini" else {})})
    model = kmconf.get("gemini.model", "gemini-2.5-flash") if engine == "gemini" else (MODEL or "Codex の既定")
    run_ = {"at": now.isoformat(timespec="minutes"), "model": model, "engine": "gemini google_search" if engine == "gemini" else "codex --search",
            "tokens": tokens, "answers": rows}
    os.makedirs(RUNS_BY[engine], exist_ok=True)
    p = os.path.join(RUNS_BY[engine], f"{stamp}.json")
    json.dump(run_, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    shutil.rmtree(work, ignore_errors=True)
    hit = sum(1 for r in rows if any(x["own"] for x in r["recommendations"]))
    print(f"{p}  当社が挙がった質問 {hit}/{len(rows)}")
    return p


# ---- 画面 ----
def runs(engine="codex"):
    return [json.load(open(f, encoding="utf-8")) for f in sorted(glob.glob(os.path.join(RUNS_BY[engine], "*.json")))]


def rank_of(recs, key=None):
    """当社（key 指定ならその名前）が最初に出た順位。出なければ None"""
    for i, r in enumerate(recs, 1):
        if (key and key in r.get("own", [])) or (not key and r.get("own")):
            return i
    return None


def build(a=None):
    engine = getattr(a, "engine", "codex") or "codex"
    rs = runs(engine)
    if not rs:
        if engine != "codex":
            print(f"{engine} の記録はまだありません")
            return None
        sys.exit("まだ記録がありません（kmention.py run を先に）")
    last, prev = rs[-1], (rs[-2] if len(rs) > 1 else None)
    brands = load("brands.json")["brands"]
    e = html.escape
    pv = {r["id"]: r for r in (prev or {}).get("answers", [])}
    qv = {q["id"]: q for q in load("questions.json")["questions"]}   # 検索数（vol）はいまの設定から付ける
    for r in last["answers"]:
        r["kw"], r["vol"] = qv.get(r["id"], {}).get("kw"), qv.get(r["id"], {}).get("vol")
    last["answers"].sort(key=lambda r: -(r.get("vol") or 0))
    total = len(last["answers"])
    hit = sum(1 for r in last["answers"] if rank_of(r["recommendations"]))
    vtot = sum(r.get("vol") or 0 for r in last["answers"])
    # 順位でも割り引く（1番目＝全部・2番目＝半分・3番目＝3分の1）。月11万回の1問が4番目でも全部に数えられないように
    vhit = round(sum((r.get("vol") or 0) / rank_of(r["recommendations"]) for r in last["answers"] if rank_of(r["recommendations"])))
    by_brand = {b["key"]: sum(1 for r in last["answers"] if rank_of(r["recommendations"], b["key"])) for b in brands}
    others = {}
    for r in last["answers"]:
        for x in r["recommendations"]:
            if not x.get("own"):
                others.setdefault(x["name"], set()).add(r["id"])
    top_others = sorted(others.items(), key=lambda kv: -len(kv[1]))[:15]
    cards = []
    for r in last["answers"]:
        rk = rank_of(r["recommendations"])
        pr = rank_of(pv[r["id"]]["recommendations"]) if r["id"] in pv else "-"
        own_pages = sorted({x.get("source_url") or x.get("url") for x in r["recommendations"] if x.get("own")})
        lis = "".join(
            f'<li class="{"own" if x.get("own") else ""}"><a href="{e(x.get("url") or "#")}" target="_blank" rel="noopener">{e(x["name"])}</a>'
            + (f'<span class="badge">当社</span>' if x.get("own") else "") + "</li>" for x in r["recommendations"])
        state = f'<b class="hit">{rk}番目</b>' if rk else '<b class="miss">挙がらず</b>'
        prev_s = "" if pr == "-" else f'<span class="prev">前回 {pr}番目</span>' if pr else '<span class="prev">前回 挙がらず</span>'
        vol = (f'<span class="vol">月{r["vol"]:,}回（{e(r["kw"])}）</span>' if r.get("vol") is not None else
               '<span class="vol">今の質問一覧から外した質問</span>' if r["id"] not in qv else '<span class="vol">検索数なし（ChatGPTらしい聞き方）</span>')
        cards.append(f'''<article class="q"><div class="meta"><span class="cat">{e(r["cat"])}</span>{state}{prev_s}{vol}</div>
<h2>{e(r["q"])}</h2><ol>{lis or "<li>勧めるものなし</li>"}</ol>
{''.join(f'<p class="src">根拠にされた当社のページ: <a href="{e(u)}" target="_blank" rel="noopener">{e(u)}</a></p>' for u in own_pages if u)}</article>''')
    hist = "".join(f'<li>{e(x["at"][:16].replace("T", " "))}　{sum(1 for r in x["answers"] if rank_of(r["recommendations"]))}/{len(x["answers"])}問</li>'
                   for x in reversed(rs[-12:]))
    kw_links = "".join(f'<p class="lead"><a href="{e(os.path.basename(k))}">強化すべきキーワード（{e(os.path.basename(k)[9:-5])}）を見る</a></p>'
                       for k in sorted(glob.glob(os.path.join(OUT, "keywords-*.html"))))
    others_page = [x for x in PAGE_BY if x != engine and os.path.exists(os.path.join(OUT, PAGE_BY[x]))]
    switch = "答え手：" + "　".join(f"<b>{e(LABEL_BY[x])}</b>" if x == engine else f'<a href="{e(PAGE_BY[x])}">{e(LABEL_BY[x])}</a>'
                                   for x in [engine] + others_page)
    note = ("ChatGPT と同じ OpenAI のモデルが Web を検索して答えたもので、chatgpt.com の画面の答えそのものではありません。" if engine == "codex" else
            "Gemini の API が Google 検索を使って答えたもので、gemini.google.com の画面の答えそのものではありません。")
    page = f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kurage AI言及トラッカー</title><meta name="robots" content="noindex,nofollow">
<style>
:root{{--bg:#f6f8f9;--paper:#fff;--ink:#16232b;--sub:#56656f;--line:#dfe6ea;--teal:#0a7a6e;--teal-l:#e3f3f0;--red:#b3261e}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font-family:"Noto Sans JP","Hiragino Sans",system-ui,sans-serif;line-height:1.7}}
header{{background:var(--paper);border-bottom:1px solid var(--line)}}.wrap{{max-width:1080px;margin:0 auto;padding:12px 16px}}
.brand{{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--ink);font-weight:800}}.brand img{{width:40px;height:auto;object-fit:contain}}
h1{{font-size:clamp(20px,3vw,26px);margin:14px 0 4px}}.lead{{color:var(--sub);font-size:14px;margin:0 0 12px}}
.sum{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:10px 0 18px}}
.sum div{{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:10px 12px}}.sum b{{display:block;font-size:22px;color:var(--teal)}}.sum span{{font-size:13px;color:var(--sub)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(320px,100%),1fr));gap:12px}}
.q{{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:12px 14px;min-width:0}}
.q h2{{font-size:15px;line-height:1.55;margin:6px 0}}.meta{{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;font-size:13px}}
.cat{{background:var(--teal-l);color:var(--teal);border-radius:999px;padding:0 8px;font-weight:700}}.hit{{color:var(--teal)}}.miss{{color:var(--red)}}.prev{{color:var(--sub)}}.vol{{color:var(--sub);font-size:12px}}
ol{{margin:4px 0 6px;padding-left:1.3em;font-size:14px}}li{{overflow-wrap:anywhere}}li.own{{font-weight:800}}.badge{{margin-left:6px;background:var(--teal);color:#fff;border-radius:999px;padding:0 6px;font-size:11px}}
a{{color:#0b5cad}}.src{{font-size:12.5px;color:var(--sub);margin:2px 0;overflow-wrap:anywhere}}
section{{margin:20px 0}}h3{{font-size:16px;margin:0 0 6px}}.box{{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:12px 14px;font-size:14px}}
</style></head><body>
<header><div class="wrap"><a class="brand" href="./"><img src="https://kurage.exbridge.jp/images/kurage-mascot-cutout.png" alt="Kurage" width="40" height="40">Kurage AI言及トラッカー</a></div></header>
<main class="wrap">
<h1>AI の回答に、当社は挙がっているか</h1>
{kw_links}
<p class="lead">{switch}</p>
<p class="lead">検索数（Google の月間検索数・同じことを Google で探すときの語）の多い順。{e(last["at"][:16].replace("T", " "))} の回（{e(last["model"])}・{total}問）。{note}</p>
<div class="sum"><div><b>{hit}/{total}</b><span>当社が挙がった質問</span></div><div><b>{(vhit / vtot if vtot else 0):.0%}</b><span>検索数と順位で重みを付けた割合（各質問の月間検索数÷順位の合計 {vhit:,} ／ 全体 {vtot:,}。1番目なら全部・2番目なら半分）</span></div>
{''.join(f'<div><b>{by_brand[b["key"]]}</b><span>{e(b["name"])}</span></div>' for b in brands)}</div>
<div class="grid">{''.join(cards)}</div>
<section><h3>よく一緒に挙がる他社・サイト</h3><div class="box">{"、".join(f"{e(n)}（{len(s)}問）" for n, s in top_others) or "なし"}</div></section>
<section><h3>回ごとの推移</h3><div class="box"><ul>{hist}</ul></div></section>
</main></body></html>'''
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, PAGE_BY[engine])
    open(p, "w", encoding="utf-8").write(page)
    print(p)
    return p


# ---- 配置・メール（公開先とメールは config/settings.json）----
def url():
    return kmconf.public_url()


def deploy(_=None):
    print(kmconf.publish(kmconf.outputs_to_publish()))


def mail(_=None):
    rs = runs()
    last, prev = rs[-1], (rs[-2] if len(rs) > 1 else None)
    pv = {r["id"]: r for r in (prev or {}).get("answers", [])}
    qv = {q["id"]: q for q in load("questions.json")["questions"]}
    last["answers"].sort(key=lambda r: -(qv.get(r["id"], {}).get("vol") or 0))
    hit = sum(1 for r in last["answers"] if rank_of(r["recommendations"]))
    lines = [f"Kurage AI言及トラッカー {last['at'][:16].replace('T', ' ')} ／ 当社が挙がった質問 {hit}/{len(last['answers'])}", "",
             f"一覧ページ: {url() or os.path.join(OUT, 'index.html')}", ""]
    for r in last["answers"]:
        rk = rank_of(r["recommendations"])
        pr = rank_of(pv[r["id"]]["recommendations"]) if r["id"] in pv else None
        mark = f"{rk}番目" if rk else "挙がらず"
        if r["id"] in pv:
            mark += f"（前回 {pr}番目）" if pr else "（前回 挙がらず）"
        v = qv.get(r["id"], {}).get("vol")
        lines.append(f"・{mark}　{r['q']}" + (f"（月{v:,}回）" if v else ""))
    gr = runs("gemini")
    if gr:
        gl = gr[-1]
        gh = sum(1 for r in gl["answers"] if rank_of(r["recommendations"]))
        lines += ["", f"Gemini（Google 検索つき）{gl['at'][:16].replace('T', ' ')}：当社が挙がった質問 {gh}/{len(gl['answers'])}",
                  f"Gemini の一覧ページ: {(url() + 'gemini.html') if url() else os.path.join(OUT, 'gemini.html')}"]
    lines += ["", "ChatGPT と同じ OpenAI のモデルが Web を検索して答えたもので、chatgpt.com の画面の答えそのものではありません。",
              "このメールは Kurage AI言及トラッカー（kmention）から自動で送っています。"]
    if kmconf.send_mail(f"AI言及トラッカー 当社 {hit}/{len(last['answers'])}問", "\n".join(lines)):
        print("sent")


def weekly(a):
    """cron から週1回。質問 → 画面 → 公開 → メール"""
    run(a)
    build()
    if kmconf.get("gemini.enabled", False):
        try:
            a.engine = "gemini"
            run(a)
            build(a)
        except Exception as ex:   # Gemini が失敗しても、ChatGPT 側の公開とメールは続ける
            print(f"Gemini の回は失敗: {str(ex)[:200]}")
        a.engine = "codex"
    deploy()
    mail()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "build", "deploy", "mail", "weekly"])
    ap.add_argument("--only")
    ap.add_argument("--batch", type=int, default=9)
    ap.add_argument("--engine", choices=["codex", "gemini"], default="codex")
    a = ap.parse_args()
    {"run": run, "build": build, "deploy": deploy, "mail": mail, "weekly": weekly}[a.cmd](a)
