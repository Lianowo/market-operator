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


def test_guard_catches_template_failures_and_preserves_metadata():
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