#!/usr/bin/env python3
"""強化すべきキーワードと要点（会社の事実）から、SEO・AEO・GEO・LLMO の面で強いページを作る（Kurage AI言及トラッカーの一部）。
  python3 pagegen.py make specs/<名前>.json     # 下書き→点検→outputs/pages/<slug>.html と点検結果
  python3 pagegen.py deploy <slug>             # settings.json の公開先の pages/<slug>.html（既定は noindex・確認用）

2026-11-01 ユーザー指示「強化すべきキーワードや要点から、SEO・AEO・GEO・LLMO 的に強いホームページを作る機能」。
- 入れるもの（specs/*.json）: keyword・slug・会社（名前・地域・URL・問い合わせ先）・facts（製品・価格・事例・できないこと）・必要なら kw2（関連語）
- 文章の下書きはローカルの LLM（Ollama・既定 gemma4。費用なし）。**入れた事実だけで書かせる**。
- 点検（通らなければ公開しない）: 入れた事実に無い数字／根拠のない断定（必ず・確実・No.1 …）／題名の長さ（全角35字まで）／
  AEO の判定（aeo.py: 定義文・質問と答え）／320・390・1280px で横にはみ出さないか（playwright があるとき）
- 見た目は kmention 専用の型（web-order の型は借りない。2026-11-01 に型の引き上げ設定で崩れたため）。
"""
import argparse
import datetime
import html as H
import json
import os
import re
import subprocess
import sys
import unicodedata
import urllib.request

import aeo as AEO
import kmconf

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "outputs", "pages")
OLLAMA = os.environ.get("KMENTION_OLLAMA") or kmconf.get("llm.ollama_url", "http://127.0.0.1:11434")
LLM = kmconf.get("llm.model", "gemma4:12b-it-qat")
SHOT_PY = kmconf.get("playwright_python") or sys.executable   # playwright が入っている Python
ABSOLUTE = AEO.ABSOLUTE
e = H.escape

SCHEMA_HINT = """{
 "title": "題名（キーワードを先頭に。全角35字まで。会社名は入れない）",
 "description": "説明文（全角80〜120字。キーワードを入れる）",
 "h1": "見出し",
 "lead": "見出しの下の2〜3文",
 "definition": "「{kw}とは、〜を指します。」で始まる定義の文（2文まで）",
 "points": ["定義の下に置く要点（3つ）"],
 "problems": [{"t": "困りごとの短い見出し", "d": "1文"}],
 "steps": [{"t": "手順の見出し", "d": "1〜2文"}],
 "faq": [{"q": "人が実際に聞く質問", "a": "40〜150字の答え。最初の文で答えを言い切る"}]
}"""


def norm(s):
    return unicodedata.normalize("NFKC", s or "")


def numbers(s):
    out = set()
    for x in re.findall(r"\d+(?:\.\d+)?", norm(s).replace(",", "")):
        out.add(x.lstrip("0") or "0")
    return out


def xlen(s):
    return sum(1 if ord(c) < 0x1100 else 2 for c in s) / 2   # 全角換算


def ask(spec):
    facts = json.dumps(spec["facts"], ensure_ascii=False, indent=1)
    prompt = f"""あなたは日本の中小企業のホームページを書く編集者です。キーワード「{spec['keyword']}」で検索する人や、ChatGPT に同じことを聞く人に、まっすぐ答えるページの文章を書きます。
決まり:
- 書いてよいのは「事実」に書かれていることだけ。事実に無い数字・価格・期間・実績・効果（何割減った等）は書かない
- 「必ず」「確実」「No.1」「業界初」「誰でも」などの断定は使わない
- 事実より強い言い方にしない（「動かせる」を「どこでも動く」「環境を選ばない」にしない、「できる」を「簡単にできる」にしない）
- 会社の名前は「当社」と書く。売り込みの言葉（最高・最強・圧倒的）は使わない
- 人が口で言うような、短すぎない自然な日本語で書く
- problems は4つ、steps は4〜5つ、faq は5〜6つ

# 会社
{json.dumps(spec['company'], ensure_ascii=False)}

# 事実
{facts}

# 返す形（JSON だけを返す）
{SCHEMA_HINT.replace('{kw}', spec['keyword'])}"""
    body = json.dumps({"model": LLM, "prompt": prompt, "stream": False, "think": False, "format": "json",
                       "options": {"num_predict": 3000, "temperature": 0.3}}).encode()
    r = json.loads(urllib.request.urlopen(urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                                                 headers={"Content-Type": "application/json"}), timeout=600).read())
    return json.loads(r["response"])


