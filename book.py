# -*- coding: utf-8 -*-
"""自動予約（Playwright で実際の画面を操作する）。

  python book.py            … 監視で見つかった空き（book_queue.json）を予約する（毎時の実行）
  python book.py --release  … 22日0:00 の翌月分受付開始に合わせて待機し、開始と同時に予約する
  python book.py --release --now … 待たずにすぐ翌月分の予約処理を行う（テスト用）

config.json の auto_book.mode が "rehearsal" の間は、最後の「確定」ボタンの直前で必ず止まる。
各画面のスクリーンショットを shots/ に保存する（GitHub の Actions 画面の Artifacts から見られる）。
"""
import datetime as dt, json, os, re, sys, time
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
from common import BASE, JST, WEEK, QUEUE_PATH, load_config, load_state, save_state, today, notify, label

CONFIG = load_config()
AB = CONFIG.get("auto_book", {})
REHEARSAL = AB.get("mode", "rehearsal") != "book"
SHOTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")
os.makedirs(SHOTS, exist_ok=True)

FINAL_WORDS = ("確定", "申込", "申し込", "予約する", "完了", "登録")
NEXT_WORDS = ("次へ", "確認", "進む")
AVOID_WORDS = ("戻る", "もどる", "中止", "キャンセル", "取消", "ホーム", "閉じる", "ログアウト", "削除")
ONLINE_PAY = ("オンライン", "クレジット", "事前決済", "カード")
ONSITE_PAY = ("現地", "窓口", "当日", "後払", "施設で", "支払わない")


class Stop(Exception):
    """これ以上自動で進めてはいけない状態"""


