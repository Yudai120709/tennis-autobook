# -*- coding: utf-8 -*-
"""空き枠の監視（ログイン不要の空き状況検索だけを使う）。
条件に合う新しい空きを通知し、自動予約が有効なら予約候補を book_queue.json に書き出す。"""
import datetime as dt, html, json, os, re, sys, time, urllib.parse
import requests
from common import BASE, WEEK, QUEUE_PATH, load_config, load_state, save_state, today, notify, label

UA = {"User-Agent": "Mozilla/5.0 (personal tennis-court watcher)"}
RETRY_WAITS = (5, 15, 30)   # スポレクが遅い・つながらないときは、この秒数をあけて最大3回やり直す


def fetch(s, method, url, **kw):
    """スポレクへの1回の通信。時間切れ・接続失敗・サーバー側のエラー（500番台）はやり直す"""
    kw.setdefault("timeout", 90)
    for i in range(len(RETRY_WAITS) + 1):
        try:
            r = s.request(method, url, **kw)
            if r.status_code >= 500:
                raise requests.HTTPError(f"サーバー側のエラー {r.status_code}", response=r)
            r.raise_for_status()
            return r
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            if (code is not None and code < 500) or i == len(RETRY_WAITS):
                raise
            print(f"  スポレクの応答待ちで失敗（{str(e)[:60]}）。{RETRY_WAITS[i]}秒後にやり直します", flush=True)
            time.sleep(RETRY_WAITS[i])


CONFIG = load_config()
# 試しに1件だけ予約する（GitHub の手動実行で test_book を選んだとき）
TEST_BOOK = bool(os.environ.get("TEST_BOOK"))
if TEST_BOOK:
    # 平日に使える公園だけ・平日の昼間（19時より前）・6日後以降（キャンセルできる枠）・1件だけ
    CONFIG["targets"] = [dict(t, days="weekday") for t in CONFIG["targets"] if t.get("days", "all") != "holiday"]
    CONFIG["days_from"] = max(6, CONFIG.get("days_from", 6))
    CONFIG["days_ahead"] = min(30, CONFIG.get("days_ahead", 60))
    CONFIG.setdefault("short_notice", {})["enabled"] = False
    CONFIG.setdefault("auto_book", {})["try_per_run"] = 1
    print("【テスト】平日の昼間の枠を1件だけ予約します")


def dec(r):
    return r.content.decode("cp932", "replace")


def post(s, path, data):
    body = urllib.parse.urlencode(data, encoding="cp932")
    r = fetch(s, "POST", f"{BASE}/{path}", data=body,
              headers={**UA, "Content-Type": "application/x-www-form-urlencoded", "Referer": f"{BASE}/"})
    return dec(r)


def form_fields(page):
    f = {}
    for t in re.findall(r"<input[^>]*>", page):
        n = re.search(r'name="([^"]*)"', t)
        if not n or re.search(r'type="(checkbox|radio)"', t):
            continue
        v = re.search(r'value="([^"]*)"', t)
        f[n.group(1)] = html.unescape(v.group(1)) if v else ""
    return f


def open_search(s, sport, bld, day):
    """空き状況画面を開く（セッション作成を兼ねる）。公園名と施設（コート種別）一覧を返す"""
    f = form_fields(dec(fetch(s, "GET", f"{BASE}/", headers=UA)))
    cls, pps = sport.split("_")
    d = day.strftime("%Y-%m-%d")
    f.update(daystarthome=d, daystart=d, selectPpsClPpscd=sport, selectPpsClsCd=cls, selectPpsCd=pps,
             selectAreaBcd=bld, selectBldCd=bld, dayofweekClearFlg="1", timezoneClearFlg="1")
    page = post(s, "rsvWOpeInstSrchVacantAction.do", f)
    if "空き状況画面" not in page:
        raise RuntimeError(f"空き状況画面を開けませんでした（公園コード {bld}）")
    m = re.search(r'name="selectBldName" value="([^"]*)"', page)
    park = html.unescape(m.group(1)) if m else bld
    sel = re.search(r'<select id="facility-select".*?</select>', page, re.S)
    insts = re.findall(r'<option data-bldcd="[^"]*" value="([\d_]+)"[^>]*>([^<]*)<', sel.group(0)) if sel else []
    return park, [(i.split("_")[0], n.strip()) for i, n in insts]


