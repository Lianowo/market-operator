"""Offline notification contracts; never connects to Telegram."""
import threading
import queue

from market_operator.tg import TelegramNotifier, NotificationGuard, CHANNEL_OPS


def notifier():
    n = TelegramNotifier({"TELEGRAM_BOT_TOKEN": "SIG", "TELEGRAM_CHAT_ID": "PUBLIC",
                          "TELEGRAM_TRADE_BOT_TOKEN": "OPS", "TELEGRAM_TRADE_CHAT_ID": "PRIVATE"})
    calls = []
    n._post_text_sync = lambda token, chat, kind, body: calls.append((token, chat, kind, body)) or True
    return n, calls


def test_watch_template_and_ops_channel():
    n, calls = notifier()
    n.send_signal("BTCUSDT", "LEVEL_REJECTED", "buy", "level_rejected_limit", 100, 95,
                  watch_details={"watch_price": 100, "near_price": 101,
                                 "prog_price": 108, "max_wait_hours": 4})
    assert calls[0][0:2] == ("SIG", "PUBLIC")
    text = calls[0][3]
    assert "关注参考价: 100" in text and "4 小时" in text
    assert "观察失效价: 108" in text
    assert "系统执行" not in text and "LEVEL_REJECTED" not in text
    n.send_watch("BTCUSDT", "4h", "关注作废", "行情走完")
    assert calls[1][0:2] == ("OPS", "PRIVATE")
    assert n.channel_of("watch") == CHANNEL_OPS


def test_defense_notice_is_not_a_fill():
    n, calls = notifier()
    n.send_fill("BTCUSDT", "防线推进", 1, 110, entry_price=100,
                direction=1, price_label="参考价(认知帧收盘)")
    assert "持仓数量" in calls[0][3]
    assert "成交数量" not in calls[0][3]
    assert "持仓方向: 多" in calls[0][3]


def test_long_photo_message_preserves_full_watch_text():
    n, calls = notifier()
    photos = []
    n._post_photo_sync = lambda *args: photos.append(args) or True
    text = "关注说明" * 400
    assert n._post_photo_or_render("SIG", "PUBLIC", "signal", text, b"png", None)
    assert calls[0][3] == text
    assert len(photos) == 1


def test_async_transport_and_render_never_run_on_caller_thread():
    n = TelegramNotifier(async_mode=True)
    caller = threading.get_ident()
    threads = []
    n._post_photo_sync = lambda *args: threads.append(threading.get_ident()) or True

    def render():
        threads.append(threading.get_ident())
        return b"png"

    assert n.send("signal", "text", png_factory=render)
    n._q.join()
    assert len(threads) == 2 and all(t != caller for t in threads)
    n._q.put_nowait(None)


def test_bw_chart_pure_price_action_ignores_volume_and_renders_cleanly():
    """纯价格行为黑白结构图不依赖也不渲染成交量;5 列或 6 列 K 线渲染完全一致。"""
    import io

    from PIL import Image

    from market_operator.tg import render_bw_kline_png

    base = [(1700000000000 + i * 7200000, 100.0 + i * 0.5, 101.5 + i * 0.5,
             99.0 + i * 0.5, 101.0 + i * 0.5, 100.0) for i in range(30)]
    flat = render_bw_kline_png(base, symbol="BTCUSDT", tf_label="2h",
                               title_tag="SIGNAL")
    spiky = render_bw_kline_png(
        base[:-1] + [(base[-1][0], base[-1][1], base[-1][2], base[-1][3],
                      base[-1][4], 1200.0)],
        symbol="BTCUSDT", tf_label="2h", title_tag="SIGNAL")
    assert flat and spiky
    assert flat == spiky                      # 纯K线图表不受成交量干扰

    legacy = render_bw_kline_png([b[:5] for b in base], symbol="BTCUSDT",
                                 tf_label="2h", title_tag="SIGNAL")
    assert legacy == flat
    assert Image.open(io.BytesIO(legacy)).size == (1000, 560)

    class Boom:
        async_mode = True
        def send_alert(self, text):
            raise ValueError("bad template")
    guard = NotificationGuard(Boom())
    assert guard.send_alert("x") is False
    assert guard.errors == 1 and guard.async_mode is True


def test_bar_gap_stats_distinguishes_visual_spacing_from_missing_bars():
    from market_operator.tg import _bar_gap_stats

    t0 = 1700000000000
    dense = [(t0 + i * 7200000, 1.0, 1.0, 1.0, 1.0, 0.0) for i in range(6)]
    assert _bar_gap_stats(dense) == (6, 120, 0)

    # 120m, 120m, 240m: median 120m, one real 2x gap after the third bar.
    gapped = [(t0, 1, 1, 1, 1, 0), (t0 + 7200000, 1, 1, 1, 1, 0),
              (t0 + 14400000, 1, 1, 1, 1, 0),
              (t0 + 28800000, 1, 1, 1, 1, 0)]
    assert _bar_gap_stats(gapped) == (4, 120, 1)
    assert _bar_gap_stats([]) == (0, 0, 0)
    assert _bar_gap_stats([dense[0]]) == (1, 0, 0)


