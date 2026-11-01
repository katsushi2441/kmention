#!/usr/bin/env python3
"""ホームページから、SEO・AEO・GEO・LLMO の面で強化すべきキーワードを抽出する（Kurage AI言及トラッカーの一部）。
  python3 keywords.py run <site>      # 読んで判定し data/keywords/<site>-<日時>.json に残す（<site> は config/sites.json の名前）
  python3 keywords.py build <site>    # outputs/keywords-<site>.html を作る

2026-11-01 ユーザー指示「ホームページから強化すべきキーワードを抽出する機能」。同じ日に手でした判断
（「AI 業務効率化」は答えるページが無いので作る、「AI開発会社」は ai-development を直す）を自動で出す。
1. サイトマップから config/sites.json の範囲のページを読む（題名・見出し・説明文・質問・本文、AEO の判定）
2. ページごとに、そのページを探す人が打つ検索語の候補をローカルの LLM（Ollama・既定 gemma4）で出す（費用なし・think:false）
3. キーワードプランナー（Google 広告 API）で候補と関連語の月間検索数を測り、より検索される言い方も拾う
4. Search Console の順位・表示回数、kmention の結果（AI に挙がるか）と突き合わせる（Search Console は設定があるときだけ）
5. 語ごとに「新しいページを作る／題名と冒頭を直す／足りている」を決め、検索数×足りなさで並べる
判定（作る・直す）に生成AIは使わない。gemma4 は候補の語を出すところだけ。
"""
import argparse
import datetime
import glob
import html as H
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

import aeo as AEO
import kmconf

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "outputs")
DATA = os.path.join(ROOT, "data", "keywords")
OLLAMA = os.environ.get("KMENTION_OLLAMA") or kmconf.get("llm.ollama_url", "http://127.0.0.1:11434")
LLM = kmconf.get("llm.model", "gemma4:12b-it-qat")
UA = "Mozilla/5.0 (Kurage AI mention tracker; kmention keywords)"
JST = datetime.timezone(datetime.timedelta(hours=9))
MIN_VOL = 50   # これより検索の少ない語は並べない（ユーザー指摘「みんなが聞いてないことで上位でも意味ない」）


def norm(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "").lower())


PARTICLES = {"の", "と", "は", "が", "を", "に", "で", "へ", "や", "も", "な"}


def tokens(s):
    """語の部分。キーワードプランナーは「ストレス チェック と は」のように助詞まで区切るので、助詞は外す"""
    return [t for t in re.split(r"\s+", unicodedata.normalize("NFKC", s or "").lower().strip()) if t and t not in PARTICLES]


def key_of(s):
    """言い方違い（在庫 管理／在庫 の 管理／管理 在庫）を1つにまとめる鍵"""
    return "".join(sorted(tokens(s)))


def show(s):
    """キーワードプランナーの区切りの空白を外して読みやすくする（英数字どうしの間だけ空白を残す）"""
    ts = re.split(r"\s+", (s or "").strip())
    ts = [t.upper() if re.fullmatch(r"[a-z]{1,5}", t) else t for t in ts]   # ai・it・aio・llmo は大文字で
    out = ts[0] if ts else ""
    for t in ts[1:]:
        out += (" " if re.match(r"[A-Za-z0-9]", t) and re.search(r"[A-Za-z0-9]$", out) else "") + t
    return out


def get(url, timeout=30):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout).read().decode("utf-8", "ignore")


# ---- 1. ページを読む ----
def site_urls(cfg):
    urls, todo, seen = [], list(cfg["sitemaps"]), set()
    while todo:
        sm = todo.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            x = get(sm)
        except Exception:
            continue
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", x):
            loc = H.unescape(loc)
            if re.search(r"sitemap[^/]*\.(xml|php)$", loc):
                todo.append(loc)
            elif any(re.search(p, loc) for p in cfg["include"]) and not any(e in loc for e in cfg.get("exclude", [])):
                urls.append(loc)
    return list(dict.fromkeys(urls))[:cfg["max_pages"]]


def text_of(fragment):
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def aeo(html):
    return AEO.analyze(html)


def page_info(url):
    h = get(url)
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", h, flags=re.S)
    m = lambda rx: text_of((re.search(rx, h, re.S | re.I) or [None, ""])[1])
    a = aeo(h)
    return {"url": url, "title": m(r"<title>(.*?)</title>"), "h1": m(r"<h1[^>]*>(.*?)</h1>"),
            "desc": H.unescape((re.search(r'<meta name="description" content="([^"]*)"', h) or [None, ""])[1]),
            "h2": [text_of(x) for x in re.findall(r"<h2[^>]*>(.*?)</h2>", body, re.S)][:12],
            "faq": [text_of(x) for x in re.findall(r"<summary[^>]*>(.*?)</summary>", body, re.S)][:10],
            "aeo": {"score": a.get("score"), "definitions": (a.get("metrics") or {}).get("definitions", {}).get("found"),
                    "questions": (a.get("metrics") or {}).get("question_answers", {}).get("questions"),
                    "claims": (a.get("metrics") or {}).get("claim_risk", {}).get("absolute_claims")},
            "text": text_of(body)[:1500]}


