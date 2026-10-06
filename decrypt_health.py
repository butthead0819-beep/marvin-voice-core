"""DecryptHealthMonitor：偵測接收端「收到封包卻持續解不開」的 secret_key desync 風暴。

背景（2026-06-23 incident）：網路抖動 → discord.py 快速 RESUME 語音 session → 沿用舊
secret_key，但 Discord 在空檔換了 key → 接收封包永遠解不開（CryptoError 風暴）。KeySync
補丁重抓 `voice_client.secret_key` 重試，但那把 key 本身就是舊的、重讀無用；而這層傳輸層
CryptoError 被 KeySync drop 掉、Sentinel（只數 DAVE 層）看不到 → 炸 40 分鐘升級永不觸發。
只有一次**完整重連**（非 RESUME）拿到新 key 才修得好。

本模組是純邏輯（now 由 caller 傳入、無時鐘無 IO），偵測「持續零成功解密」→ 通知 caller
觸發完整重連自癒。CryptoError 要有封包才會出現 → 沒人講話不誤判（天然 gate）。
"""
from __future__ import annotations


class DecryptHealthMonitor:
    """收到封包卻持續解不開的偵測器。

    record_success / record_failure 餵入每次解密結果（now=time.time()）；
    should_escalate(now) 在「連續 ≥min_failures 次失敗且跨度 ≥sustained_s 秒、期間零成功」
    時回 True 一次（升級完整重連），之後等下一次 record_success 才能再升級（不 spam）。

    - min_failures：確認封包真的在穩定流進（不是零星雜散封包）。
    - sustained_s：撐過這段時間仍零解密 → 不是 KeySync 救得回的瞬間抖動、是真 desync。
    """

    def __init__(self, sustained_s: float = 8.0, min_failures: int = 10):
        self.sustained_s = sustained_s
        self.min_failures = min_failures
        self._fail_streak = 0
        self._streak_start = 0.0
        self._escalated = False

    def record_success(self, now: float) -> None:
        """成功解密（key 同步正常）→ 清 streak、解除升級閂（恢復後可再升級）。"""
        self._fail_streak = 0
        self._streak_start = 0.0
        self._escalated = False

    def record_failure(self, now: float) -> None:
        """KeySync 重抓 key 後仍 CryptoError（key 本身壞）→ 累計連續失敗。"""
        if self._fail_streak == 0:
            self._streak_start = now
        self._fail_streak += 1

    def should_escalate(self, now: float) -> bool:
        """是否該升級完整重連。達標時回 True 一次後上閂，等 record_success 才會再放行。"""
        if self._escalated:
            return False
        if self._fail_streak < self.min_failures:
            return False
        if now - self._streak_start < self.sustained_s:
            return False
        self._escalated = True
        return True


def _sorted_list(values: set) -> list:
    """集合轉排序 list；型別混雜無法比較時退回 repr 排序。"""
    try:
        return sorted(values)
    except TypeError:
        return sorted(values, key=repr)


class DecryptAttribution:
    """解密結果歸因：依 SSRC 聚合「自上次 emit 以來」的 ok / fail 特徵，定位是誰、什麼特徵的封包解不開。

    純邏輯（無 IO / 無時鐘），now 由 caller 傳入。ok 與 fail 各自一組子統計（次數、pt/ext/cc 集合、
    封包大小範圍、該窗第一個原始標頭樣本），讓異常窗口能和乾淨窗口比對。
    summary() 視窗到期（或 force）時吐一行字串並清空統計；乾淨窗口存成「基準」，異常時先印基準。
    """

    def __init__(self, window_s: float = 60.0, max_ssrcs: int = 8):
        self.window_s = window_s
        self.max_ssrcs = max_ssrcs
        self._stats: dict = {}
        self._fail_total = 0
        self._last_emit = None
        self._baseline: str | None = None
        self._in_anomaly = False

    @staticmethod
    def _new_sub() -> dict:
        return {"n": 0, "pt": set(), "ext": set(), "cc": set(),
                "min": None, "max": None, "hdr": None}

    def record(self, ssrc, ok: bool, now: float, *, uid=None, payload=None,
               extended=None, cc=None, size=None, sample=None) -> None:
        """餵入一筆解密結果。第一筆時把視窗起點定在 now。"""
        if self._last_emit is None:
            self._last_emit = now
        b = self._stats.get(ssrc)
        if b is None:
            b = {"uid": None, "ok": self._new_sub(), "fail": self._new_sub()}
            self._stats[ssrc] = b
        if uid is not None:
            b["uid"] = uid
        s = b["ok"] if ok else b["fail"]
        s["n"] += 1
        if not ok:
            self._fail_total += 1
        if payload is not None:
            s["pt"].add(payload)
        if extended is not None:
            s["ext"].add(extended)
        if cc is not None:
            s["cc"].add(cc)
        if size is not None:
            s["min"] = size if s["min"] is None else min(s["min"], size)
            s["max"] = size if s["max"] is None else max(s["max"], size)
        if sample is not None and s["hdr"] is None:
            s["hdr"] = sample

    def summary(self, now: float, force: bool = False) -> str | None:
        """視窗到期（或 force）才結算並清空統計；否則回 None（不清統計）。

        - 從沒 record → None。
        - 本窗無失敗：force → None（不動基準）；到期 → 存成基準，若剛從異常恢復則回 "[恢復後] …"。
        - 本窗有失敗：回 "[異常前基準] … ‖ [異常] …"（進入異常時）或 "[異常] …"（持續異常）。
        """
        if self._last_emit is None:
            return None
        if not force and now - self._last_emit < self.window_s:
            return None
        if self._fail_total == 0:
            if force:
                return None
            body = self._fmt_window()
            self._reset(now)
            if self._in_anomaly:
                self._in_anomaly = False
                self._baseline = body
                return "[恢復後] " + body
            self._baseline = body
            return None
        body = self._fmt_window()
        self._reset(now)
        if not self._in_anomaly:
            prefix = f"[異常前基準] {self._baseline or '(無)'} ‖ [異常] "
            self._in_anomaly = True
        else:
            prefix = "[異常] "
        return prefix + body

    def _reset(self, now: float) -> None:
        self._stats = {}
        self._fail_total = 0
        self._last_emit = now

    def _fmt_window(self) -> str:
        if not self._stats:
            return "(無封包)"
        ranked = sorted(
            self._stats.items(),
            key=lambda kv: (-kv[1]["fail"]["n"], kv[0] is None, kv[0] if kv[0] is not None else 0),
        )
        parts = [f"ssrc={ssrc} uid={b['uid']} ok={self._fmt_sub(b['ok'])} fail={self._fmt_sub(b['fail'])}"
                 for ssrc, b in ranked[:self.max_ssrcs]]
        omitted = len(ranked) - self.max_ssrcs
        if omitted > 0:
            parts.append(f"…+{omitted} ssrc")
        return " | ".join(parts)

    @staticmethod
    def _fmt_sub(s: dict) -> str:
        if s["n"] == 0:
            return "0"
        ln = "-" if s["min"] is None else f"{s['min']}-{s['max']}"
        hdr = "-" if s["hdr"] is None else s["hdr"]
        return (f"{s['n']}(pt={_sorted_list(s['pt'])} ext={_sorted_list(s['ext'])} "
                f"cc={_sorted_list(s['cc'])} len={ln} hdr={hdr})")