def week(s, bld, inst, day):
    txt = post(s, "rsvWOpeInstSrchVacantAjaxAction.do", {
        "displayNo": "prwrc2000", "useDay": day.strftime("%Y%m%d"), "bldCd": bld,
        "instCd": inst, "transVacantMode": "0", "clearFlag": "0"})
    try:
        return json.loads(txt)
    except ValueError:
        raise RuntimeError("空き状況を取得できませんでした（セッション切れか画面変更の可能性）")


def wanted(day, start, is_holiday, tgt=None):
    """土日（設定により祝日も）は全枠、それ以外は weekday_start_from 時以降に始まる枠。
    公園ごとの "days" が "weekday" なら平日だけ、"holiday" なら土日祝だけを対象にする"""
    weekend = day.weekday() >= 5 or (is_holiday and CONFIG.get("holidays_as_weekend", False))
    days = (tgt or {}).get("days", "all")
    if days == "weekday" and weekend:
        return False
    if days == "holiday" and not weekend:
        return False
    if TEST_BOOK:
        return not weekend and start // 100 < CONFIG.get("weekday_start_from", 19)
    if weekend:
        return CONFIG.get("weekend_all", True)
    return start // 100 >= CONFIG.get("weekday_start_from", 19)


def scan_target(prio, tgt, first, last, wait):
    """1公園分を順番に確認する（公園ごとに別セッション）"""
    bld, sport = str(tgt["park"]), tgt["sport"]
    s = requests.Session()
    found = []
    park, insts = open_search(s, sport, bld, first)
    for inst, iname in insts:
        day = first
        while day <= last:
            time.sleep(wait)   # サーバーに負荷をかけない
            j = week(s, bld, inst, day)
            hol = {w["useDay"] for w in j.get("weekDay", []) if w.get("holidayFlg") == 4}
            cells = [t for r in j.get("result", []) for t in r.get("timeResult", [])]
            for r in j.get("result", []):
                for t in r.get("timeResult", []):
                    d = dt.datetime.strptime(str(t["useDay"]), "%Y%m%d").date()
                    if t.get("status") == 0 and t.get("alt") == "空き" and first <= d <= last \
                            and wanted(d, t["startTime"], t["useDay"] in hol, tgt):
                        found.append({
                            "key": f'{sport}_{bld}_{inst}_{t["useDay"]}_{t["startTime"]}',
                            "sport": sport, "bld": bld, "inst": inst, "useDay": t["useDay"],
                            "tzoneNo": r["tzoneNo"], "start": t["startTime"], "end": t["endTime"],
                            "park": park, "court": iname, "date": d.isoformat(), "wd": WEEK[d.weekday()],
                            "time": f'{t["startTime"]//100}:{t["startTime"]%100:02d}-'
                                    f'{t["endTime"]//100}:{t["endTime"]%100:02d}', "prio": prio})
            if cells and all(t.get("alt") == "受付期間外" for t in cells) and day > first:
                break  # ここから先はまだ予約受付前
            day += dt.timedelta(days=7)
    return found


def find_vacancies():
    from concurrent.futures import ThreadPoolExecutor
    t0 = today()
    first = t0 + dt.timedelta(days=CONFIG.get("days_from", 6))
    sn = CONFIG.get("short_notice", {})
    if sn.get("enabled"):
        first = min(first, t0 + dt.timedelta(days=sn.get("from_days", 0)))
    last = t0 + dt.timedelta(days=CONFIG.get("days_ahead", 60))
    wait = max(1, CONFIG.get("wait_seconds", 2))
    with ThreadPoolExecutor(max_workers=min(3, len(CONFIG["targets"]))) as ex:
        futs = [ex.submit(scan_target, i, t, first, last, wait) for i, t in enumerate(CONFIG["targets"])]
        return [v for f in futs for v in f.result()]


def lim(x):
    """上限の設定値。null・0・未設定は「上限なし」"""
    return x if isinstance(x, int) and x > 0 else None