# ---- 2. 候補の語（gemma4）----
def candidates(p):
    prompt = ("次のウェブページを、Google で探す人が打つ検索語を3つ考えてください。日本の中小企業や一般の人が実際に打つ、"
              "2〜3語の短い検索語にします。会社名や製品名は入れません。JSON の文字列の配列だけを返してください。\n\n"
              f"題名: {p['title']}\n見出し: {p['h1']}\n説明: {p['desc']}\n小見出し: {' / '.join(p['h2'][:8])}\n本文の冒頭: {p['text'][:500]}")
    body = json.dumps({"model": LLM, "prompt": prompt, "stream": False, "think": False,
                       "options": {"num_predict": 160, "temperature": 0.2}}).encode()
    try:
        r = json.loads(urllib.request.urlopen(urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                                                     headers={"Content-Type": "application/json"}), timeout=120).read())
        arr = json.loads(re.search(r"\[.*\]", r.get("response", ""), re.S).group(0))
        return [str(x).strip() for x in arr if str(x).strip()][:3]
    except Exception:
        return []


# ---- 3. 検索数（キーワードプランナー）----
def volumes(seeds):
    """{正規化した語: (検索数, 表記)}。種の語と関連語の両方。10語ずつ"""
    if not kmconf.get("google_ads.yaml") or not kmconf.get("google_ads.customer_id"):
        sys.exit("検索数を測るには Google 広告 API（キーワードプランナー）の設定が要ります（settings.json の google_ads）")
    from google.ads.googleads.client import GoogleAdsClient
    client = GoogleAdsClient.load_from_storage(kmconf.path_of(kmconf.get("google_ads.yaml")))
    svc = client.get_service("KeywordPlanIdeaService")
    out = {}
    for i in range(0, len(seeds), 10):
        req = client.get_type("GenerateKeywordIdeasRequest")
        req.customer_id = str(kmconf.get("google_ads.customer_id")).replace("-", "")
        req.language = "languageConstants/1005"
        req.geo_target_constants.append("geoTargetConstants/2392")
        req.keyword_plan_network = client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH
        req.keyword_seed.keywords.extend(seeds[i:i + 10])
        try:
            for idea in svc.generate_keyword_ideas(request=req):
                out.setdefault(norm(idea.text), (idea.keyword_idea_metrics.avg_monthly_searches or 0, idea.text))
        except Exception as e:
            print("  キーワードプランナー失敗:", str(e)[:120], file=sys.stderr)
        time.sleep(1)
    return out


# ---- 4. Search Console・kmention ----
def gsc_rows(site):
    if not site:
        return []
    access_token = gsc_token()
    if not access_token:
        print("  Search Console の設定が無いので、順位は見ません（settings.json の search_console）", file=sys.stderr)
        return []
    body = {"startDate": (datetime.date.today() - datetime.timedelta(days=90)).isoformat(), "endDate": datetime.date.today().isoformat(),
            "dimensions": ["query", "page"], "rowLimit": 25000, "type": "web"}
    url = "https://searchconsole.googleapis.com/webmasters/v3/sites/" + urllib.parse.quote(site, safe="") + "/searchAnalytics/query"
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read()).get("rows", [])


def gsc_token():
    """search_console.authorized_user_json（client_id・client_secret・refresh_token を持つ JSON）から
    アクセストークンを取る。代わりに module_dir に access_token() を持つ gsc_oauth.py を置いてもよい"""
    p = kmconf.path_of(kmconf.get("search_console.authorized_user_json"))
    if p and os.path.exists(p):
        d = json.load(open(p, encoding="utf-8"))
        body = urllib.parse.urlencode({"client_id": d["client_id"], "client_secret": d["client_secret"],
                                       "refresh_token": d["refresh_token"], "grant_type": "refresh_token"}).encode()
        return json.loads(urllib.request.urlopen("https://oauth2.googleapis.com/token", data=body, timeout=60).read())["access_token"]
    md = kmconf.get("search_console.module_dir")
    if md and os.path.exists(os.path.join(md, "gsc_oauth.py")):
        sys.path.insert(0, md)
        from gsc_oauth import access_token
        return access_token()
    return ""


def gsc_for(kw, rows):
    ts = tokens(kw)
    hit = [r for r in rows if all(t in norm(r["keys"][0]) for t in ts)]
    if not hit:
        return None
    best = min(hit, key=lambda r: r["position"])
    return {"impressions": sum(r["impressions"] for r in hit), "clicks": sum(r["clicks"] for r in hit),
            "best_position": round(best["position"], 1), "best_query": best["keys"][0], "best_page": best["keys"][1]}