def test_queue_full_drops_without_network_or_blocking():
    n, calls = notifier()
    n.async_mode = True
    n._q = queue.Queue(maxsize=1)
    n._q.put_nowait((None, (), {}))
    assert n.send_alert("full") is False
    assert n.dropped == 1 and calls == []


def test_signal_broadcasts_amt_structural_pattern_and_source_kind():
    """公开信号广播隐藏核心算法思想(R3_TRAP/BOX_EDGE/平衡箱/系统开仓状态),仅对外展示可执行价位。"""
    from market_operator.tg import render_bw_kline_png

    n, calls = notifier()
    n.send_signal(
        "ETHUSDT", "LEVEL_REJECTED", "buy", "level_rejected_market", 2500.0, 2450.0,
        tf_label="4h", regime="TRANSITION", target_1=2650.0,
        exec_status="仅参考 · 4h 周期容量已满,本系统本拍未开仓",
        size_note="降档触发",
        box_lo=2460.0, box_hi=2660.0,
        price_context={
            "trade_context": "箱沿拒绝",
            "sub_kind": "R3_TRAP",
            "source_kind": "BOX_EDGE",
            "execution_target": 2650.0,
            "box_start_ts": 1700000000000 + 40 * 7200000,

        },
    )
    assert calls[0][0:2] == ("SIG", "PUBLIC")
    body = calls[0][3]
    assert "📡 交易信号: ETHUSDT" in body
    assert "🟢 交易方向: 做多 (LONG)" in body
    assert "🎯 参考入场: 2500" in body
    assert "🛡️ 止损防守: 2450" in body
    assert "🏁 目标参考: 2650 (R:R 3.00)" in body
    assert "📊 建议仓位:" in body and "%" in body
    assert "⚖️ 建议杠杆:" in body and "x" in body
    for secret in ("箱沿拒绝", "R3_TRAP", "BOX_EDGE", "TRANSITION", "REJECT",
                   "平衡箱体", "事件区间", "系统执行", "周期容量已满", "降档触发"):
        assert secret not in body

    # 验证 96 根 K 线与平衡区起点锚定渲染:带起点的图与全宽图不同
    bars_96 = [(1700000000000 + i * 7200000, 2400.0 + (i % 10) * 5,
                2420.0 + (i % 10) * 5, 2390.0 + (i % 10) * 5,
                2410.0 + (i % 10) * 5, 100.0) for i in range(96)]
    png_start = render_bw_kline_png(
        bars_96, symbol="ETHUSDT", tf_label="4h", title_tag="SIGNAL LONG",
        box_lo=2400.0, box_hi=2460.0, entry_price=2410.0, stop_price=2380.0,
        target_price=2470.0, direction=1,
        box_start_ts=bars_96[45][0], public_mode=True)
    png_full = render_bw_kline_png(
        bars_96, symbol="ETHUSDT", tf_label="4h", title_tag="SIGNAL LONG",
        box_lo=2400.0, box_hi=2460.0, entry_price=2410.0, stop_price=2380.0,
        target_price=2470.0, direction=1,
        box_start_ts=bars_96[0][0], public_mode=True)
    assert png_start and png_full and png_start != png_full

def test_long_alert_is_split_instead_of_truncated():
    """超长告警逐条切分,4000 字符以后的内容不再被静默丢掉。"""
    n, calls = notifier()
    text = "\n".join(f"行{i}" for i in range(1500))
    assert n.send_alert(text) is True
    chunks = [c[3] for c in calls]
    assert len(chunks) > 1
    assert all(len(c) <= 4000 for c in chunks)
    assert "行1499" in "\n".join(chunks)


class _Resp:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}

    def json(self):
        return self._payload


class _Session:
    """按序回放预设响应;记录每次提交的载荷(不联网)。"""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.posts: list[dict] = []

    def post(self, url, data=None, files=None, timeout=None):
        self.posts.append({"url": url, "data": data or {}, "files": files})
        return self.responses.pop(0) if self.responses else _Resp()


def _notifier_with_session(session) -> TelegramNotifier:
    return TelegramNotifier({"TELEGRAM_BOT_TOKEN": "SIG", "TELEGRAM_CHAT_ID": "PUBLIC",
                             "TELEGRAM_TRADE_BOT_TOKEN": "OPS",
                             "TELEGRAM_TRADE_CHAT_ID": "PRIVATE"},
                            session=session, async_mode=False)