def shape(d):
    """返ってきた JSON の形を確かめる。崩れていれば理由を返す"""
    try:
        for k in ("title", "description", "h1", "lead", "definition"):
            if not isinstance(d.get(k), str) or not d[k].strip():
                return k
        if not (isinstance(d.get("points"), list) and d["points"]):
            return "points"
        for k, keys in (("problems", ("t", "d")), ("steps", ("t", "d")), ("faq", ("q", "a"))):
            if not (isinstance(d.get(k), list) and d[k]) or any(not all(isinstance(x.get(y), str) and x[y] for y in keys) for x in d[k]):
                return k
    except AttributeError:
        return "全体"
    return None


def check_text(d, spec):
    """文章の点検。問題の一覧を返す（空なら通過）"""
    allowed = numbers(json.dumps(spec, ensure_ascii=False))
    issues = []
    texts = [d["title"], d["description"], d["h1"], d["lead"], d["definition"]] + d["points"] + \
            [x["t"] + x["d"] for x in d["problems"] + d["steps"]] + [x["q"] + x["a"] for x in d["faq"]]
    for t in texts:
        extra = numbers(t) - allowed
        if extra:
            issues.append(f"事実に無い数字 {sorted(extra)}: {t[:40]}")
        if re.search(ABSOLUTE, t):
            issues.append(f"断定の言葉: {t[:40]}")
    if xlen(d["title"]) > 35:
        issues.append(f"題名が長い（全角{xlen(d['title']):.0f}字）")
    if not norm(d["title"]).replace(" ", "").startswith(norm(spec["keyword"]).replace(" ", "")[:4]):
        issues.append("題名の先頭にキーワードが無い")
    if not d["definition"].startswith(spec["keyword"].replace(" ", "")) or "とは" not in d["definition"][:40]:
        issues.append("定義文が「○○とは」で始まっていない")
    return issues


CSS = """:root{--bg:#f5f8fa;--paper:#fff;--ink:#14232c;--sub:#4f616c;--line:#dbe4e9;--accent:#0a7a6e;--accent-l:#e3f3f0}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:"Noto Sans JP","Hiragino Sans",system-ui,sans-serif;line-height:1.8}
a{color:#0b5cad}img{max-width:100%;height:auto}
header.top{background:var(--paper);border-bottom:1px solid var(--line)}.wrap{max-width:1040px;margin:0 auto;padding:0 16px}
header.top .wrap{display:flex;align-items:center;justify-content:space-between;gap:10px;min-height:58px;flex-wrap:wrap}
.brand{font-weight:800;color:var(--ink);text-decoration:none}.cta-s{font-size:14px;font-weight:800;color:#fff;background:var(--accent);border-radius:999px;padding:6px 14px;text-decoration:none;white-space:nowrap}
.hero{background:linear-gradient(135deg,#0f2e3a,#0a5a52);color:#fff;padding:40px 0 34px}
.hero h1{font-size:clamp(24px,4.4vw,38px);line-height:1.35;margin:0 0 12px}.hero p{margin:0;max-width:760px;font-size:16px;opacity:.95}
section{margin:30px 0}h2{font-size:clamp(20px,3vw,26px);line-height:1.45;margin:0 0 12px}
.answer{background:var(--paper);border:1px solid var(--line);border-left:5px solid var(--accent);border-radius:12px;padding:16px 18px}
.answer h2{font-size:clamp(19px,2.6vw,23px)}.answer ul{margin:8px 0 0;padding-left:1.2em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.card{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:14px 16px}.card h3{margin:0 0 6px;font-size:16px;line-height:1.5}.card p{margin:0;color:var(--sub);font-size:14.5px}
ol.steps{counter-reset:s;list-style:none;padding:0;margin:0;display:grid;gap:10px}
ol.steps li{background:var(--paper);border:1px solid var(--line);border-radius:12px;padding:12px 14px 12px 52px;position:relative}
ol.steps li::before{counter-increment:s;content:counter(s);position:absolute;left:14px;top:12px;width:26px;height:26px;border-radius:50%;background:var(--accent-l);color:var(--accent);font-weight:800;text-align:center;line-height:26px}
ol.steps h3{margin:0 0 4px;font-size:16px}ol.steps p{margin:0;color:var(--sub);font-size:14.5px}
.faq details{background:var(--paper);border:1px solid var(--line);border-radius:10px;margin:0 0 8px;padding:10px 14px}
.faq summary{cursor:pointer;font-weight:800}.faq summary h3{display:inline;font-size:inherit;margin:0}.faq p{margin:8px 0 2px;color:var(--sub)}
.cta{background:var(--paper);border:1px solid var(--line);border-radius:14px;padding:20px;text-align:center}
.cta a{display:inline-block;margin-top:10px;background:var(--accent);color:#fff;font-weight:800;border-radius:999px;padding:10px 22px;text-decoration:none}
footer{color:var(--sub);font-size:13px;padding:24px 0 40px;text-align:center}"""


