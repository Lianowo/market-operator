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
