#!/usr/bin/env python3
"""設定（config/settings.json）と秘密（.env／環境変数）を読み、公開・メールを行う。kmention.py・keywords.py・pagegen.py が使う。

- settings.json が無ければ settings.example.json を使う（そのままでも「質問を投げて画面を作る」までは動く）。
- パスワード・APIキーは settings.json に書かない。`*_env` に環境変数の名前を書き、値は環境変数か .env に置く。
- 公開先の `{token}` は data/.token（初回に作る推測されにくい番号）に置き換わる。
"""
import ftplib
import glob
import io
import json
import os
import re
import secrets
import shutil
import smtplib
import ssl

ROOT = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(ROOT, "config")
_S = None


def settings():
    global _S
    if _S is None:
        p = os.path.join(CONF, "settings.json")
        if not os.path.exists(p):
            p = os.path.join(CONF, "settings.example.json")
        _S = json.load(open(p, encoding="utf-8"))
    return _S


def get(path, default=None):
    """settings の入れ子を "a.b" で引く"""
    v = settings()
    for k in path.split("."):
        if not isinstance(v, dict) or k not in v:
            return default
        v = v[k]
    return default if v in ("", None) else v


def path_of(p):
    return os.path.join(ROOT, os.path.expanduser(p)) if p else ""


def secret(name):
    """環境変数 → env_file（既定は .env）の順に探す。値は表示しない"""
    if not name:
        return ""
    if os.environ.get(name):
        return os.environ[name]
    f = path_of(get("env_file", ".env"))
    if os.path.exists(f):
        for ln in open(f, encoding="utf-8"):
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", ln)
            if m and m.group(1) == name:
                return m.group(2).strip().strip("\"'")
    return ""


def token():
    p = os.path.join(ROOT, "data", ".token")
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(secrets.token_hex(6))
    return open(p).read().strip()


def public_url(rel=""):
    u = get("publish.public_url", "")
    return (u.replace("{token}", token()).rstrip("/") + "/" + rel) if u else ""


def publish(files):
    """files = [(手元のパス, 公開先での相対パス)]。publish.method が ftps ならまとめて1接続で、dir ならフォルダへ写す"""
    method = get("publish.method", "none")
    if method == "none":
        print("公開しない設定です（settings.json の publish.method）。outputs/ を見てください")
        return ""
    if method == "dir":
        base = path_of(get("publish.dir").replace("{token}", token()))
        for src, rel in files:
            os.makedirs(os.path.dirname(os.path.join(base, rel)), exist_ok=True)
            shutil.copyfile(src, os.path.join(base, rel))
        return public_url() or base
    if method != "ftps":
        raise SystemExit(f"publish.method は ftps / dir / none のどれか（いま {method}）")
    base = get("publish.remote_dir").replace("{token}", token()).rstrip("/")
    f = ftplib.FTP_TLS(secret(get("publish.host_env", "FTP_HOST")), timeout=60)
    f.login(secret(get("publish.user_env", "FTP_USER")), secret(get("publish.password_env", "FTP_PASS")))
    f.prot_p()
    made = set()
    for d in [base] + sorted({os.path.dirname(f"{base}/{rel}") for _, rel in files}):
        if d not in made:
            try:
                f.mkd(d)
            except ftplib.error_perm:
                pass
            made.add(d)
    if get("publish.noindex", True):   # 確認用の画面を検索に出さない
        f.storbinary(f"STOR {base}/.htaccess", io.BytesIO(b'Header set X-Robots-Tag "noindex, nofollow"\nOptions -Indexes\n'))
    for src, rel in files:
        f.storbinary(f"STOR {base}/{rel}", open(src, "rb"))
    f.quit()
    return public_url()


def outputs_to_publish():
    """outputs/ の画面（index.html・gemini.html・keywords-*.html・pages/*.html）"""
    out = os.path.join(ROOT, "outputs")
    fs = [os.path.join(out, "index.html"), os.path.join(out, "gemini.html")] + glob.glob(os.path.join(out, "keywords-*.html")) + glob.glob(os.path.join(out, "pages", "*.html"))
    return [(p, os.path.relpath(p, out)) for p in fs if os.path.exists(p)]


def send_mail(subject, body):
    """mail.to が空なら送らない。465 は SSL、それ以外は STARTTLS"""
    from email.header import Header
    from email.mime.text import MIMEText
    to = get("mail.to", [])
    if not to:
        print("メールの宛先が無いので送りません（settings.json の mail.to）")
        return False
    to = [to] if isinstance(to, str) else to
    sender = get("mail.from") or get("mail.user")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = Header(subject, "utf-8"), sender, ", ".join(to)
    host, port = get("mail.smtp_host", "smtp.gmail.com"), int(get("mail.smtp_port", 465))
    pw = secret(get("mail.password_env", "SMTP_PASSWORD")).replace(" ", "")
    if port == 465:
        sm = smtplib.SMTP_SSL(host, port, timeout=60, context=ssl.create_default_context())
    else:
        sm = smtplib.SMTP(host, port, timeout=60)
        sm.starttls(context=ssl.create_default_context())
    with sm:
        if get("mail.user"):
            sm.login(get("mail.user"), pw)
        sm.sendmail(sender, to, msg.as_string())
    return True