def render(d, spec):
    c, kw = spec["company"], spec["keyword"]
    url = c["url"].rstrip("/") + "/" + spec["slug"] + ".html"
    ld = [{"@context": "https://schema.org", "@type": "WebPage", "name": d["title"], "description": d["description"], "url": url,
           "inLanguage": "ja", "about": kw, "dateModified": datetime.date.today().isoformat(), "publisher": {"@type": "Organization", "name": c["name"], "url": c["url"]}},
          {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
              {"@type": "Question", "name": x["q"], "acceptedAnswer": {"@type": "Answer", "text": x["a"]}} for x in d["faq"]]},
          {"@context": "https://schema.org", "@type": "HowTo", "name": d["h1"],
           "step": [{"@type": "HowToStep", "name": x["t"], "text": x["d"]} for x in d["steps"]]},
          {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
              {"@type": "ListItem", "position": 1, "name": c["name"], "item": c["url"]},
              {"@type": "ListItem", "position": 2, "name": kw, "item": url}]}]
    lds = "".join('<script type="application/ld+json">' + json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "</script>\n" for x in ld)
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(d['title'])}｜{e(c['name'])}</title><meta name="description" content="{e(d['description'])}">
<link rel="canonical" href="{e(url)}"><meta property="og:type" content="website"><meta property="og:title" content="{e(d['title'])}">
<meta property="og:description" content="{e(d['description'])}"><meta property="og:url" content="{e(url)}"><meta name="twitter:card" content="summary_large_image">
{lds}<style>{CSS}</style></head><body>
<header class="top"><div class="wrap"><a class="brand" href="{e(c['url'])}">{('<img src="' + e(c['logo']) + '" alt="" width="32" height="32" style="vertical-align:middle;margin-right:8px;width:32px;height:auto;object-fit:contain">') if c.get('logo') else ''}{e(c['name'])}</a><a class="cta-s" href="{e(c['contact'])}">相談する</a></div></header>
<div class="hero"><div class="wrap"><h1>{e(d['h1'])}</h1><p>{e(d['lead'])}</p></div></div>
<main class="wrap">
<section class="answer"><h2>{e(kw)}とは</h2><p>{e(d['definition'])}</p><ul>{''.join(f'<li>{e(x)}</li>' for x in d['points'])}</ul></section>
<section><h2>こんなことで困っていませんか</h2><div class="grid">{''.join(f'<div class="card"><h3>{e(x["t"])}</h3><p>{e(x["d"])}</p></div>' for x in d['problems'])}</div></section>
<section><h2>進め方</h2><ol class="steps">{''.join(f'<li><h3>{e(x["t"])}</h3><p>{e(x["d"])}</p></li>' for x in d['steps'])}</ol></section>
<section class="faq"><h2>よくある質問</h2>{''.join(f'<details><summary><h3>{e(x["q"])}</h3></summary><p>{e(x["a"])}</p></details>' for x in d['faq'])}</section>
{sources(spec)}
<section class="cta"><h2>まずはご相談ください</h2><p>{e(c.get('cta', '困っていることを教えてください。'))}</p><a href="{e(c['contact'])}">相談する</a></section>
</main><footer>{e(c['name'])}</footer></body></html>"""


def sources(spec):
    """このページの根拠（出典と更新日）。AEO の「根拠」の観点で、出典と日付の無いページは引用されにくい"""
    src = spec.get("sources") or []
    if not src:
        return ""
    today = datetime.date.today().strftime("%Y年%m月%d日")
    lis = "".join(f'<li><a href="{e(x["url"])}">{e(x["name"])}</a></li>' for x in src)
    return f'<section><h2>このページの根拠</h2><p>参照: 次のページの内容をもとに書いています（{today}更新）。</p><ul>{lis}</ul></section>'


def check_page(path):
    issues, info = [], {}
    try:
        a = AEO.analyze(open(path, encoding="utf-8").read())
        m = a["metrics"]
        info["aeo"] = a["score"]
        if not m["definitions"]["found"]:
            issues.append("AEO: 定義文が数えられない")
        if not m["question_answers"]["questions"]:
            issues.append("AEO: 質問と答えが数えられない")
        if m["claim_risk"]["absolute_claims"]:
            issues.append(f"AEO: 断定 {m['claim_risk']['absolute_claims']}")
    except (ValueError, KeyError):
        issues.append("AEO の判定に失敗")
    shot = f"""
