# -*- coding: utf-8 -*-
"""テニスコート空き枠の監視・メール通知（ログイン不要の公開空き状況検索を使用）。
条件に合う新しい空き枠が見つかったら、即座にメールで通知する。
22日の一斉解放時は --release オプションで0時ジャストまで待機し、上位4公園を最優先で同時並行スキャンする。
1時間に1回、空きがなくても監視状況を定期メールで送信する。
"""
import concurrent.futures
import datetime as dt, html, json, os, re, sys, time, urllib.parse
import requests
from common import BASE, WEEK, load_config, load_state, save_state, today, notify, label

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (personal tennis-court watcher)"}
RETRY_WAITS = (1, 1, 2, 2, 2, 2, 2, 2, 2, 2)   # スポレク激重時対策：1〜2秒間隔で最大10回しつこく食らいつく
JST = dt.timezone(dt.timedelta(hours=9))


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


def wanted(d, start, is_hol, tgt):
    """設定した曜日・時間帯に合致するか判定"""
    days = tgt.get("days", "all")
    weekend = d.weekday() in (5, 6) or (CONFIG.get("holidays_as_weekend", True) and is_hol)
    if days == "weekday" and weekend:
        return False
    if days == "holiday" and not weekend:
        return False
    if weekend:
        return CONFIG.get("weekend_all", True)
    return start // 100 >= CONFIG.get("weekday_start_from", 19)


def calc_target_range(today_date: dt.date) -> tuple[dt.date, dt.date]:
    """
    案A（都立スポレクの受付可能期間に完全連動）:
    - 1日〜21日: 今日+days_from 〜 当月末日まで（翌月分は全コマ受付期間外のため除外）
    - 22日〜末日: 今日+days_from 〜 翌月末日まで（22日0時に翌月分が一斉解放されるため網羅）
    """
    import calendar
    days_from = int(CONFIG.get("days_from", 6))
    first = today_date + dt.timedelta(days=days_from)
    if today_date.day < 22:
        _, last_day = calendar.monthrange(today_date.year, today_date.month)
        last = dt.date(today_date.year, today_date.month, last_day)
    else:
        year = today_date.year + (1 if today_date.month == 12 else 0)
        month = 1 if today_date.month == 12 else today_date.month + 1
        _, last_day = calendar.monthrange(year, month)
        last = dt.date(year, month, last_day)
    if first > last:
        first = last
    return first, last


def scan_target(prio, tgt, first, last, wait):
    """1公園分を順番に確認する"""
    bld, sport = str(tgt["park"]), tgt["sport"]
    name = tgt.get("memo", bld)
    s = requests.Session()
    found = []
    try:
        park, insts = open_search(s, sport, bld, first)
        for inst, iname in insts:
            day = first
            while day <= last:
                time.sleep(0.3)
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
                if cells and all(t.get("alt") == "受付期間外" for t in cells) and day > first + dt.timedelta(days=28):
                    break
                day += dt.timedelta(days=7)
    except Exception as e:
        print(f"  [{name}] の確認でエラー: {e}", flush=True)
    return found


