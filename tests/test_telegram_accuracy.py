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
    assert "非交易所限价挂单" in text and "4 小时" in text
    assert "行情走完作废价: 108" in text
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


def test_queue_full_drops_without_network_or_blocking():
    n, calls = notifier()
    n.async_mode = True
    n._q = queue.Queue(maxsize=1)
    n._q.put_nowait((None, (), {}))
    assert n.send_alert("full") is False
    assert n.dropped == 1 and calls == []