def booking_candidates(found, state):
    ab = CONFIG.get("auto_book", {})
    if not ab.get("enabled"):
        return []
    cap = lim(ab.get("max_active"))
    if cap is not None and len(state["booked"]) >= cap:
        print(f"自動予約は上限（{cap}件）に達しているため行いません")
        return []
    booked_days = {b["date"] for b in state["booked"]}
    booked_times = {(b["date"], b.get("start")) for b in state["booked"]}
    out = []
    for v in sorted(found, key=lambda v: (v["date"], v["start"], v["prio"])):
        if state["attempts"].get(v["key"], 0) >= ab.get("max_attempts_per_slot", 2):
            continue
        if (v["date"], v["start"]) in booked_times:
            continue   # 同じ日時に別の公園を重ねて予約することはできない
        if ab.get("one_per_day", False) and v["date"] in booked_days:
            continue
        out.append(v)
    n = lim(ab.get("try_per_run"))
    return out if n is None else out[:n]


# ---- 直前の空き（days_from より前）：通知だけして、メールの返信「とって」で予約する ----
REPLY_WORDS = ("とって", "取って", "とる", "取る", "予約", "ok", "OK", "お願い")


def is_short(v):
    return v["date"] < (today() + dt.timedelta(days=CONFIG.get("days_from", 6))).isoformat()


def still_usable(v):
    """開始時刻まで min_hours_before 時間以上あるか"""
    from common import JST
    h = CONFIG.get("short_notice", {}).get("min_hours_before", 2)
    st = dt.datetime.strptime(f'{v["date"]} {v["start"]:04d}', "%Y-%m-%d %H%M").replace(tzinfo=JST)
    return st - dt.datetime.now(JST) >= dt.timedelta(hours=h)


def _top_text(body):
    """返信本文のうち、引用（元のメール）より上の部分だけ"""
    out = []
    for line in body.splitlines():
        t = line.strip()
        if t.startswith(">") or re.search(r"(wrote:|のメッセージ:|のメール:|からのメッセージ|-----Original|^On .+,|^20\d\d[/年].*[:：]$|^【テニス直前の空き】)", t):
            break
        out.append(t)
    return "\n".join(out)


def read_replies(state):
    """Gmail の受信箱から「とって」の返信を読み、予約したい枠のキーを返す（読んだメールは既読にする）"""
    import email, imaplib
    from email.header import decode_header, make_header
    user, pw = os.environ.get("GMAIL_USER"), os.environ.get("GMAIL_APP_PASSWORD")
    if not (user and pw) or os.environ.get("DRY_RUN"):
        return [], []
    allowed = {a.strip().lower() for a in (os.environ.get("MAIL_TO") or user).split(",") if a.strip()}
    allowed.add(user.lower())
    want, notes = [], []
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=30)
        m.login(user, pw)
        m.select("INBOX")
        since = (today() - dt.timedelta(days=7)).strftime("%d-%b-%Y")
        typ, data = m.search(None, f'(UNSEEN SINCE {since})')
        for num in (data[0].split() if typ == "OK" else []):
            typ, raw = m.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(raw[0][1])
            subj = str(make_header(decode_header(msg.get("Subject", ""))))
            tok = re.search(r"#(SN\d{8,})", subj)
            if not tok or tok.group(1) not in state["offers"]:
                continue   # 直前の空きのお知らせへの返信ではない
            frm = email.utils.parseaddr(msg.get("From", ""))[1].lower()
            m.store(num, "+FLAGS", "\\Seen")
            if frm not in allowed:
                notes.append(f"登録されていないアドレス（{frm}）からの返信なので無視しました")
                continue
            body = ""
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                    break
            top = _top_text(body)
            offer = state["offers"][tok.group(1)]
            if not any(w in top for w in REPLY_WORDS) and not re.search(r"\d", top):
                notes.append(f"返信（{subj}）に「とって」が見つからなかったので、何もしませんでした")
                continue
            nums = [int(x) for x in re.findall(r"\d+", top.translate(str.maketrans("０１２３４５６７８９", "0123456789")))]
            nums = [n for n in nums if 1 <= n <= len(offer)]
            picked = [offer[n - 1] for n in nums] if nums else list(offer)
            want += [k for k in picked if k not in want]
        m.logout()
    except Exception as e:
        notes.append(f"返信メールを読めませんでした：{e}")
    return want, notes