class Booker:
    def __init__(self, page):
        self.page = page
        self.n = 0
        self.dialogs = []
        page.on("dialog", self.on_dialog)

    # ブラウザの alert / confirm への対応
    def on_dialog(self, d):
        msg = d.message
        self.dialogs.append(msg)
        print(f"  ダイアログ: {msg}")
        if d.type == "confirm":
            # ペナルティ等の警告には同意しない。予約の最終確認だけ、本番モードで同意する
            ok = (not REHEARSAL) and any(w in msg for w in ("予約", "申込", "申し込", "確定")) \
                and not any(w in msg for w in ("ペナルティ", "キャンセル", "取消", "削除", "中止"))
            d.accept() if ok else d.dismiss()
        else:
            d.accept()

    def shot(self, name):
        self.n += 1
        p = os.path.join(SHOTS, f"{dt.datetime.now(JST):%m%d_%H%M%S}_{self.n:02d}_{name}.png")
        try:
            self.page.screenshot(path=p, full_page=True)
        except Exception:
            pass

    def text(self):
        return self.page.inner_text("body")

    def login(self):
        pg = self.page
        pg.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=90000)
        if pg.evaluate("typeof gRecaptchaActive !== 'undefined' && gRecaptchaActive === true"):
            raise Stop("ログイン画面で画像認証（reCAPTCHA）が有効になっています。自動ログインはしません")
        pg.click("#btn-login")
        pg.wait_for_selector("#userId", timeout=60000)
        if pg.evaluate("typeof gRecaptchaActive !== 'undefined' && gRecaptchaActive === true"):
            raise Stop("ログイン画面で画像認証（reCAPTCHA）が有効になっています。自動ログインはしません")
        pg.fill("#userId", os.environ["SPOREC_USER_ID"])
        pg.fill("#password", os.environ["SPOREC_PASSWORD"])
        with pg.expect_navigation(timeout=90000):
            pg.click("#btn-go")
        body = self.text()
        if pg.query_selector("#userId"):
            self.shot("login_failed")
            raise Stop("ログインできませんでした（利用者番号・パスワード・有効期限を確認してください）")
        if any(w in body for w in ("認証コード", "ワンタイム", "SMS", "確認コード")):
            self.shot("mfa")
            raise Stop("ログイン時に追加の認証（SMS等）を求められたため、自動では進めません")
        print("  ログインしました")

    def open_week(self, v):
        """ログイン状態で、指定の公園・日付の空き状況画面を開く"""
        pg = self.page
        cls, pps = v["sport"].split("_")
        d = dt.datetime.strptime(str(v["useDay"]), "%Y%m%d").strftime("%Y-%m-%d")
        fields = dict(daystarthome=d, daystart=d, selectPpsClPpscd=v["sport"], selectPpsClsCd=cls,
                      selectPpsCd=pps, selectAreaBcd=v["bld"], selectBldCd=v["bld"],
                      dayofweekClearFlg="1", timezoneClearFlg="1", selectIcd="0")
        if not pg.query_selector("form[name=form1]") or not pg.evaluate("typeof doAction === 'function'"):
            pg.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=90000)
        with pg.expect_navigation(timeout=90000):
            pg.evaluate("""(f) => {
                const form = document.form1;
                for (const [k, v] of Object.entries(f)) {
                    let el = form.elements[k];
                    if (!el || el.length) { el = document.createElement('input'); el.type = 'hidden'; el.name = k; form.appendChild(el); }
                    el.value = v;
                }
                doAction(form, gRsvWOpeInstSrchVacantAction);
            }""", fields)
        if "空き状況" not in pg.title():
            self.shot("search_failed")
            raise RuntimeError("空き状況画面を開けませんでした")
        # 施設（コート種別）が違えば切り替える
        cur = pg.evaluate("document.form1.selectInstCd ? document.form1.selectInstCd.value : ''")
        if cur and cur != v["inst"]:
            opt = pg.evaluate(f"""Array.from(document.querySelectorAll('#facility-select option'))
                                  .map(o => o.value).find(x => x.split('_')[0] === '{v["inst"]}')""")
            if opt:
                with pg.expect_navigation(timeout=90000):
                    pg.select_option("#facility-select", opt)

    def cell_available(self, cid):
        return self.page.evaluate(f"""(() => {{
            const td = document.getElementById('{cid}');
            if (!td) return false;
            const el = td.querySelector('[onclick*="setReserv"]') || (String(td.getAttribute('onclick')||'').includes('setReserv') ? td : null);
            return !!el;
        }})()""")

    def wait_week(self, cid):
        try:
            self.page.wait_for_function(f"document.getElementById('{cid}') !== null", timeout=60000)
        except PwTimeout:
            return False
        return True

    def book_cell(self, v):
        """空き状況画面上の1枠を予約する。成功で True、空いていなければ False、途中停止は Stop"""
        pg = self.page
        cid = f'{v["useDay"]}_{v["tzoneNo"]}'
        if not self.wait_week(cid) or not self.cell_available(cid):
            return False
        pg.evaluate(f"""(() => {{
            const td = document.getElementById('{cid}');
            (td.querySelector('[onclick*="setReserv"]') || td).click();
        }})()""")
        try:
            pg.wait_for_function("document.form1.selectSize && document.form1.selectSize.value != '0'", timeout=30000)
        except PwTimeout:
            self.shot("select_failed")
            return False
        self.shot("selected")
        try:
            with pg.expect_navigation(timeout=30000):
                pg.evaluate("checkSelect(document.form1, gRsvWOpeReservedApplyAction)")
        except PwTimeout:
            self.shot("apply_not_opened")
            raise RuntimeError("申込画面に進めませんでした（警告: " + (self.dialogs[-1] if self.dialogs else "なし") + "）")
        return self.walk_apply(v)

    def walk_apply(self, v):
        """申込画面から先を進める。画面の内容は文字で判断し、分からなければ止まる"""
        pg = self.page
        for step in range(8):
            pg.wait_for_load_state("domcontentloaded")
            self.shot(f"apply{step}")
            body = self.text()
            if any(w in body for w in ("予約が完了", "予約を受け付けました", "受付が完了", "申込みが完了", "予約番号", "受付番号")):
                return True
            if "エラー" in pg.title() or "エラー画面" in body[:200]:
                raise RuntimeError("エラー画面になりました: " + re.sub(r"\s+", " ", body)[:200])
            self.choose_onsite_payment()
            self.fill_basics()
            btns = self.buttons()
            nxt = [b for b in btns if any(w in b[1] for w in NEXT_WORDS) and not any(w in b[1] for w in FINAL_WORDS)]
            fin = [b for b in btns if any(w in b[1] for w in FINAL_WORDS)]
            if nxt:
                target = nxt[0]
            elif fin:
                if REHEARSAL:
                    raise Stop(f"リハーサルのため「{fin[0][1]}」ボタンの直前で止めました（予約はされていません）")
                target = fin[0]
            else:
                raise Stop("次に押すボタンが判断できない画面になったため止めました")
            print(f"  「{target[1]}」を押します")
            before = pg.url + str(len(body))
            target[0].click()
            try:
                pg.wait_for_load_state("domcontentloaded", timeout=90000)
                pg.wait_for_timeout(1500)
            except PwTimeout:
                pass
            if pg.url + str(len(self.text())) == before and self.dialogs and "入力" in self.dialogs[-1]:
                raise Stop("入力不足の警告が出ました: " + self.dialogs[-1])
        raise Stop("申込画面が想定より長く続いたため止めました")

    def buttons(self):
        out = []
        for el in self.page.query_selector_all("button, input[type=button], input[type=submit], a.btn"):
            try:
                if not el.is_visible():
                    continue
                t = (el.inner_text() or el.get_attribute("value") or "").strip()
            except Exception:
                continue
            if t and not any(w in t for w in AVOID_WORDS):
                out.append((el, t))
        return out

    def choose_onsite_payment(self):
        """支払方法の選択肢があれば、オンライン決済ではない方を選ぶ"""
        radios = self.page.evaluate("""Array.from(document.querySelectorAll('input[type=radio]')).map((r, i) => {
            const lab = (r.labels && r.labels[0] ? r.labels[0].innerText : (r.parentElement ? r.parentElement.innerText : '')) || '';
            return {i, name: r.name, label: lab.trim(), checked: r.checked};
        })""")
        pay = [r for r in radios if any(w in r["label"] for w in ONLINE_PAY + ONSITE_PAY)]
        if not pay:
            return
        onsite = [r for r in pay if any(w in r["label"] for w in ONSITE_PAY) and not any(w in r["label"] for w in ONLINE_PAY)]
        if not onsite:
            raise Stop("支払方法にオンライン決済以外の選択肢が見つからないため止めました")
        self.page.evaluate("(i) => { const r = document.querySelectorAll('input[type=radio]')[i]; r.checked = true; r.click(); }", onsite[0]["i"])
        print(f"  支払方法：「{onsite[0]['label']}」を選択")

    def fill_basics(self):
        """利用人数が空欄なら設定値を入れ、同意のチェックを付ける"""
        players = str(AB.get("players", 4))
        self.page.evaluate("""(n) => {
            for (const el of document.querySelectorAll('input[type=text], input[type=tel], input[type=number]')) {
                const lab = ((el.labels && el.labels[0] ? el.labels[0].innerText : '') + ' ' + (el.name||'') + ' ' + (el.id||'')
                             + ' ' + (el.closest('tr') ? el.closest('tr').innerText : '')).toLowerCase();
                if ((lab.includes('人数') || lab.includes('ninzu') || lab.includes('ninzuu')) && !el.value) el.value = n;
            }
            for (const el of document.querySelectorAll('input[type=checkbox]')) {
                const lab = (el.labels && el.labels[0] ? el.labels[0].innerText : (el.parentElement ? el.parentElement.innerText : '')) || '';
                if (lab.includes('同意') && !el.checked) el.click();
            }
        }""", players)


