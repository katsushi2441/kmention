#!/usr/bin/env python3
"""AI に引用されやすい形か（AEO）の判定。keywords.py と pagegen.py が使う。

settings.json の aeo_php に Kurage Track & GEO & SEO の ktrackgeoseo_aeo.php があればそれで判定し、
無ければ内蔵の簡易判定（定義文・質問の見出し・根拠のない断定）で数える。どちらも同じ形の dict を返す:
  {"score": 0〜100, "engine": "...", "metrics": {"definitions": {"found": n}, "question_answers": {"questions": n},
                                                   "claim_risk": {"absolute_claims": [...]}}}
"""
import html as H
import json
import os
import re
import shutil
import subprocess
import tempfile

import kmconf

ABSOLUTE = r"必ず|絶対(?:に)?|完全(?:に)?|確実(?:に)?|唯一|業界初|日本初|世界初|日本一|世界一|No\.?\s*1|100[%％]|間違いなく|誰でも"


def _text(fragment):
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def lite(html):
    """内蔵の簡易判定。本文は <main> があればその中、無ければ <body>"""
    body = re.sub(r"<script.*?</script>|<style.*?</style>|<nav.*?</nav>|<footer.*?</footer>", " ", html, flags=re.S | re.I)
    m = re.search(r"<main\b.*?</main>", body, re.S | re.I) or re.search(r"<body\b.*?</body>", body, re.S | re.I)
    body = m.group(0) if m else body
    text = _text(body)
    defs = len(re.findall(r"[^。！？\s]{2,40}とは[、，,]", text))
    heads = [_text(x) for x in re.findall(r"<(?:h2|h3|summary|dt)[^>]*>(.*?)</(?:h2|h3|summary|dt)>", body, re.S | re.I)]
    qs = sum(1 for h in heads if re.search(r"[？?]$", h))
    claims = sorted(set(re.findall(ABSOLUTE, text)))
    score = (35 if defs else 0) + min(qs, 3) * 12 + (29 if not claims else max(0, 29 - 10 * len(claims)))
    return {"score": min(score, 100), "engine": "簡易判定", "metrics": {"definitions": {"found": defs},
            "question_answers": {"questions": qs}, "claim_risk": {"absolute_claims": claims}}}


def analyze(html):
    php = kmconf.get("aeo_php")
    if php and os.path.exists(php) and shutil.which("php"):
        with tempfile.NamedTemporaryFile("w", suffix=".html", encoding="utf-8", delete=False) as f:
            f.write(html)
        try:
            r = subprocess.run(["php", "-r", f"require {php!r}; echo json_encode(ktgs_aeo_analyze(file_get_contents({f.name!r})), JSON_UNESCAPED_UNICODE);"],
                               capture_output=True, text=True, timeout=60)
            a = json.loads(r.stdout)
            a["engine"] = "Kurage Track & GEO & SEO"
            return a
        except (ValueError, subprocess.SubprocessError):
            pass
        finally:
            os.unlink(f.name)
    return lite(html)
