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
FINAL_EXACT = ("予約",)  # 予約内容確認画面の確定ボタンは「予約」だけの名前
NEXT_WORDS = ("次へ", "確認", "進む")
AVOID_WORDS = ("戻る", "もどる", "中止", "キャンセル", "取消", "ホーム", "閉じる", "ログアウト", "削除")
ONLINE_PAY = ("オンライン", "クレジット", "事前決済", "カード")
ONSITE_PAY = ("現地", "窓口", "当日", "後払", "施設で", "支払わない")


BOOK_VERSION = "2026-11-02 v10（テスト予約つき）"
LOGIN_WAIT_MS = 300000   # ログインの各段階で、画面が出るまで最大5分待つ
print("book.py 版:", BOOK_VERSION)


class Stop(Exception):
    """これ以上自動で進めてはいけない状態"""


class Booker:
    def __init__(self, page):
        self.page = page
        self.n = 0
        self.dialogs = []
        self.last_select_error = ""
        page.on("dialog", self.on_dialog)

    # ブラウザの alert / confirm への対応
    def on_dialog(self, d):
        msg = d.message
        self.dialogs.append(msg)
        print(f"  ダイアログ: {msg}")
        if d.type == "confirm" and "選択情報をリセット" in msg:
            d.accept()   # 自分の仮押さえ（選択中の枠）を解除する確認だけは同意する
        elif d.type == "confirm":
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

    # スポレクの入口のサーバーが、奥のサーバーから返事をもらえなかったときの画面の文
    SERVER_ERROR_WORDS = ("Proxy Error", "Bad Gateway", "Service Unavailable",
                          "Gateway Timeout", "Internal Server Error")
    # やり直すのは、スポレクのエラー画面が出たとき・5分待っても進まないときだけ（最大3回）
    LOGIN_RETRY_WAITS = (30, 60, 120)

    def _server_error(self, resp=None):
        """スポレク側のエラー画面なら、その名前を返す（ふつうの画面なら空文字）"""
        if resp is not None and resp.status >= 500:
            return f"サーバー側のエラー {resp.status}"
        try:
            body = self.text()[:3000]
        except Exception:
            return ""
        for w in self.SERVER_ERROR_WORDS:
            if w in body:
                return w
        return ""

    def login(self):
        """スポレクはログインで長く考えることがあるので、すぐにはあきらめない"""
        waits = self.LOGIN_RETRY_WAITS
        for i in range(len(waits) + 1):
            try:
                self._login_once()
                return
            except Stop:
                raise          # ID・パスワードの間違い、画像認証などは、やり直しても同じ
            except Exception as e:
                if i == len(waits):
                    raise Stop(f"スポレクがログインに応えないため、{len(waits)}回やり直しても"
                               f"ログインできませんでした（{str(e)[:100]}）")
                print(f"  ログインに失敗（{str(e)[:100]}）。{waits[i]}秒後にやり直します"
                      f"（{i + 1}/{len(waits)}）", flush=True)
                self.shot("login_retry")
                time.sleep(waits[i])

    def _login_once(self):
        pg = self.page
        resp = pg.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=LOGIN_WAIT_MS)
        err = self._server_error(resp)
        if err:
            raise RuntimeError(f"ホーム画面がエラー: {err}")
        if pg.evaluate("typeof gRecaptchaActive !== 'undefined' && gRecaptchaActive === true"):
            raise Stop("ログイン画面で画像認証（reCAPTCHA）が有効になっています。自動ログインはしません")
        btn = pg.query_selector("#btn-login")
        if not (btn and btn.is_visible()):
            # 前回の送信が、遅れて実は通っていた場合など。もうログインしている
            print("  ログイン済みでした")
            self.shot("after_login")
            return
        pg.click("#btn-login", timeout=LOGIN_WAIT_MS)
        pg.wait_for_selector("#userId", timeout=LOGIN_WAIT_MS)
        if pg.evaluate("typeof gRecaptchaActive !== 'undefined' && gRecaptchaActive === true"):
            raise Stop("ログイン画面で画像認証（reCAPTCHA）が有効になっています。自動ログインはしません")
        pg.fill("#userId", os.environ["SPOREC_USER_ID"])
        pg.fill("#password", os.environ["SPOREC_PASSWORD"])
        with pg.expect_navigation(timeout=LOGIN_WAIT_MS) as nav:   # ログインの送信は長く考えることがある
            pg.click("#btn-go")
        try:
            resp = nav.value
        except Exception:
            resp = None
        err = self._server_error(resp)
        if err:
            raise RuntimeError(f"ログインの送信後にエラー: {err}")
        body = self.text()
        if pg.query_selector("#userId"):
            self.shot("login_failed")
            raise Stop("ログインできませんでした（利用者番号・パスワード・有効期限を確認してください）")
        if any(w in body for w in ("認証コード", "ワンタイム", "SMS", "確認コード")):
            self.shot("mfa")
            raise Stop("ログイン時に追加の認証（SMS等）を求められたため、自動では進めません")
        print("  ログインしました")
        self.shot("after_login")

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

    def wait_week(self, cid, sec=60):
        try:
            self.page.wait_for_function(f"document.getElementById('{cid}') !== null", timeout=sec * 1000)
        except PwTimeout:
            return False
        return True

    def wait_grid_or_error(self, cid, sec):
        """表が出たら True。「再度操作を行って下さい」等の警告が出たら、待たずに False を返す"""
        n0 = len(self.dialogs)
        end = time.time() + sec
        while time.time() < end:
            try:
                if self.page.evaluate(f"document.getElementById('{cid}') !== null"):
                    return True
            except Exception:
                pass
            if len(self.dialogs) > n0:
                return False   # 通信エラーの警告が出た → すぐ再操作する
            time.sleep(0.5)
        return False

    def ensure_grid(self, opener, cid, tries=10):
        """空き状況の表が出るまで再操作をくり返す。
        警告が出たらすぐ表の読み込みをやり直し、それでもだめなら画面ごと開き直す（最大 tries 回）"""
        for i in range(tries):
            if self.wait_grid_or_error(cid, 60):
                return True
            if i == 0:
                self.shot("grid_missing")
            print(f"  表が出ません（{i + 1}/{tries}回目）。再度操作します")
            time.sleep(2)
            try:
                self.page.evaluate("typeof getWeekInfoAjax === 'function' && getWeekInfoAjax(11, 0, 0)")
            except Exception:
                pass
            if self.wait_grid_or_error(cid, 30):
                return True
            time.sleep(min(5 * (i + 1), 30))   # 混雑していそうなら少しずつ間をあける
            try:
                self.open_week(opener)   # 画面ごと開き直す
            except Exception as e:
                print(f"  画面の開き直しに失敗: {e}")
                try:
                    self.login()
                    self.open_week(opener)
                except Exception:
                    pass
        self.shot("grid_missing_last")
        return False

    def reset_selection(self, cid=None):
        """画面の「選択解除」を押して、残っている仮押さえを手放す（失敗しても先へ進む）"""
        pg = self.page
        try:
            if not pg.evaluate("typeof akireset === 'function' && !!document.form1"):
                return False
            pg.evaluate("akireset(document.form1)")
            time.sleep(2)
            if cid:
                self.wait_week(cid, 30)
            print("  選択解除を押して、残っていた仮押さえを解除しました")
            return True
        except Exception as e:
            print(f"  選択解除に失敗: {str(e)[:100]}")
            return False

    def book_cell(self, v):
        """空き状況画面上の1枠を予約する。成功で True、空いていなければ False、途中停止は Stop"""
        pg = self.page
        cid = f'{v["useDay"]}_{v["tzoneNo"]}'
        if not self.wait_week(cid, 60) or not self.cell_available(cid):
            return False
        n_dlg = len(self.dialogs)
        pg.evaluate(f"""(() => {{
            const td = document.getElementById('{cid}');
            (td.querySelector('[onclick*="setReserv"]') || td).click();
        }})()""")
        try:
            pg.wait_for_function("document.form1.selectSize && document.form1.selectSize.value != '0'", timeout=90000)
        except PwTimeout:
            self.shot("select_failed")
            msg = self.dialogs[-1].replace("\n", " ") if len(self.dialogs) > n_dlg else "警告なし"
            self.last_select_error = msg
            return None   # 空いているのに選べなかった
        self.shot("selected")
        try:
            with pg.expect_navigation(timeout=90000):
                pg.evaluate("checkSelect(document.form1, gRsvWOpeReservedApplyAction)")
        except PwTimeout:
            self.shot("apply_not_opened")
            raise RuntimeError("申込画面に進めませんでした（警告: " + (self.dialogs[-1] if self.dialogs else "なし") + "）")
        return self.walk_apply(v)

    def walk_apply(self, v):
        """申込画面から先を進める。画面の内容は文字で判断し、分からなければ止まる"""
        pg = self.page
        recaptcha_clicked = False  # 1件の申込につきクリックは最大1回
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
            if not REHEARSAL:
                recaptcha_clicked = self.check_reservation_recaptcha(recaptcha_clicked)
            btns = self.buttons()
            nxt = [b for b in btns if any(w in b[1] for w in NEXT_WORDS) and not any(w in b[1] for w in FINAL_WORDS)]
            fin = [b for b in btns if any(w in b[1] for w in FINAL_WORDS)
                   or re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff]", "", b[1]) in FINAL_EXACT]
            if not nxt and not fin and "予約内容確認" in body:
                # 予約内容確認画面：「キャンセル」以外の目立つボタン（btn-go など）を確定ボタンとみなす
                fin = [b for b in btns if b[2]]
            print("  画面内のボタン: " + " / ".join(b[1].replace("\n", " ")[:20] for b in btns))
            if nxt:
                target = nxt[0]
            elif fin:
                missing = self.missing_required()
                self.shot(f"apply{step}_filled")
                if missing:
                    raise Stop("必須の入力欄が空のままです: " + "、".join(missing))
                if REHEARSAL:
                    raise Stop(f"リハーサルのため「{fin[0][1]}」ボタンの直前で止めました（予約はされていません）")
                target = fin[0]
            else:
                self.dump_page(f"apply{step}_unknown")
                names = "、".join(b[1].replace("\n", " ")[:20] for b in btns) or "なし"
                raise Stop("次に押すボタンが判断できない画面になったため止めました（見つかったボタン：" + names + "）")
            print(f"  「{target[1]}」を押します")
            before = pg.url + str(len(body))
            n_dlg, n_fr = len(self.dialogs), len(pg.frames)
            target[0].click()
            self.wait_change(before, n_dlg, n_fr, 90)
            if pg.url + str(len(self.text())) == before and self.dialogs and "入力" in self.dialogs[-1]:
                raise Stop("入力不足の警告が出ました: " + self.dialogs[-1])
        raise Stop("申込画面が想定より長く続いたため止めました")

    def wait_change(self, before, n_dlg, n_fr, sec=90):
        """ボタンを押したあと、画面が実際に変わるまで待つ（スポレクは反応が遅いことがある）。
        変わったら True。sec 秒待っても変わらなければ False（そのまま次の判断へ進む）"""
        pg = self.page
        end = time.time() + sec
        while time.time() < end:
            try:
                pg.wait_for_load_state("domcontentloaded", timeout=max(1000, int((end - time.time()) * 1000)))
            except PwTimeout:
                break
            except Exception:
                pass
            try:
                if (pg.url + str(len(self.text())) != before or len(self.dialogs) > n_dlg
                        or len(pg.frames) != n_fr):
                    pg.wait_for_timeout(1500)   # 変わった直後は表示が落ち着くまで少し待つ
                    return True
            except Exception:
                pass   # 画面の切り替わり中は読めないことがある
            pg.wait_for_timeout(1000)
        print(f"  {sec}秒待っても画面が変わりませんでした。画面を確かめて続けます")
        return False

    def recaptcha_widgets(self):
        """表示中の reCAPTCHA のチェック欄だけを取得する（非表示のバッジは除く）。"""
        widgets = []
        for frame in self.page.frames:
            if "recaptcha" not in frame.url.lower():
                continue
            try:
                if not frame.frame_element().is_visible():
                    continue
                anchor = frame.locator("#recaptcha-anchor")
                if anchor.count() and anchor.first.is_visible():
                    widgets.append(anchor.first)
            except Exception:
                # 表示中の認証フレームを読めなければ予約を進めない。
                raise Stop("reCAPTCHAの状態を確認できないため止めました")
        return widgets

    def recaptcha_image_challenge(self):
        """画像・音声などの追加認証が表示されたか。解答やトークン操作はしない。"""
        for frame in self.page.frames:
            if "recaptcha" not in frame.url.lower():
                continue
            try:
                if not frame.frame_element().is_visible():
                    continue
                challenge = frame.locator("#rc-imageselect, #recaptcha-audio-button, #audio-source")
                for i in range(challenge.count()):
                    if challenge.nth(i).is_visible():
                        return True
            except Exception:
                raise Stop("追加認証の状態を確認できないため止めました")
        return False

    def check_reservation_recaptcha(self, clicked):
        """チェック欄を通常のクリックで1回押し、実際に認証済みになるまで待つ。"""
        if self.recaptcha_image_challenge():
            self.shot("recaptcha_manual_required")
            raise Stop("画像・音声などの追加認証が出たため止めました。手動で予約してください")
        widgets = self.recaptcha_widgets()
        if not widgets:
            return clicked
        if len(widgets) != 1:
            raise Stop("reCAPTCHAのチェック欄が複数あるため止めました")
        anchor = widgets[0]
        if anchor.get_attribute("aria-checked") == "true":
            return clicked
        if clicked:
            self.shot("recaptcha_not_verified")
            raise Stop("reCAPTCHAが再び未認証になったため止めました（繰り返しクリックはしません）")
        print("  「私はロボットではありません」を1回クリックします")
        try:
            anchor.click(timeout=5000)
        except Exception:
            self.shot("recaptcha_click_failed")
            raise Stop("reCAPTCHAのチェック欄をクリックできないため止めました")
        # 認証結果だけを最大30秒待つ。画像の解答・トークン偽装・再クリックはしない。
        until = time.monotonic() + 30
        while time.monotonic() < until:
            if self.recaptcha_image_challenge():
                self.shot("recaptcha_manual_required")
                raise Stop("画像・音声などの追加認証が出たため止めました。手動で予約してください")
            widgets = self.recaptcha_widgets()
            if len(widgets) == 1 and widgets[0].get_attribute("aria-checked") == "true":
                self.shot("recaptcha_checked")
                print("  reCAPTCHAにチェックが付きました。予約処理を続けます")
                return True
            self.page.wait_for_timeout(500)
        self.shot("recaptcha_timeout")
        raise Stop("reCAPTCHAの認証が30秒以内に完了しないため止めました（予約は続けません）")

    def buttons(self):
        """押せる部品（ボタン・リンク・onclick付き要素）を集める。画面上部のメニューは除く"""
        out = []
        sel = ("button, input[type=button], input[type=submit], input[type=image], "
               "a, [onclick], [role=button]")
        seen = set()
        for el in self.page.query_selector_all(sel):
            try:
                if not el.is_visible():
                    continue
                info = el.evaluate("""e => {
                    const r = e.getBoundingClientRect();
                    return {
                        nav: !!e.closest('header, nav, .navbar, .dropdown-menu, #header, .modal, footer'),
                        top: r.top + window.scrollY,
                        go: /btn-go|btn-primary|btn-next|btn-submit/.test(e.className || '') || /btn-go/.test(e.id || ''),
                        key: e.outerHTML.slice(0, 200)
                    };
                }""")
                if info["nav"] or info["top"] < 175 or info["key"] in seen:
                    continue
                seen.add(info["key"])
                t = (el.inner_text() or el.get_attribute("value") or el.get_attribute("alt") or "").strip()
            except Exception:
                continue
            if t and not any(w in t for w in AVOID_WORDS):
                out.append((el, t, info["go"]))
        return out

    def dump_page(self, name):
        """止まったときに画面の作り（HTML）を保存する"""
        try:
            p = os.path.join(SHOTS, f"{dt.datetime.now(JST):%m%d_%H%M%S}_{name}.html")
            with open(p, "w", encoding="utf-8") as f:
                f.write(self.page.content())
        except Exception:
            pass

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
        n = self.page.evaluate("""(n) => {
            let filled = 0;
            const rowText = (el) => {
                const r = el.closest('tr, .row, .form-group, dl, li');
                return (r ? r.innerText : '') || '';
            };
            for (const el of document.querySelectorAll('input[type=text], input[type=tel], input[type=number], input:not([type])')) {
                const lab = ((el.labels && el.labels[0] ? el.labels[0].innerText : '') + ' ' + (el.name||'') + ' ' + (el.id||'')
                             + ' ' + rowText(el)).toLowerCase();
                if ((lab.includes('人数') || lab.includes('ninzu')) && !el.value) {
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    setter.call(el, n);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                    filled++;
                }
            }
            for (const el of document.querySelectorAll('input[type=checkbox]')) {
                const lab = (el.labels && el.labels[0] ? el.labels[0].innerText : (el.parentElement ? el.parentElement.innerText : '')) || '';
                if (lab.includes('同意') && !el.checked) el.click();
            }
            return filled;
        }""", players)
        if n:
            print(f"  利用人数に {players} を入力しました（{n}か所）")

    def missing_required(self):
        """「必須」と書かれた行で、空のままの入力欄の項目名を返す"""
        return self.page.evaluate("""() => {
            const out = [];
            for (const r of document.querySelectorAll('tr, .row, .form-group')) {
                const t = r.innerText || '';
                if (!t.includes('必須')) continue;
                for (const el of r.querySelectorAll('input[type=text], input[type=tel], input[type=number], input:not([type]), select')) {
                    if (el.offsetParent !== null && !el.value) {
                        out.push(t.split('必須')[0].trim().split('\\n')[0]);
                        break;
                    }
                }
            }
            return [...new Set(out)];
        }""")