def deadline(date_iso):
    d = dt.date.fromisoformat(date_iso)
    return (d - dt.timedelta(days=4)).isoformat()


def group_by_week(cands):
    """同じ公園・コートの7日分（1画面に出る範囲）ごとにまとめ、優先度の高い候補を含む週から並べる"""
    base = {}
    for v in cands:
        k = (v["bld"], v["inst"])
        base[k] = min(base.get(k, v["date"]), v["date"])
    groups, order = {}, []
    for v in cands:   # cands は優先順
        k = (v["bld"], v["inst"])
        chunk = (dt.date.fromisoformat(v["date"]) - dt.date.fromisoformat(base[k])).days // 7
        g = k + (chunk,)
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(v)
    out = []
    for g in order:
        lst = groups[g]
        start = dt.date.fromisoformat(base[g[:2]]) + dt.timedelta(days=7 * g[2])
        opener = dict(lst[0]); opener["useDay"] = int(start.strftime("%Y%m%d"))   # 画面を開く日付の指定だけに使う
        out.append((opener, lst))
    return out


def run(candidates, max_new, title):
    """候補を優先順に予約する。結果を通知して state.json に記録する"""
    state = load_state()
    booked_days = {b["date"] for b in state["booked"]}
    booked_times = {(b["date"], b.get("start")) for b in state["booked"]}
    max_new = max_new if max_new else float("inf")   # None = 上限なし
    results, done, stop_reason = [], 0, None
    if not candidates:
        print("予約候補はありません")
        return
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(locale="ja-JP", viewport={"width": 1280, "height": 1600})
        page.set_default_timeout(60000)
        b = Booker(page)
        try:
            b.login()
            for opener, group in group_by_week(candidates):
                need_open = True
                for v in group:
                    if done >= max_new:
                        break
                    if (v["date"], v["start"]) in booked_times:
                        continue   # 同じ日時の重複予約はシステム上できない
                    if AB.get("one_per_day", False) and v["date"] in booked_days:
                        continue
                    try:
                        if need_open:
                            b.open_week(opener)   # その週の7日分が1画面に出る
                            need_open = False
                        ok = b.book_cell(v)
                    except Stop:
                        raise
                    except Exception as e:
                        state["attempts"][v["key"]] = state["attempts"].get(v["key"], 0) + 1
                        results.append(f"× {label(v)}（失敗: {str(e)[:120]}）")
                        need_open = True
                        continue
                    if ok:
                        done += 1
                        need_open = True
                        booked_days.add(v["date"])
                        booked_times.add((v["date"], v["start"]))
                        state["booked"].append({k: v[k] for k in ("key", "date", "wd", "time", "park", "court", "start")})
                        save_state(state)   # 1件ごとに記録する
                        results.append(f"○ {label(v)}　※不要ならキャンセル期限 {deadline(v['date'])} までに取消")
                if done >= max_new:
                    break
        except Stop as e:
            stop_reason = str(e)
        except Exception as e:
            stop_reason = f"想定外のエラー: {str(e)[:200]}"
            b.shot("error")
        finally:
            browser.close()
    save_state(state)
    lines = results[:]
    if stop_reason:
        lines.append(f"停止: {stop_reason}")
    if REHEARSAL:
        lines.append("（リハーサルモード：予約は確定していません。Actions の Artifacts で画面を確認してください）")
    subject = f"【{title}】" + (f"{done}件予約しました" if done else ("リハーサル結果" if REHEARSAL else "予約できませんでした"))
    notify(subject, "\n".join(lines) or "空いている候補はありませんでした")