def kmention_for(kw):
    qs = {q["id"]: q for q in json.load(open(os.path.join(ROOT, "config", "questions.json"), encoding="utf-8"))["questions"]}
    runs = sorted(glob.glob(os.path.join(ROOT, "data", "runs", "*.json")))
    if not runs:
        return None
    last = json.load(open(runs[-1], encoding="utf-8"))
    for r in last["answers"]:
        q = qs.get(r["id"])
        if q and q.get("kw") and set(tokens(kw)) <= set(tokens(q["kw"])) | set(tokens(q["q"])):
            rk = next((i for i, x in enumerate(r["recommendations"], 1) if x.get("own")), None)
            return {"question": r["q"], "rank": rk}
    return None


# ---- 5. 判定 ----
def covering(kw, pages):
    """題名か見出しに、語のすべての部分を含むページ"""
    ts = [t for t in tokens(kw) if len(t) >= 2] or tokens(kw)   # 「名古屋 市」の「市」のような1文字は必須にしない
    return [p for p in pages if all(t in norm(p["title"] + " " + p["h1"]) for t in ts)]


def judge(kw, vol, pages, gsc, km):
    cov = covering(kw, pages)
    reasons = []
    if not cov:
        action, gap = "新しいページを作る", 1.0
        reasons.append("題名か見出しにこの語を持つページが無い")
    else:
        p = max(cov, key=lambda p: (p["aeo"].get("score") or 0))
        t0 = norm(p["title"])[:max(12, len(norm(kw)) + 4)]
        weak = []
        if not all(t in t0 for t in [t for t in tokens(kw) if len(t) >= 2]):
            weak.append("題名の先頭にこの語が無い")
        if not p["aeo"].get("definitions"):
            weak.append("本文に「○○とは」の定義文が無い（AEO）")
        if gsc and gsc["best_position"] > 10:
            weak.append(f"Google の順位が{gsc['best_position']}位")
        if km and not km["rank"]:
            weak.append("ChatGPT の回答に挙がらない（kmention）")
        if weak:
            action, gap = "題名と冒頭を直す", 0.6
            reasons += weak
        else:
            action, gap = "足りている", 0.1
        reasons.insert(0, f"対象のページ: {p['url']}")
    if km and not km["rank"]:
        gap *= 1.3
    return {"keyword": kw, "volume": vol, "action": action, "score": round(vol * gap), "reasons": reasons,
            "gsc": gsc, "kmention": km}


def run(a):
    cfg = json.load(open(os.path.join(ROOT, "config", "sites.json"), encoding="utf-8"))["sites"][a.site]
    os.makedirs(DATA, exist_ok=True)
    cache = os.path.join(DATA, f"_cache-{a.site}.json")
    if a.reuse and os.path.exists(cache):   # 読み直さずに判定だけやり直す（gemma4 とキーワードプランナーを呼ばない）
        c = json.load(open(cache, encoding="utf-8"))
        return decide(a, c["pages"], list(dict.fromkeys(x for p in c["pages"] for x in p["candidates"])), {k: tuple(v) for k, v in c["vol"].items()}, cfg)
    urls = site_urls(cfg)
    print(f"{len(urls)}ページを読む", flush=True)
    pages = []
    for u in urls:
        try:
            p = page_info(u)
        except Exception as e:
            print("  読めず", u, str(e)[:80])
            continue
        p["candidates"] = candidates(p)
        pages.append(p)
        print(f"  {p['title'][:30]} → {p['candidates']}", flush=True)
    seeds = list(dict.fromkeys(c for p in pages for c in p["candidates"]))
    vol = volumes(seeds)
    json.dump({"pages": [{k: v for k, v in x.items() if k != "text"} for x in pages], "vol": vol},
              open(os.path.join(DATA, f"_cache-{a.site}.json"), "w", encoding="utf-8"), ensure_ascii=False)
    decide(a, pages, seeds, vol, cfg)


