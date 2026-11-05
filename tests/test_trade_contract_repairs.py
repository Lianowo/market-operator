import pytest

from market_operator.events import LEVEL_REJECTED, MarketEvent
from market_operator.events import BREAKOUT_ACCEPTED
from market_operator.hypothesis import StructureView
from market_operator.routing import MarketRouter, RouterParams, WHITELIST_BALANCE
from market_operator.structure import Regime, Zone, ZoneRole
from market_operator.position import PositionManager
from market_operator.tg import TelegramNotifier


def test_bz_market_numbers_describe_confirmation_position():
    zone = Zone(lo=111.73596, hi=112.23, role=ZoneRole.SUPPORT,
                box_opposite=109.55352)
    atr = (112.82178 - 111.73596) / 2
    ev = MarketEvent(BREAKOUT_ACCEPTED, -1, 0, 3,
                     {"zone": zone, "ref": zone.lo, "invalid": 112.3})
    p = RouterParams(whitelist=WHITELIST_BALANCE)
    dec = MarketRouter(p).on_bar("BZUSDT", (0, 110, 111, 109, 109.7, 1), 3,
          StructureView(Regime.TRANSITION, (zone,), atr), atr, [ev],
          position_open=False, entry_resting=False)[0]
    assert dec.kind == "market"
    assert dec.meta["confirm_close"] == 109.7
    n = TelegramNotifier({"TELEGRAM_BOT_TOKEN": "offline", "TELEGRAM_CHAT_ID": "offline"})
    bodies = []
    n._post_text_sync = lambda *args: bodies.append(args[-1]) or True
    n.send_signal("BZUSDT", ev.id, "sell", dec.route, dec.intent.ref_price, dec.hyp.invalid,
                  box_lo=109.55352, box_hi=112.23, price_context=dec.meta)
    assert "确认价位置: 5%" in bodies[0]
    assert "不是方向行程" in bodies[0]


def test_doge_fine_target_completion_arms_without_waiting_for_12h_close():
    pm = PositionManager(reject_opp_lock_frac=-.25, time_stop=False)
    pos = pm.open("DOGEUSDT", 1, .092248446, .0871726355129833,
                  .1023574157045584, LEVEL_REJECTED, 0, 0)
    pm.on_bar("DOGEUSDT", (900000, .102, .1036, .101, .10351, 1), .001,
              price_only=True, skip_trail=True)
    assert pos.armed
    assert pos.invalid > pos.entry
    assert pos.bars_held == 0
    # This is a functional regression for the candidate, not PnL acceptance.