# ---------- 22日0:00 の受付開始 ----------
def release_candidates():
    """翌月の、条件に合う全枠（公園の時間帯は事前に空き状況から取る）を優先順に並べる"""
    import requests, check_courts as cc
    t = today()
    first = (t.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    last = (first + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    lo = t + dt.timedelta(days=CONFIG.get("days_from", 6))
    s = requests.Session()
    cands = []
    for prio, tgt in enumerate(CONFIG["targets"]):
        park, insts = cc.open_search(s, tgt["sport"], str(tgt["park"]), max(first, lo))
        for inst, iname in insts:
            day = max(first, lo)
            while day <= last:
                j = cc.week(s, str(tgt["park"]), inst, day)
                hol = {w["useDay"] for w in j.get("weekDay", []) if w.get("holidayFlg") == 4}
                for r in j.get("result", []):
                    for c in r.get("timeResult", []):
                        d = dt.datetime.strptime(str(c["useDay"]), "%Y%m%d").date()
                        if first <= d <= last and d >= lo and c.get("alt") != "保守日" \
                                and cc.wanted(d, c["startTime"], c["useDay"] in hol):
                            cands.append({"key": f'{tgt["sport"]}_{tgt["park"]}_{inst}_{c["useDay"]}_{c["startTime"]}',
                                          "sport": tgt["sport"], "bld": str(tgt["park"]), "inst": inst,
                                          "useDay": c["useDay"], "tzoneNo": r["tzoneNo"], "start": c["startTime"],
                                          "end": c["endTime"], "park": park, "court": iname,
                                          "date": d.isoformat(), "wd": WEEK[d.weekday()], "prio": prio,
                                          "holiday": d.weekday() >= 5 or (c["useDay"] in hol and CONFIG.get("holidays_as_weekend", False)),
                                          "time": f'{c["startTime"]//100}:{c["startTime"]%100:02d}-'
                                                  f'{c["endTime"]//100}:{c["endTime"]%100:02d}'})
                day += dt.timedelta(days=7)
                time.sleep(1)
    rel = CONFIG.get("release", {})
    hours = rel.get("preferred_start_hours", [])
    hr = lambda v: hours.index(v["start"] // 100) if v["start"] // 100 in hours else len(hours)
    weekend_first = rel.get("weekend_first", True)
    cands.sort(key=lambda v: (0 if (weekend_first and v["holiday"]) else 1,
                              v["date"], hr(v), v["prio"]))
    return cands


def release(now_flag):
    rel = CONFIG.get("release", {})
    if not rel.get("enabled", True):
        print("22日の一斉予約は無効です")
        return
    state = load_state()
    month = (today().replace(day=1) + dt.timedelta(days=32)).strftime("%Y-%m")
    if state.get("release_done") == month and not now_flag:
        print(f"{month} 分の一斉予約は実行済みです")
        return
    late = False
    if not now_flag:
        n = dt.datetime.now(JST)
        target = (n + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if n.day == 22 and n.hour < 2:
            late = True   # 起動が0時を過ぎてしまった。すぐに予約処理を行う
        elif n.day != 21 or (target - n).total_seconds() > 3 * 3600:
            print("21日の夜ではないため何もしません")
            return
    cands = release_candidates()
    print(f"翌月分の候補 {len(cands)} 枠")
    if not now_flag and not late:
        # 受付開始の数分前まで待ってから、開始時刻ちょうどまで待つ（ログインは run の中で行う）
        start = target + dt.timedelta(seconds=rel.get("start_delay_seconds", 1))
        login_at = start - dt.timedelta(minutes=rel.get("login_minutes_before", 5))
        while dt.datetime.now(JST) < login_at:
            time.sleep(5)
        global _WAIT_UNTIL, _PREOPEN
        _WAIT_UNTIL = start
        groups = group_by_week(cands)
        _PREOPEN = groups[0][0] if groups else None
    from check_courts import lim
    run(cands, lim(rel.get("max_bookings")), "22日一斉予約")
    state = load_state()
    state["release_done"] = month
    save_state(state)


_WAIT_UNTIL = None
_PREOPEN = None
_orig_login = Booker.login


def _login_then_wait(self):
    """22日：0時の数分前にログインし、最初に見る週の空き状況画面を開いたまま0時を待つ。
    待っている間も2分おきに画面を開き直して、ログイン状態が切れないようにする"""
    _orig_login(self)
    if not _WAIT_UNTIL:
        return
    print(f"  受付開始 {_WAIT_UNTIL:%H:%M:%S} まで、空き状況画面を開いて待機します")
    last = 0
    while True:
        remain = (_WAIT_UNTIL - dt.datetime.now(JST)).total_seconds()
        if remain <= 0:
            break
        if _PREOPEN and remain > 40 and time.time() - last > 120:
            try:
                self.open_week(_PREOPEN)
                last = time.time()
            except Exception as e:
                print(f"  待機中の画面更新に失敗: {e}")
                try:
                    _orig_login(self)
                except Exception:
                    pass
        time.sleep(0.2 if remain < 5 else 1)
    print("  受付開始。空き状況を確認します")


Booker.login = _login_then_wait


def hourly():
    if not AB.get("enabled"):
        return
    from check_courts import lim
    queue = json.load(open(QUEUE_PATH, encoding="utf-8")) if os.path.exists(QUEUE_PATH) else []
    state = load_state()
    cap, per = lim(AB.get("max_active")), lim(AB.get("max_per_run"))
    room = None if cap is None else cap - len(state["booked"])
    if room is not None and room <= 0:
        print("自動予約の上限に達しています")
        return
    n = [x for x in (room, per) if x is not None]
    run(queue, min(n) if n else None, "テニス自動予約")


if __name__ == "__main__":
    for k in ("SPOREC_USER_ID", "SPOREC_PASSWORD"):
        if not os.environ.get(k):
            print(f"{k} が設定されていません（GitHub の Secrets に登録してください）", file=sys.stderr)
            sys.exit(1)
    if "--release" in sys.argv:
        release("--now" in sys.argv)
    else:
        hourly()