def wait_until_release():
    """22日 00:00:01 まで秒単位で高精度待機する（21日深夜に事前起動された場合）"""
    print("【22日一斉解放モード】00:00:01 の受付開始まで秒単位で待機します...", flush=True)
    while True:
        now = dt.datetime.now(JST)
        if now.day == 22 and (now.hour > 0 or now.minute > 0 or now.second >= 1):
            print(f"★受付開始時刻になりました！（現在時刻: {now.strftime('%H:%M:%S.%f')[:12]}） 最速スキャンを開始します！", flush=True)
            break
        target = now.replace(hour=0, minute=0, second=1, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        wait_sec = (target - now).total_seconds()
        if wait_sec > 300:
            print(f"  待機中... 受付開始まで あと {int(wait_sec // 60)} 分（現在: {now.strftime('%H:%M:%S')}）", flush=True)
            time.sleep(30)
        elif wait_sec > 60:
            print(f"  待機中... 受付開始まで あと {int(wait_sec)} 秒（現在: {now.strftime('%H:%M:%S')}）", flush=True)
            time.sleep(10)
        elif wait_sec > 5:
            print(f"  直前カウントダウン: あと {wait_sec:.1f} 秒...", flush=True)
            time.sleep(1)
        elif wait_sec > 0:
            time.sleep(0.05)
        else:
            break


def get_tokyo_rain_pops():
    """気象庁の公式予報API（東京 130000）から直近数日間の降水確率を取得する"""
    url = "https://www.jma.go.jp/bosai/forecast/data/forecast/130000.json"
    pops_by_date = {}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for item in data:
            for s in item.get("timeSeries", []):
                time_defines = s.get("timeDefines", [])
                for a in s.get("areas", []):
                    if a.get("area", {}).get("code") == "130010":  # 東京地方
                        pops = a.get("pops", [])
                        if pops:
                            for td, pop in zip(time_defines, pops):
                                d_str = td[:10]
                                val = int(pop) if pop.isdigit() else 0
                                if d_str not in pops_by_date or val > pops_by_date[d_str]:
                                    pops_by_date[d_str] = val
    except Exception as e:
        print(f"[天気] 降水確率の取得をスキップ: {e}")
    return pops_by_date


def send_notification(new_slots, tag="【空き発見】"):
    """見つかった空き枠を、直前/来週以降に分類し、雨予報と最速iPhone直行URL付きで通知する"""
    if not new_slots:
        return

    today_d = today()
    pops = get_tokyo_rain_pops()

    urgent_slots = []
    normal_slots = []
    for s in new_slots:
        d = dt.date.fromisoformat(s["date"])
        diff = (d - today_d).days
        if diff <= 5:
            urgent_slots.append(s)
        else:
            normal_slots.append(s)

    first_slot = new_slots[0]
    p_name = first_slot.get("park", "テニス")
    d_raw = first_slot.get("date", "")
    d_short = d_raw[5:].replace("-", "/") if len(d_raw) >= 10 and d_raw[4] == '-' else d_raw
    wd_str = f"({first_slot.get('wd', '')})" if first_slot.get('wd') else ""
    t_str = first_slot.get("time", "")
    slot_summary = f"{p_name} {d_short}{wd_str} {t_str}".strip()

    num_slots = len(new_slots)
    if "22日" in tag:
        if num_slots >= 2:
            prefix = f"【優先・{num_slots}枠空き/22日解放】"
        else:
            prefix = "【22日解放】"
    elif num_slots >= 2:
        prefix = f"【優先・{num_slots}枠空き】"
    elif urgent_slots and not normal_slots:
        prefix = "【直前空き】"
    else:
        prefix = "【空き発見】"

    if num_slots == 1:
        subj = f"{prefix} {slot_summary}"
    else:
        extra = num_slots - 1
        subj = f"{prefix} {slot_summary} 他{extra}件"

    lines = [subj, "", "以下の枠に空きが見つかりました：", ""]

    if urgent_slots:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("⚡【直前・今週末のキャンセル枠（お早めに！）】")
        for s in urgent_slots:
            pop_info = ""
            if s["date"] in pops:
                p_val = pops[s["date"]]
                if p_val >= 40:
                    pop_info = f" ☔雨注意(降水確率{p_val}%)"
                else:
                    pop_info = f" ☀降水確率{p_val}%"
            lines.append(f"・{label(s)}{pop_info}")
            direct_url = f"https://kouen.sports.metro.tokyo.lg.jp/web/index.jsp#target={s.get('bld')}&date={s.get('date')}&sport={s.get('sport')}"
            lines.append(f"  👉【直行ワープ】: {direct_url}")
        lines.append("")

    if normal_slots:
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        lines.append("📅【来週以降の予約枠】")
        for s in normal_slots:
            pop_info = ""
            if s["date"] in pops:
                p_val = pops[s["date"]]
                if p_val >= 40:
                    pop_info = f" ☔降水確率{p_val}%"
                else:
                    pop_info = f" ☀降水確率{p_val}%"
            lines.append(f"・{label(s)}{pop_info}")
            direct_url = f"https://kouen.sports.metro.tokyo.lg.jp/web/index.jsp#target={s.get('bld')}&date={s.get('date')}&sport={s.get('sport')}"
            lines.append(f"  👉【直行ワープ】: {direct_url}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("▼ 【iPhone最速直行】")
    first_bld = first_slot.get('bld')
    first_date = first_slot.get('date')
    first_sport = first_slot.get('sport')
    first_url = f"https://kouen.sports.metro.tokyo.lg.jp/web/index.jsp#target={first_bld}&date={first_date}&sport={first_sport}"
    lines.append(f"👉 1件目へ直行: {first_url}")
    lines.append("※タップするとSafariが開き、自動ログイン＆該当日付へ一発ワープします")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    body = chr(10).join(lines)
    notify(subj, body)

def run_single_check(is_release, targets, first, last, wait, state, allow_hourly_report=True):
    all_found = []

    if is_release:
        if len(targets) == 1:
            # 【12台並列マトリックス時】1台1公園を最速スキャン！
            t = targets[0]
            print(f"▶ 【22日00:00:01 一斉解放】{t.get('memo', t.get('park'))} を最速スキャンします！", flush=True)
            res = scan_target(0, t, first, last, 0.2)
            if res:
                all_found.extend(res)
        else:
            # 【手動全公園実行時】最優先の上位4公園を 4並列で最速同時スキャン！
            top_targets = targets[:4]
            rest_targets = targets[4:]
            print("▶ 第1弾：最優先4公園を同時に並行確認します（最大4並列）...", flush=True)
            top_names = [t.get("memo", t.get("park")) for t in top_targets]
            print(f"   対象: {', '.join(top_names)}", flush=True)

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(scan_target, prio, tgt, first, last, 0.5)
                           for prio, tgt in enumerate(top_targets)]
                for f in concurrent.futures.as_completed(futures):
                    res = f.result()
                    if res:
                        all_found.extend(res)

            # 最優先4公園で見つかった枠を「即座に第1報としてメール送信」！
            uniq_first = {}
            for slot in all_found:
                if slot["key"] not in uniq_first:
                    uniq_first[slot["key"]] = slot
            first_new_slots = [s for s in uniq_first.values() if s["key"] not in state["notified"]]
            if first_new_slots:
                first_new_slots = sorted(first_new_slots, key=lambda s: (s["date"], s["start"], s["prio"]))
                print(f"★最優先4公園で {len(first_new_slots)} 件の空き枠を検知！即座に速報メールを送信します！", flush=True)
                send_notification(first_new_slots, tag="【22日速報・最優先4公園】")
                for s in first_new_slots:
                    state["notified"][s["key"]] = s["date"]
                save_state(state)

            # 続いて残りの公園を2並列で確認
            if rest_targets:
                print("▶ 第2弾：続いて残りの公園を確認します...", flush=True)
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(scan_target, prio + 4, tgt, first, last, 1.0)
                               for prio, tgt in enumerate(rest_targets)]
                    for f in concurrent.futures.as_completed(futures):
                        res = f.result()
                        if res:
                            all_found.extend(res)
    else:
        # ふだんの定期確認：全12公園を 4並列で高速スキャン（所要時間: 約30〜40秒）
        print(f"▶ 全{len(targets)}公園を4並列で高速スキャンします...", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(scan_target, prio, tgt, first, last, 0.3)
                       for prio, tgt in enumerate(targets)]
            for f in concurrent.futures.as_completed(futures):
                try:
                    res = f.result()
                    if res:
                        all_found.extend(res)
                except Exception as e:
                    print(f"  スキャン中にエラー: {e}", flush=True)

    # 全体の新規枠を整理
    uniq = {}
    for slot in all_found:
        if slot["key"] not in uniq:
            uniq[slot["key"]] = slot
    sorted_slots = sorted(uniq.values(), key=lambda s: (s["date"], s["start"], s["prio"]))
    new_slots = [s for s in sorted_slots if s["key"] not in state["notified"]]

    print(f"確認完了: 検出 {len(sorted_slots)} 件（全体での新規通知対象: {len(new_slots)} 件）", flush=True)

    # 1時間に1回の定期生存報告（空きがなくても送る）
    now_dt = dt.datetime.now(JST)
    last_report_str = state.get("last_hourly_report")
    last_reported_hour = state.get("last_reported_hour")
    send_hourly = False
    if allow_hourly_report and (last_reported_hour != now_dt.hour):
        if 0 <= now_dt.minute <= 5:
            send_hourly = True
        elif not last_report_str:
            send_hourly = True
        else:
            try:
                last_report_dt = dt.datetime.fromisoformat(last_report_str)
                if (now_dt - last_report_dt).total_seconds() >= 3600:
                    send_hourly = True
            except Exception:
                send_hourly = True
    else:
        send_hourly = False
    if new_slots:
        tag = "【22日一斉解放】" if is_release else "【空き発見】"
        send_notification(new_slots, tag=tag)
        for s in new_slots:
            state["notified"][s["key"]] = s["date"]
        state["last_hourly_report"] = now_dt.isoformat()
        state["last_reported_hour"] = now_dt.hour
        save_state(state)
    elif not is_release:
        print("新規の空き枠はありませんでした。")
        if send_hourly:
            print("1時間に1回の定期報告メールを送信します。", flush=True)
            subj = "【定期報告】テニスコート監視中（現在空きなし）"
            lines = [
                subj + "\n",
                f"確認時刻: {now_dt.strftime('%Y/%m/%d %H:%M')}",
                f"対象公園: 全{len(all_targets)}箇所（木場・日比谷・芝・猿江・亀戸・大島・篠崎・有明・東綾瀬・東白鬚・汐入）",
                f"期間: {first.strftime('%m/%d')} 〜 {last.strftime('%m/%d')}",
                "状況: 現在、条件に合致する空き枠はありません。\n",
                "※全12公園を12台のサーバーが超高速（確認後2秒待機・マンマーク並列）で常時監視しています。",
                "※新しい空き枠が出た瞬間に、即座にDiscordおよびメールでお知らせします。\n",
                "━━━━━━━━━━━━━━━━━━━━",
                "▼ スポレク（東京都立公園予約システム）：",
                "https://kouen.sports.metro.tokyo.lg.jp/web/",
                "━━━━━━━━━━━━━━━━━━━━"
            ]
            # 定期報告メールを送信
            notify(subj, chr(10).join(lines))
            state["last_hourly_report"] = now_dt.isoformat()
            state["last_reported_hour"] = now_dt.hour
            save_state(state)


