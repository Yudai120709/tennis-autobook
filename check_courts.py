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
RETRY_WAITS = (5, 15, 30)   # スポレクが遅い・つながらないときの再試行待機秒数
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
                time.sleep(wait)
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


def send_notification(new_slots, tag="【空き発見】"):
    """見つかった空き枠をメール通知する"""
    if not new_slots:
        return
    subj = f"{tag}テニスコート空き枠 {len(new_slots)}件"
    lines = [f"{subj}\n"]
    lines.append("以下の枠に空きが見つかりました：\n")
    for s in new_slots:
        lines.append(f"・{label(s)}")
    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("▼ 今すぐスポレクを開いて予約：")
    lines.append("https://kouen.sports.metro.tokyo.lg.jp/web/")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    body = "\n".join(lines)
    notify(subj, body)


def run_single_check(is_release, targets, first, last, wait, state):
    all_found = []

    if is_release:
        # 【22日一斉解放時】最優先の上位4公園を 4並列で最速同時スキャン！
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
        # ふだんの定期確認：1公園ずつ確実に確認
        for prio, tgt in enumerate(targets):
            try:
                f = scan_target(prio, tgt, first, last, wait)
                if f:
                    all_found.extend(f)
                time.sleep(wait)
            except Exception as e:
                print(f"  {tgt.get('memo', tgt.get('park'))} の確認でエラー: {e}", flush=True)

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
    send_hourly = False
    if last_report_str:
        try:
            last_report_dt = dt.datetime.fromisoformat(last_report_str)
            if (now_dt - last_report_dt).total_seconds() >= 3000:
                send_hourly = True
        except Exception:
            send_hourly = True
    else:
        send_hourly = True

    if new_slots:
        tag = "【22日一斉解放】" if is_release else "【空き発見】"
        send_notification(new_slots, tag=tag)
        for s in new_slots:
            state["notified"][s["key"]] = s["date"]
        state["last_hourly_report"] = now_dt.isoformat()
        save_state(state)
    elif not is_release:
        print("新規の空き枠はありませんでした。")
        if send_hourly:
            print("1時間に1回の定期報告メールを送信します。", flush=True)
            subj = "【定期報告】テニスコート監視中（現在空きなし）"
            lines = [
                subj + "\n",
                f"確認時刻: {now_dt.strftime('%Y/%m/%d %H:%M')}",
                f"対象公園: 全{len(targets)}箇所（木場・猿江・亀戸・有明・東綾瀬など）",
                f"期間: {first.strftime('%m/%d')} 〜 {last.strftime('%m/%d')}",
                "状況: 現在、条件に合致する空き枠はありません。\n",
                "※正常に5分おきで監視を継続しています。",
                "※新しい空き枠が出た瞬間に、即座に空き枠お知らせメールをお送りします。\n",
                "━━━━━━━━━━━━━━━━━━━━",
                "▼ スポレク（東京都立公園予約システム）：",
                "https://kouen.sports.metro.tokyo.lg.jp/web/",
                "━━━━━━━━━━━━━━━━━━━━"
            ]
            notify(subj, "\n".join(lines))
            state["last_hourly_report"] = now_dt.isoformat()
            save_state(state)


def main():
    is_release = "--release" in sys.argv
    if is_release:
        wait_until_release()

    # --loop MINUTES が指定された場合は、指定分数に達するまで5分おきに高精度ループ待機
    loop_minutes = 0
    for i, arg in enumerate(sys.argv):
        if arg == "--loop" and i + 1 < len(sys.argv):
            try:
                loop_minutes = int(sys.argv[i + 1])
            except ValueError:
                loop_minutes = 30

    state = load_state()
    state.setdefault("notified", {})

    t = today()
    first = t + dt.timedelta(days=CONFIG.get("days_from", 0))
    last = t + dt.timedelta(days=CONFIG.get("days_ahead", 60))
    wait = CONFIG.get("wait_seconds", 2)
    targets = CONFIG.get("targets", [])

    INTERVAL_SECONDS = 300  # きっちり5分（300秒）間隔
    start_time = time.time()
    cycle = 1

    while True:
        cycle_start = time.time()
        now_str = dt.datetime.now(JST).strftime("%H:%M:%S")
        print("\n========================================", flush=True)
        print(f"【第 {cycle} 周目】空き確認を開始します（時刻: {now_str}）", flush=True)
        print(f"対象期間: {first} 〜 {last}（対象公園数: {len(targets)}）", flush=True)
        print("========================================", flush=True)

        run_single_check(is_release, targets, first, last, wait, state)

        # releaseモード、またはループ指定なし（手動単発実行）の場合は1回で終了
        if is_release or loop_minutes <= 0:
            break

        elapsed = time.time() - cycle_start
        total_elapsed = time.time() - start_time

        # 次の周を回すと予定時間を超える場合は終了して次へバトンタッチ
        if total_elapsed + INTERVAL_SECONDS > loop_minutes * 60:
            print(f"\n予定の実行時間（{loop_minutes}分）が経過したため、今回のジョブを完了します。", flush=True)
            break

        sleep_sec = max(10, INTERVAL_SECONDS - elapsed)
        next_time = (dt.datetime.now(JST) + dt.timedelta(seconds=sleep_sec)).strftime("%H:%M:%S")
        print(f"\n第 {cycle} 周目 完了（所要時間: {int(elapsed)}秒）。", flush=True)
        print(f"▶ 次の確認は きっちり5分後（{next_time}）に開始します。{int(sleep_sec)}秒間待機中...", flush=True)
        time.sleep(sleep_sec)
        cycle += 1


if __name__ == "__main__":
    main()