def test_429_retry_after_is_honoured(monkeypatch):
    """429 按服务端 retry_after 等待;恢复后照常投递。"""
    from market_operator import tg as tgmod

    sleeps: list[float] = []
    monkeypatch.setattr(tgmod.time, "sleep", lambda s: sleeps.append(s))
    sess = _Session([_Resp(429, {"ok": False, "parameters": {"retry_after": 7}}),
                     _Resp(200, {"ok": True})])
    n = _notifier_with_session(sess)
    assert n._post_text_sync("TOK", "CHAT", "alert", "x") is True
    assert sleeps == [7]


def test_retry_budget_exhausted_does_not_sleep_after_last_attempt(monkeypatch):
    """重试预算用尽后不再白睡最后一轮退避。"""
    from market_operator import tg as tgmod

    sleeps: list[float] = []
    monkeypatch.setattr(tgmod.time, "sleep", lambda s: sleeps.append(s))
    sess = _Session([_Resp(500, {"ok": False}), _Resp(500, {"ok": False})])
    n = _notifier_with_session(sess)
    n.max_retries = 2
    assert n._post_text_sync("TOK", "CHAT", "alert", "x") is False
    assert n.dropped == 1
    assert sleeps == [tgmod.RETRY_BACKOFF_S]


def test_dropped_report_clears_counter_and_reports_once():
    """补报成功后必须清零;再次调用返回 0,不重复播报同一次故障。"""
    n, calls = notifier()
    n.async_mode = False
    n.dropped = 3
    assert n.flush_dropped_report() == 3
    assert n.dropped == 0
    assert "累计漏发 3 条" in calls[-1][3]
    assert n.flush_dropped_report() == 0
    assert len(calls) == 1


def test_successful_delivery_auto_reports_accumulated_drops_without_loop():
    """任意一次成功投递后自动补报漏发账;补报自身成功不再套娃。"""
    sess = _Session([_Resp(200, {"ok": True}), _Resp(200, {"ok": True})])
    n = _notifier_with_session(sess)
    n.dropped = 2
    assert n.send_alert("恢复") is True
    texts = [p["data"]["text"] for p in sess.posts]
    assert any("累计漏发 2 条" in t for t in texts)
    assert len(sess.posts) == 2
    assert n.dropped == 0


def test_dropped_report_failure_keeps_counter():
    """补报本身失败时原数保留,不能把这次失败再叠加成 3。"""
    sess = _Session([_Resp(500, {"ok": False})])
    n = _notifier_with_session(sess)
    n.max_retries = 1
    n.dropped = 2
    assert n.flush_dropped_report() == 0
    assert n.dropped == 2


def test_fill_notice_uses_level_band_wording():
    """运营通知与主线原子水平位口径一致:水平带/入场带内,不再叫平衡箱体。"""
    n, calls = notifier()
    n.send_fill("BTCUSDT", "开仓", 1, 100, entry_price=100,
                box_lo=95, box_hi=105)
    body = calls[0][3]
    assert "📦 水平带: [95, 105]" in body
    assert "📍 入场带内: 50% · 居中" in body
    assert "平衡箱体" not in body and "入场箱内" not in body


def test_split_text_hard_split_never_emits_empty_chunk():
    """单行超长硬切时不得产生空消息(Telegram 会直接 400 拒收)。"""
    from market_operator.tg import split_text

    assert split_text("x" * 40, 10) == ["x" * 10] * 4
    assert split_text("", 10) == [""]


def test_async_worker_auto_reports_accumulated_drops():
    """实盘用的是异步通知器:漏发补报必须由工作线程真正发出。"""
    sess = _Session([_Resp(200, {"ok": True}), _Resp(200, {"ok": True})])
    n = TelegramNotifier({"TELEGRAM_BOT_TOKEN": "SIG", "TELEGRAM_CHAT_ID": "PUBLIC"},
                         session=sess, async_mode=True)
    n.dropped = 1
    assert n.send_alert("恢复") is True
    n._q.join()
    texts = [p["data"]["text"] for p in sess.posts]
    assert any("累计漏发 1 条" in t for t in texts)
    assert n.dropped == 0
    n._q.put_nowait(None)


def test_photo_failure_with_text_fallback_counts_at_most_one_drop(monkeypatch):
    """图片失败走文本兜底:成功不计漏发,文本也失败时只计一条(不双计)。"""
    from market_operator import tg as tgmod

    monkeypatch.setattr(tgmod.time, "sleep", lambda s: None)
    sess = _Session([_Resp(500, {"ok": False}), _Resp(200, {"ok": True})])
    n = _notifier_with_session(sess)
    n.max_retries = 1
    assert n._post_photo_sync("TOK", "CHAT", "fill", "cap", b"png") is True
    assert n.dropped == 0

    sess2 = _Session([_Resp(500, {"ok": False}), _Resp(500, {"ok": False})])
    n2 = _notifier_with_session(sess2)
    n2.max_retries = 1
    assert n2._post_photo_sync("TOK", "CHAT", "fill", "cap", b"png") is False
    assert n2.dropped == 1


