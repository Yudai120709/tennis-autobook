# -*- coding: utf-8 -*-
"""空き枠の監視（ログイン不要の空き状況検索だけを使う）。
条件に合う新しい空きを通知し、自動予約が有効なら予約候補を book_queue.json に書き出す。"""
import datetime as dt, html, json, os, re, sys, time, urllib.parse
import requests
from common import BASE, WEEK, QUEUE_PATH, load_config, load_state, save_state, today, notify, label

UA = {"User-Agent": "Mozilla/5.0 (personal tennis-court watcher)"}
CONFIG = load_config()


def dec(r):
    return r.content.decode("cp932", "replace")


def post(s, path, data):
    body = urllib.parse.urlencode(data, encoding="cp932")
    r = s.post(f"{BASE}/{path}", data=body, timeout=60,
               headers={**UA, "Content-Type": "application/x-www-form-urlencoded", "Referer": f"{BASE}/"})
    r.raise_for_status()
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
    f = form_fields(dec(s.get(f"{BASE}/", headers=UA, timeout=60)))
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


def wanted(day, start, is_holiday):
    """土日（設定により祝日も）は全枠、それ以外は weekday_start_from 時以降に始まる枠"""
    weekend = day.weekday() >= 5 or (is_holiday and CONFIG.get("holidays_as_weekend", False))
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
                            and wanted(d, t["startTime"], t["useDay"] in hol):
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


def main():
    state = load_state()
    found = find_vacancies()
    new = [v for v in found if v["key"] not in state["notified"]]
    print(f"条件に合う空き {len(found)} 件（うち新規 {len(new)} 件）")
    queue = booking_candidates(new, state)
    json.dump(queue, open(QUEUE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if new:
        lines = [label(v) for v in sorted(new, key=lambda v: (v["date"], v["start"]))]
        msg = "\n".join(lines[:30]) + (f"\n…ほか {len(lines) - 30} 件" if len(lines) > 30 else "")
        if queue:
            msg += "\n\n→ 自動予約を試みます（結果は別に通知します）"
        notify(f"【テニス空き】{len(new)}件", msg + f"\n\n{BASE}/")
    for v in new:
        state["notified"][v["key"]] = v["date"]
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