def main():
    state = load_state()
    state.setdefault("offers", {})
    found = find_vacancies()
    sn_on = CONFIG.get("short_notice", {}).get("enabled")
    short = [v for v in found if is_short(v)] if sn_on else []
    found = [v for v in found if not is_short(v)]
    new = found if TEST_BOOK else [v for v in found if v["key"] not in state["notified"]]
    print(f"条件に合う空き {len(found)} 件（うち新規 {len(new)} 件）・直前の空き {len(short)} 件")
    queue = booking_candidates(new, state)

    # 返信で「とって」と言われた直前の枠 → まだ空いていれば予約の列に加える
    if sn_on:
        want, notes = read_replies(state)
        by_key = {v["key"]: v for v in short}
        gone = []
        for k in want:
            v = by_key.get(k)
            if v and still_usable(v) and v["key"] not in {q["key"] for q in queue}:
                queue.append(v)
            else:
                gone.append(k)
        if want:
            print(f"返信で予約の依頼 {len(want)} 枠（うち空いていない {len(gone)} 枠）")
        if gone or notes:
            msg = []
            if gone:
                msg.append("返信いただいた枠のうち、次の枠はもう空いていなかったため予約できませんでした：")
                msg += ["・" + "_".join(k.split("_")[3:]) for k in gone]
            msg += notes
            notify("【テニス】返信の処理結果", "\n".join(msg))

    json.dump(queue, open(QUEUE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if TEST_BOOK:
        print("【テスト】予約する枠: " + (label(queue[0]) if queue else "なし（平日の昼間に空きがありません）"))
        json.dump(queue[:1], open(QUEUE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        save_state(state)
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            open(out, "a").write(f"book={'true' if queue else 'false'}\n")
        return
    if new:
        lines = [label(v) for v in sorted(new, key=lambda v: (v["date"], v["start"]))]
        msg = "\n".join(lines[:30]) + (f"\n…ほか {len(lines) - 30} 件" if len(lines) > 30 else "")
        if queue:
            msg += "\n\n→ 自動予約を試みます（結果は別に通知します）"
        notify(f"【テニス空き】{len(new)}件", msg + f"\n\n{BASE}/")
    for v in new:
        state["notified"][v["key"]] = v["date"]

    # 直前の空きは、新しく見つかったものだけ「返信で予約」の案内つきで通知
    sn_new = sorted([v for v in short if v["key"] not in state["notified"] and still_usable(v)],
                    key=lambda v: (v["date"], v["start"], v["prio"]))
    if sn_new:
        tok = "SN" + dt.datetime.now().strftime("%m%d%H%M%S")
        state["offers"][tok] = [v["key"] for v in sn_new[:20]]
        cancel = (today() + dt.timedelta(days=4)).isoformat()
        lines = [f"{i}. {label(v)}" + ("　※キャンセルするとペナルティ対象" if v["date"] <= cancel else "")
                 for i, v in enumerate(sn_new[:20], 1)]
        notify(f"【テニス直前の空き】{len(sn_new)}件 #{tok}",
               "\n".join(lines) + "\n\n予約したいときは、このメールに返信してください。\n"
               "・全部とる：「とって」とだけ書いて返信\n"
               "・一部だけ：番号を書いて返信（例：「1 3」）\n"
               "次の確認（ふだんは1時間以内、火・水は15分以内が目安）で、まだ空いていれば予約します。\n"
               "※件名の #" + tok + " は消さないでください。\n"
               "※利用日の4日前を過ぎた枠は、予約後にキャンセルするとペナルティの対象です。\n\n"
               f"{BASE}/")
        for v in sn_new:
            state["notified"][v["key"]] = v["date"]
    # 利用日が全部過ぎた案内は捨てる
    t = today().strftime("%Y%m%d")
    state["offers"] = {k: v for k, v in state["offers"].items() if any(x.split("_")[3] >= t for x in v)}
    save_state(state)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        open(out, "a").write(f"book={'true' if queue else 'false'}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