LAST_STOP = None   # 直前の run が止まった理由（サーバーでの見張り用）


class Session:
    """ブラウザを開いたまま使い回す（サーバーで常駐するとき）。session を渡さなければ毎回開いて閉じる"""

    def __init__(self):
        self.p = self.browser = self.b = None
        self.born = 0

    def start(self):
        if self.b is None:
            self.p = sync_playwright().start()
            self.browser = self.p.chromium.launch()
            page = self.browser.new_page(locale="ja-JP", viewport={"width": 1280, "height": 1600})
            page.set_default_timeout(120000)
            self.b = Booker(page)
            self.born = time.time()
        return self.b

    def logged_in(self):
        """ホーム画面を開き、右上が「ログイン」ボタンなら未ログイン"""
        try:
            pg = self.start().page
            pg.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=120000)
            el = pg.query_selector("#btn-login")
            return not (el and el.is_visible())
        except Exception:
            return False

    def ensure_login(self):
        b = self.start()
        if not self.logged_in():
            _orig_login(b)
        return b

    def close(self):
        for x in (self.browser, self.p):
            try:
                x.close() if x is self.browser else x.stop()
            except Exception:
                pass
        self.p = self.browser = self.b = None


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


def run(candidates, max_new, title, session=None):
    """候補を優先順に予約する。結果を通知して state.json に記録する"""
    state = load_state()
    booked_days = {b["date"] for b in state["booked"]}
    booked_times = {(b["date"], b.get("start")) for b in state["booked"]}
    max_new = max_new if max_new else float("inf")   # None = 上限なし
    results, done, stop_reason = [], 0, None
    lazy = callable(candidates)   # 監視（watch）：ログインと同時に空きを調べ、終わってから候補を受け取る
    get_candidates = candidates if lazy else None
    if lazy:
        candidates = []
    if not lazy and not candidates:
        print("予約候補はありません")
        if title == "22日一斉予約":
            notify(f"【{title}】候補なし", "条件に合う翌月分の枠が見つかりませんでした（予約は行っていません）")
        return
    weeks, grid_ok, grid_ng, empty = 0, 0, 0, 0
    select_ng, reset_done = 0, False
    own = session is None
    sess = session or Session()
    if True:
        b = sess.start()
        try:
            if own or not sess.logged_in():
                b.login()          # 22日は、このあと0時まで待つ
            elif _WAIT_UNTIL:
                _wait_open(b)      # ログイン済みの画面のまま0時まで待つ
            if lazy:
                candidates = get_candidates()
                get_candidates = None
                state = load_state()   # 空き確認が state.json を更新しているので読み直す
                booked_days = {x["date"] for x in state["booked"]}
                booked_times = {(x["date"], x.get("start")) for x in state["booked"]}
                if not candidates:
                    print("予約する候補はありません（ログインしたまま終了）")
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
                            weeks += 1
                            if b.ensure_grid(opener, f'{v["useDay"]}_{v["tzoneNo"]}'):
                                if not reset_done:
                                    b.reset_selection(f'{v["useDay"]}_{v["tzoneNo"]}')   # 前回の仮押さえを残さない
                                    reset_done = True
                                grid_ok += 1
                                if grid_ok == 1:
                                    b.shot("week_first")
                            else:
                                grid_ng += 1
                                results.append(f"× {opener['park']} {opener['court']} {opener['date']}の週：空き状況の表を読み込めませんでした")
                                if grid_ng >= 3 and grid_ok == 0:
                                    raise Stop("何度開き直しても空き状況の表が出ないため止めました"
                                               "（警告: " + (b.dialogs[-1].replace(chr(10), ' ') if b.dialogs else "なし") + "）")
                                break
                        ok = b.book_cell(v)
                    except Stop:
                        raise
                    except Exception as e:
                        state["attempts"][v["key"]] = state["attempts"].get(v["key"], 0) + 1
                        results.append(f"× {label(v)}（失敗: {str(e)[:120]}）")
                        need_open = True
                        continue
                    if ok is None:
                        select_ng += 1
                        results.append(f"× {label(v)}：空きなのに選択できませんでした（{b.last_select_error[:80]}）")
                        b.reset_selection()
                        reset_done = False
                        need_open = True
                        if select_ng >= 3 and done == 0:
                            raise Stop("空いている枠を押しても選択できない状態が続いたため止めました（警告: " + b.last_select_error[:80] + "）")
                        continue
                    if not ok:
                        empty += 1
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
            if REHEARSAL and "直前で止めました" in stop_reason:
                # リハーサルで押さえた枠を残さない：空き状況の画面に戻って「選択解除」
                try:
                    b.page.go_back(wait_until="domcontentloaded", timeout=60000)
                    time.sleep(3)
                    b.reset_selection()
                    b.shot("after_reset")
                except Exception as e2:
                    print(f"  戻って選択解除できませんでした: {str(e2)[:100]}")
        except Exception as e:
            stop_reason = f"想定外のエラー: {str(e)[:200]}"
            b.shot("error")
        finally:
            if own or stop_reason:
                sess.close()   # 止まったときは、次の回にブラウザを開き直してログインし直す
    global LAST_STOP
    LAST_STOP = stop_reason
    if lazy and get_candidates:
        # ログインで止まった場合も、空き確認（通知）は最後まで終わらせる
        try:
            candidates = get_candidates()
        except Exception as e:
            print(f"空き確認のエラー: {e}")
            candidates = []
        state = load_state()
    if lazy and not candidates:
        if not stop_reason:
            import glob
            for f in glob.glob(os.path.join(SHOTS, "*")):
                os.remove(f)   # 空きがない回の画面は残さない
        if stop_reason:
            print(f"停止: {stop_reason}（予約する候補がないので通知はしません）")
        return
    save_state(state)
    lines = [f"候補 {len(candidates)} 枠／画面を開いた週 {weeks}（表が出た {grid_ok}・出なかった {grid_ng}）／"
             f"空いていなかった枠 {empty}／空きなのに選べなかった枠 {select_ng}／予約 {done} 件", ""] + results
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
                                and cc.wanted(d, c["startTime"], c["useDay"] in hol, tgt):
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