def decide(a, pages, seeds, vol, cfg):
    rows = gsc_rows(cfg.get("gsc"))
    # 候補の語と、より検索される言い方（候補の語の部分をすべて含む関連語・検索数が多いもの）
    # 候補の語と、候補の語の部分をすべて含む関連語（より具体的で検索の多い言い方）。言い方違いは検索数の多いほうにまとめる
    kws = {}
    def add(v, txt):
        k = key_of(txt)
        if k and (k not in kws or v > kws[k][0]):
            kws[k] = (v, txt)
    for s in seeds:
        add(vol.get(norm(s), (0, s))[0], vol.get(norm(s), (0, s))[1])
        ts = tokens(s)
        for k, (vv, txt) in vol.items():
            if len(ts) >= 2 and all(t in k for t in ts) and len(k) <= len(norm(s)) + 6:
                add(vv, txt)
    res = []
    for k, (v, txt) in kws.items():
        if v < MIN_VOL or re.search(r"株式\s*会社|有限\s*会社|合同\s*会社|株式会社", txt):   # 他社の社名で探す語は外す
            continue
        r = judge(txt, v, pages, gsc_for(txt, rows), kmention_for(txt))   # 照合は元の区切りのまま（詰めると「名古屋システム開発」が1語になる）
        r["keyword"] = show(txt)
        res.append(r)
    res.sort(key=lambda r: -r["score"])
    stamp = datetime.datetime.now(JST).strftime("%Y%m%d-%H%M")
    p = os.path.join(DATA, f"{a.site}-{stamp}.json")
    json.dump({"site": a.site, "at": stamp, "pages": [{k: v for k, v in x.items() if k != "text"} for x in pages], "keywords": res},
              open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(p, f"語 {len(res)}件")


def build(a):
    fs = sorted(glob.glob(os.path.join(DATA, f"{a.site}-[0-9]*.json")))
    if not fs:
        sys.exit(f"まだ抽出の記録がありません（keywords.py run {a.site} を先に）")
    d = json.load(open(fs[-1], encoding="utf-8"))
    e = H.escape
    rows = []
    for r in d["keywords"]:
        g = r.get("gsc")
        km = r.get("kmention")
        rows.append(f'''<article class="k {'new' if r['action'].startswith('新') else 'fix' if r['action'].startswith('題') else 'ok'}">
<div class="top"><b>{e(r['keyword'])}</b><span class="vol">月{r['volume']:,}回</span><span class="act">{e(r['action'])}</span></div>
<ul>{''.join(f'<li>{e(x)}</li>' for x in r['reasons'])}</ul>
<p class="sub">{'Google: ' + e(g['best_query']) + ' ' + str(g['best_position']) + '位・表示' + format(g['impressions'], ',') + '回' if g else 'Google: 表示なし'}{'　ChatGPT: ' + (str(km['rank']) + '番目' if km['rank'] else '挙がらず') if km else ''}</p></article>''')
    page = f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>強化すべきキーワード｜Kurage AI言及トラッカー</title><meta name="robots" content="noindex,nofollow">
<style>
body{{margin:0;background:#f6f8f9;color:#16232b;font-family:"Noto Sans JP","Hiragino Sans",system-ui,sans-serif;line-height:1.7}}
header{{background:#fff;border-bottom:1px solid #dfe6ea}}.wrap{{max-width:1080px;margin:0 auto;padding:12px 16px}}
.brand{{display:flex;align-items:center;gap:10px;text-decoration:none;color:#16232b;font-weight:800}}.brand img{{width:40px;height:auto;object-fit:contain}}
h1{{font-size:clamp(20px,3vw,26px);margin:14px 0 4px}}.lead{{color:#56656f;font-size:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}}
.k{{background:#fff;border:1px solid #dfe6ea;border-left:5px solid #9aa8b1;border-radius:12px;padding:10px 14px;min-width:0}}.k.new{{border-left-color:#b3261e}}.k.fix{{border-left-color:#c17a00}}.k.ok{{border-left-color:#0a7a6e}}
.top{{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline}}.top b{{font-size:16px}}.vol{{color:#0a7a6e;font-weight:800}}.act{{font-size:13px;font-weight:800}}
ul{{margin:6px 0;padding-left:1.2em;font-size:13.5px;overflow-wrap:anywhere}}.sub{{margin:0;font-size:12.5px;color:#56656f;overflow-wrap:anywhere}}
</style></head><body><header><div class="wrap"><a class="brand" href="./"><img src="https://kurage.exbridge.jp/images/kurage-mascot-cutout.png" alt="Kurage" width="40" height="40">Kurage AI言及トラッカー</a></div></header>
<main class="wrap"><h1>強化すべきキーワード（{e(a.site)}）</h1>
<p class="lead">{e(d['at'])} 時点。{len(d['pages'])}ページを読み、検索語の候補（gemma4）→月間検索数（キーワードプランナー）→Google の順位（Search Console）→ChatGPT に挙がるか（kmention）で判定。月{MIN_VOL}回未満の語は出していません。赤＝新しいページを作る、黄＝題名と冒頭を直す、緑＝足りている。</p>
<div class="grid">{''.join(rows)}</div></main></body></html>'''
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, f"keywords-{a.site}.html")
    open(p, "w", encoding="utf-8").write(page)
    print(p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "build"])
    ap.add_argument("site")
    ap.add_argument("--reuse", action="store_true", help="前回読んだページと検索数を使い回し、判定だけやり直す")
    a = ap.parse_args()
    {"run": run, "build": build}[a.cmd](a)
