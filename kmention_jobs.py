#!/usr/bin/env python3
"""定期実行の入口（当社は kdeck の kmention-weekly から毎時呼ぶ。cron なら `kmention.py weekly` を週1回でよい）。
週1回・月曜7時台の最初の1回だけ、質問を投げて画面を作り直し、メールで知らせる。それ以外はすぐ戻る（Codex を呼ばない）。
"""
import datetime
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
WEEKDAY, HOUR = 0, 7   # 月曜7時台（JST）
DONE = os.path.join(ROOT, "data", "last_week.txt")


def _run(args, timeout):
    r = subprocess.run([PY, os.path.join(ROOT, "kmention.py")] + args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout or "")[-1500:], (r.stderr or "")[-1500:]


def run_weekly_job(**_) -> dict:
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    week = now.strftime("%G-W%V")
    done = open(DONE).read().strip() if os.path.exists(DONE) else ""
    if now.weekday() != WEEKDAY or now.hour < HOUR or done == week:
        return {"ok": True, "items": 0, "skipped": f"{now:%a %H}時（動くのは月曜7時台の1回・済み {done or 'なし'}）"}
    with open(DONE, "w") as f:
        f.write(week)   # 失敗しても同じ週に何度も Codex を呼ばない（Codex の週の枠を守る）
    steps = []
    for name, args, to in [("質問", ["run", "--batch", "9"], 3000), ("画面", ["build"], 300), ("配置", ["deploy"], 300), ("メール", ["mail"], 120)]:
        code, out, err = _run(args, to)
        steps.append({"step": name, "code": code, "tail": out.strip().splitlines()[-1:] or [""]})
        if code != 0:
            return {"ok": False, "items": 0, "failed": name, "stderr": err[-600:], "steps": steps}
    return {"ok": True, "items": 1, "steps": steps}


if __name__ == "__main__":
    import json
    print(json.dumps(run_weekly_job(), ensure_ascii=False, indent=1))
