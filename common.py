# -*- coding: utf-8 -*-
"""共通処理：設定・状態ファイル・通知"""
import datetime as dt, json, os, smtplib
from email.mime.text import MIMEText
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://kouen.sports.metro.tokyo.lg.jp/web"
JST = dt.timezone(dt.timedelta(hours=9))
WEEK = "月火水木金土日"
STATE_PATH = os.path.join(HERE, "state.json")
QUEUE_PATH = os.path.join(HERE, "book_queue.json")


def load_config():
    return json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))


def today():
    return dt.datetime.now(JST).date()


def load_state():
    if os.path.exists(STATE_PATH):
        s = json.load(open(STATE_PATH, encoding="utf-8"))
    else:
        s = {}
    s.setdefault("notified", {})   # 通知済みの空き枠
    s.setdefault("booked", [])     # 自動予約した枠
    s.setdefault("attempts", {})   # 予約に失敗した回数
    return s


def save_state(s):
    t = today().isoformat()
    s["notified"] = {k: v for k, v in s["notified"].items() if v >= t}
    s["booked"] = [b for b in s["booked"] if b["date"] >= t]
    s["attempts"] = {k: v for k, v in s["attempts"].items() if k.split("_")[-2] >= t.replace("-", "")}
    tmp = STATE_PATH + ".tmp"
    json.dump(s, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_PATH)


def label(v):
    return f'{v["date"]}({v["wd"]}) {v["time"]}  {v["park"]} {v["court"]}'


def notify(subject, text):
    print(f"[通知] {subject}\n{text}")
    if os.environ.get("DRY_RUN"):
        return
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        requests.post(hook, json={"content": f"**{subject}**\n{text}"[:1900]}, timeout=30).raise_for_status()
    user, pw, to = (os.environ.get(k) for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "MAIL_TO"))
    if user and pw:
        msg = MIMEText(text, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"], msg["To"] = user, to or user
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as m:
            m.login(user, pw)
            m.send_message(msg)