def release(now_flag, session=None):
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
    run(cands, lim(rel.get("max_bookings")), "22日一斉予約", session=session)
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
    _wait_open(self)


def _wait_open(self):
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


def watch(session=None):
    """毎日の確認：ログイン（ブラウザ）と空き確認（ログイン不要の検索）を同時に始め、
    条件に合う空きがあれば、ログイン済みの画面ですぐ予約する"""
    import threading, check_courts as cc
    if not AB.get("enabled"):
        cc.main()
        return
    box = {}

    def work():
        try:
            cc.main()
        except Exception as e:
            box["err"] = e

    th = threading.Thread(target=work, daemon=True)
    th.start()

    def get():
        th.join()
        if "err" in box:
            raise RuntimeError(f"空き状況の確認でエラー: {box['err']}")
        queue = json.load(open(QUEUE_PATH, encoding="utf-8")) if os.path.exists(QUEUE_PATH) else []
        state = load_state()
        cap, per = cc.lim(AB.get("max_active")), cc.lim(AB.get("max_per_run"))
        if cap is not None and len(state["booked"]) >= cap:
            print("自動予約の上限に達しています")
            return []
        n = [x for x in ((None if cap is None else cap - len(state["booked"])), per) if x is not None]
        return queue[:min(n)] if n else queue

    if os.environ.get("TEST_BOOK"):
        run(get, 1, "【テスト予約】テニス自動予約", session=session)   # 1件だけ
        return
    run(get, None, "テニス自動予約", session=session)


if __name__ == "__main__":
    for k in ("SPOREC_USER_ID", "SPOREC_PASSWORD"):
        if not os.environ.get(k):
            print(f"{k} が設定されていません（GitHub の Secrets に登録してください）", file=sys.stderr)
            sys.exit(1)
    if "--release" in sys.argv:
        release("--now" in sys.argv)
    elif "--watch" in sys.argv:
        watch()
    else:
        hourly()