def main():
    is_release = "--release" in sys.argv
    if is_release:
        wait_until_release()

    # 引数パース: --loop, --target-index, --interval
    loop_minutes = 0
    target_index = None
    interval_seconds = 2  # 既定: 2秒待機スキャン

    for i, arg in enumerate(sys.argv):
        if arg == "--loop" and i + 1 < len(sys.argv):
            try:
                loop_minutes = int(sys.argv[i + 1])
            except ValueError:
                loop_minutes = 30
        elif arg == "--target-index" and i + 1 < len(sys.argv):
            try:
                target_index = int(sys.argv[i + 1])
            except ValueError:
                target_index = None
        elif arg == "--interval" and i + 1 < len(sys.argv):
            try:
                interval_seconds = int(sys.argv[i + 1])
            except ValueError:
                interval_seconds = 10

    state = load_state()
    state.setdefault("notified", {})

    t = today()
    first, last = calc_target_range(t)
    wait = CONFIG.get("wait_seconds", 2)
    all_targets = CONFIG.get("targets", [])

    if target_index is not None and 0 <= target_index < len(all_targets):
        targets = [all_targets[target_index]]
        assigned_name = targets[0].get("memo", f"Target-{target_index}")
        print(f"【担当公園マンマーク】No.{target_index}: {assigned_name} を超高速（確認後{interval_seconds}秒待機）で監視します", flush=True)
        # 代表（target 0）だけ定期報告メールを送信。それ以外は空き枠発見時のみ送信
        allow_hourly = (target_index == 0)
    else:
        targets = all_targets
        assigned_name = f"全{len(targets)}公園"
        allow_hourly = True

    INTERVAL_SECONDS = interval_seconds
    start_time = time.time()
    cycle = 1

    while True:
        cycle_start = time.time()
        now_str = dt.datetime.now(JST).strftime("%H:%M:%S")
        print("\n========================================", flush=True)
        print(f"【第 {cycle} 周目】空き確認を開始します（時刻: {now_str}）", flush=True)
        print(f"対象期間: {first} 〜 {last}（対象公園数: {len(targets)}）", flush=True)
        print("========================================", flush=True)

        run_single_check(is_release, targets, first, last, wait, state, allow_hourly_report=allow_hourly)

        # releaseモード、またはループ指定なし（手動単発実行）の場合は1回で終了
        if loop_minutes <= 0:
            break

        elapsed = time.time() - cycle_start
        total_elapsed = time.time() - start_time

        # 次の周を回すと予定時間を超えるか、正時の交代時刻に到達した場合は次便へバトンタッチ
        now_dt = dt.datetime.now(JST)
        is_handover_time = (total_elapsed >= 150 * 60) and (now_dt.hour % 3 == 0) and (0 <= now_dt.minute <= 8)
        is_timeout = (total_elapsed + INTERVAL_SECONDS > loop_minutes * 60)

        if is_handover_time or is_timeout:
            reason = "正時の交代時刻に到達したため、待機中の次便へバトンタッチします" if is_handover_time else f"予定時間（{loop_minutes}分）が経過したため完了します"
            print(f"\n【バトンタッチ完了】{reason}（稼働時間: {int(total_elapsed // 60)}分）。", flush=True)
            break

        sleep_sec = max(1, interval_seconds)
        next_time = (dt.datetime.now(JST) + dt.timedelta(seconds=sleep_sec)).strftime("%H:%M:%S")
        print(f"\n第 {cycle} 周目 完了（所要時間: {int(elapsed)}秒）。", flush=True)
        print(f"▶ 次の確認は {int(sleep_sec)}秒後（{next_time}）に開始します。待機中...", flush=True)
        time.sleep(sleep_sec)
        cycle += 1


if __name__ == "__main__":
    main()