from playwright.sync_api import sync_playwright
import json
res={{}}
with sync_playwright() as p:
    b=p.chromium.launch()
    for w in (320,390,1280):
        pg=b.new_page(viewport={{'width':w,'height':900}}); pg.goto('file://{path}'); pg.wait_for_timeout(300)
        res[w]=pg.evaluate('document.documentElement.scrollWidth'); pg.close()
    b.close()
print(json.dumps(res))"""
    r = subprocess.run([SHOT_PY, "-c", shot], capture_output=True, text=True, timeout=120)
    try:
        widths = json.loads(r.stdout.strip().splitlines()[-1])
        info["widths"] = widths
        for w, sw in widths.items():
            if int(sw) > int(w):
                issues.append(f"{w}px で横にはみ出す（{sw}px）")
    except (ValueError, IndexError):
        issues.append("表示の確認に失敗")
    return issues, info


def make(a):
    spec = json.load(open(a.spec, encoding="utf-8"))
    os.makedirs(OUT, exist_ok=True)
    for attempt in range(1, a.tries + 1):
        d = ask(spec)
        bad = shape(d)
        if bad:   # gemma4 が形を崩すことがある（FAQ の答えが抜ける等）。崩れていたら書き直させる
            issues = [f"形が崩れている: {bad}"]
            print(f"  下書き{attempt}: {issues[0]}", flush=True)
            continue
        issues = check_text(d, spec)
        print(f"  下書き{attempt}: 文章の問題 {len(issues)}件", flush=True)
        if not issues:
            break
        for x in issues[:6]:
            print("    -", x)
    if shape(d):
        print("通らず  何度書き直させても形が崩れた（gemma4）")
        return False
    path = os.path.join(OUT, spec["slug"] + ".html")
    open(path, "w", encoding="utf-8").write(render(d, spec))
    page_issues, info = check_page(path)
    report = {"at": datetime.datetime.now().isoformat(timespec="minutes"), "spec": a.spec, "text_issues": issues,
              "page_issues": page_issues, **info, "draft": d}
    json.dump(report, open(path[:-5] + ".check.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = not issues and not page_issues
    print(("通過" if ok else "通らず") + f"  AEO {info.get('aeo')}  幅 {info.get('widths')}  {path}")
    for x in issues + page_issues:
        print("  -", x)
    return ok


def deploy(a):
    print(kmconf.publish([(os.path.join(OUT, a.slug + ".html"), f"pages/{a.slug}.html")]) + "pages/" + a.slug + ".html")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("spec")
    m.add_argument("--tries", type=int, default=3)
    dp = sub.add_parser("deploy")
    dp.add_argument("slug")
    a = ap.parse_args()
    sys.exit(0 if {"make": make, "deploy": deploy}[a.cmd](a) in (True, None) else 1)
