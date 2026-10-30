"""持仓管理状态机(docs/methodology.md §8 六条,全部事件驱动、逐 bar 调用)。

每 bar 对持仓评估一次,规则优先级 §8.1 > §8.5 > §8.2 > §8.3 > §8.6
(出场类规则先到先判,同一 bar 不重复出场;§8.2 减仓与 §8.3 移动防线可
同 bar 并存,§8.2 在前):

1. §8.1 破位退出:收盘越过 invalid ⇒ EXIT 剩余全部。
2. §8.5 反向 STRUCTURE_BROKEN:新事件中存在方向与持仓相反的 STRUCTURE_BROKEN ⇒ EXIT 剩余全部(CHoCH)。
3. §8.2 target_1:bar 极值触及目标 ⇒ REDUCE 当前仓 50%(仅一次,§9.1 的
   target 为结构区域近缘),余仓 invalid 移至 entry(保本,只收紧不放松)。
4. §8.3 移动防线:每新确认的逆向 pivot(空仓=pivot high,多仓=pivot low)
   ⇒ invalid 收紧至 pivot 外侧 ± k_bm·atr(只许向有利方向移动,永不放松;
   防线调整无订单,记录于 Position.log)。
5. §8.4 加仓:v1 关闭(PositionManager(allow_add=False),阶段 3 TREND_RESUMED
   NO-GO ⇒ 策略层不出现加仓输入)。try_add 完整实现:同向新 TREND_RESUMED 且浮盈
   ≥ 1R 时允许一次,合并后最坏情景损失不得劣于加仓前(风险中性约束),
   由独立测试验证。
6. §8.6 时间止损:bars_held 达到 n_time(§7 先验 N_time=24)时
   MFE < 0.5R ⇒ EXIT(MFE 以 bar 极值度量,R = |entry − 初始 invalid|)。

OrderIntent.qty_frac 约定:以该仓 OPEN 名义为基准 1.0;REDUCE=减仓部分
(0.5),EXIT=剩余全部(未减仓 1.0 / 减仓后 0.5),ADD=新增部分;账本可对账
(OPEN 1.0 − REDUCE 0.5 − EXIT 0.5 = 0)。权益占比换算属 sizing/记账层。

v6 增量(docs/v6-protocol.md §2-S2,加性开关,默认参数行为逐位不变):
- breakeven_on_reduce=False(臂 A "Target-Clamp"):§8.2 减仓成交后**不再**
  把 invalid 移至 entry(反模式实证禁令:保本移动),防线只随 §8.3 pivot
  收紧;默认 True ⇒ v1/v4/v5 逐位旧行为。
- time_stop=False(对照臂 B "Turtle"):§8.6 时间止损整条关闭;默认 True
  ⇒ v1/v4/v5 逐位旧行为。

"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .events import TREND_RESUMED, STRUCTURE_BROKEN, MarketEvent
from .hypothesis import Hypothesis
from .broker import OrderIntent, OrderKind
from .structure import Params, Pivot, PivotKind, _bar_parts

MFE_MIN_R = 0.5          # §7 expiry 先验:N_time 根内 MFE < 0.5R
ADD_MIN_R = 1.0          # §8.4 先验:浮盈 ≥ 1R 才允许加仓
# ---------------------------------------------------------------------------
# v21 对侧锁利(唯一出口;position.py 与 live_runner.py 共用 `opp_lock_step`)
#
# 语义(2026-10-24 重写:回吐预算,不是绝对价位):
#
#     budget = (1 − |frac|) · |target_1 − entry|      # 建仓即冻结(价格单位)
#     stop   = peak − d · budget                       # d=+1 多 / d=−1 空
#
#   - peak = 入场以来最有利极值(`Position._best`,单调不回落);峰值
#     天然 ≥ target(arming 的定义就是价格到过那里),所以 stop 恒在
#     entry 的盈利侧 —— 不需要"落位/盈利"闸门去兜(旧的
#     `lock_is_protective` / `lock_is_profitable` 已随绝对价位口径删除)。
#   - arming 由**价格事实**决定:frac<0 ⇒ 收盘突破对侧(acceptance),
#     frac>0 ⇒ 盘中触及(touch)。一经发生即保持,此后每根 bar 重新推演
#     ⇒ 棘轮可多次推进(不是"一次性置位")。
#   - 三态穷尽(无第四态):
#       ① 未 arming        ⇒ 保持结构防线(不预设任何锁利价);
#       ② stop 在保护侧    ⇒ 推进(只紧不松);
#       ③ stop 已越过现价  ⇒ 立即市价离场,不留"挂着却永不触发"的单。
#
# 关键恒等式(为什么不再需要"落位闸门"):
#     stop_protect_dist(d, stop, ref) ≡ budget − d·(peak − ref)
#                                    ≡ budget − 回撤
#   "锁利价在现价保护侧" ⇔ "自峰值回撤 < 预算",两者是同一件事。把价位写成
#   entry + |frac|·span(绝对价位)时,二者会脱钩 —— 决策时现价已越过该价位,
#   于是只剩两种坏结果:拒单(不推进 = 裸奔到收盘)或挂上即成交(止盈单
#   冒充止损单)。写成"峰值 − 预算"后,"越过"不再是非法输入,而是一个
#   必须处置的状态(离场)。
# ---------------------------------------------------------------------------

OPP_LOCK_EPS = 1e-12   # 价格单位的最小判据(浮点噪声;不是风控门槛)


def stop_protect_dist(direction: int, stop_px: float, ref_px: float) -> float:
    """止损位相对现价的有向距离:**> 0 才是有效保护单**。

    多头(d=+1)止损必须低于现价 ⇒ ref−stop;空头(d=−1)止损必须高于现价
    ⇒ stop−ref。统一写成 −d·(stop−ref),两个方向共用一个式子,避免
    "某一侧写反"这类不可测的灾难。
    """
    return -float(direction) * (float(stop_px) - float(ref_px))


def opp_lock_budget(entry: float, target: float,
                    frac: float) -> Optional[float]:
    """回吐预算(价格单位)=(1 − |frac|)·|target − entry|(**建仓即冻结**)。

    三个端点都有本义,不是拟合参数:

    - `|frac| → 0`(不锁)  ⇒ 预算 = span,等于"允许回吐整段计划行程";
    - `|frac| = 0.25`      ⇒ 预算 = ¾span(生产标定值 −0.25:锁 ¼、让 ¾);
    - `|frac| → 1`(全锁)   ⇒ 预算 = 0 ⇒ 触到目标即离场。

    frac<=0 / span 退化 ⇒ None(锁利关闭,调用方不得推进防线)。
    """
    span = abs(float(target) - float(entry))
    f = abs(float(frac))
    if f <= 0.0 or span <= 0.0 or span != span:      # span != span ⇒ NaN
        return None
    return max(0.0, 1.0 - f) * span


def opp_lock_peak_px(direction: int, peak: float, budget: float) -> float:
    """防线价 = 峰值 ∓ 预算(空头 +预算 / 多头 −预算)。"""
    return float(peak) - float(direction) * float(budget)


def opp_lock_arm_hit(direction: int, target: float, frac: float,
                     high: float, low: float, close: float) -> bool:
    """arming 判据(价格事实,与调用频率无关):

    - `frac < 0` ⇒ **收盘**突破对侧缘(acceptance,协议默认);
    - `frac > 0` ⇒ **盘中**触及对侧缘(touch);
    - `frac == 0` ⇒ 未启用锁利。
    """
    tgt, f = float(target), float(frac)
    if f < 0.0:
        return bool(float(close) < tgt) if direction < 0 else bool(float(close) > tgt)
    if f > 0.0:
        return bool(float(low) <= tgt) if direction < 0 else bool(float(high) >= tgt)
    return False


@dataclass(frozen=True)
class LockDecision:
    """一次锁利裁决(纯事实记录,调用方据此改状态/下单)。"""

    action: str                    # "off" | "hold" | "tighten" | "exit"
    armed: bool = False            # 裁决后是否已 arming(单调)
    stop_px: Optional[float] = None
    budget: Optional[float] = None
    drawdown: Optional[float] = None   # 自峰值的逆向回吐(保护侧方向为正)
    reason: str = ""


def opp_lock_step(*, direction: int, entry: float, target: float, frac: float,
                  peak: float, high: float, low: float, close: float,
                  ref_px: Optional[float] = None,
                  armed: bool = False,
                  budget_override: Optional[float] = None) -> LockDecision:

    """锁利唯一裁决入口(position.py / live_runner.py 共用)。

    纯函数:只读价格事实,不碰仓位状态。调用契约 ——

    - `peak` 必须是**已含本根 bar 极值**的入场以来最有利极值;
    - `ref_px` 是"当下可见的参考价"(逐 bar 用收盘 `c`;实盘换单前用
      mark 与最新价中更保守的一侧),默认取 `close`;
    - 返回 `hold` ⇒ 什么都不做(防线仍是结构止损);
      `tighten` ⇒ 把 invalid 推进到 `stop_px`(可多次棘轮,只紧不松);
      `exit` ⇒ 立即按 `ref_px` 市价离场(`lock_giveback_exhausted`);
      `off` ⇒ 锁利参数域不成立(frac=0 或 target 落在 entry 反侧)。

    三态穷尽:不存在"拒单后原地等待"的第四态,也就不存在裸奔窗口。
    """
    d = 1 if direction > 0 else -1
    entry, target = float(entry), float(target)
    if float(frac) == 0.0:
        return LockDecision("off", armed=bool(armed), reason="lock_disabled")
    # ATR 单位预算(2026-10-30):调用方已把"预算"算成价格单位并封顶 span,
    # 这里只做参数域校验;span 比例口径仍由 `opp_lock_budget` 提供。
    budget = (float(budget_override) if budget_override is not None
              else opp_lock_budget(entry, target, frac))
    if budget is None or d * (target - entry) <= 0.0:
        # 目标在建仓价反侧(箱体外缘被成交价穿过):行程无从定义 ⇒ 不锁利、
        # 不减仓,保持结构防线。这是**参数域**判断,不是"闸门拒单"。
        return LockDecision("off", armed=bool(armed), reason="degenerate_span")

    ref = float(close) if ref_px is None else float(ref_px)
    armed_now = bool(armed) or opp_lock_arm_hit(d, target, frac, high, low, close)
    if not armed_now:
        return LockDecision("hold", armed=False, budget=budget,
                            reason="not_armed")

    peak_px = float(peak)
    stop_px = opp_lock_peak_px(d, peak_px, budget)
    drawdown = d * (peak_px - ref)          # 保护侧方向为正
    if stop_protect_dist(d, stop_px, ref) > OPP_LOCK_EPS:
        return LockDecision("tighten", armed=True, stop_px=stop_px,
                            budget=budget, drawdown=drawdown, reason="ratchet")
    return LockDecision("exit", armed=True, stop_px=stop_px, budget=budget,
                        drawdown=drawdown, reason="giveback_exhausted")


def fmt_px(px: Optional[float]) -> str:
    """Optional 价格的安全格式化(日志用;None ⇒ "n/a")。"""
    return "n/a" if px is None else f"{float(px):.8g}"



@dataclass
class Position:
    """持仓状态(§8 状态机的事实记录)。initial_risk 恒为开盘口径 R 基准,
    加仓不改变 R 度量。"""

    symbol: str
    direction: int                 # +1 多 / -1 空
    entry: float                   # 加权均价(加仓后更新)
    invalid: float                 # 当前结构失效价(只许向有利方向移动)
    target_1: Optional[float]
    event_id: str                  # 开仓机制(BREAKOUT_ACCEPTED/STRUCTURE_BROKEN)
    opened_ts: object
    opened_index: int
    initial_risk: float            # |entry_open − invalid_open|(R 基准,>0)
    size_frac: float = 1.0         # 剩余名义(以 OPEN 为 1.0)
    reduced: bool = False          # §8.2 仅在实际成交后为 True
    pending_reduce: bool = False   # v4:限价减仓待成交;不提前减量或移止损
    pending_add: bool = False      # future engine:加仓委托待成交,不提前滚仓
    pending_add_invalid: Optional[float] = None
    added: bool = False            # §8.4 加仓仅一次
    bars_held: int = 0             # 已收盘 bar 数(入场 bar 记第 1 根)
    _best: Optional[float] = None  # 入场以来最有利极值(空=min low,多=max high)
    log: list = field(default_factory=list)   # 无订单的决策记录(§8.3 防线等)
    invalid0: Optional[float] = None  # 开仓时的结构失效位,成交回执校正 R 时保留原始锚点
    armed: bool = False            # v21.7:对侧锁利已 arming(价格事实;一经 True 不回落)
    lock_off_logged: bool = False  # 锁利参数域不成立(目标在 entry 反侧)只记一次
    giveback_unit: Optional[float] = None  # ATR 预算单位(建仓后首根冻结)
    reenter_invalid_px: Optional[float] = None  # ACCEPT 重返箱内证伪锚点(被破箱沿内侧或箱中轴)


    @property
    def mfe_r(self) -> float:
        """最大有利行程,以初始 R 为单位(bar 极值口径,entry 取开盘口径)。"""
        return self.direction * (self._best - self._entry0) / self.initial_risk

    def __post_init__(self) -> None:
        if self.initial_risk <= 0.0:
            raise ValueError("initial_risk must be positive")
        if self._best is None:
            self._best = self.entry
        self._entry0 = self.entry   # 开盘口径均价(加仓不改变 R 度量基准)
        if self.invalid0 is None:
            self.invalid0 = self.invalid   # v16:原始结构失效位快照


class PositionManager:
    """§8 六条规则状态机;每 symbol 一个持仓,逐 bar 裁决。"""

    def __init__(self, params: Optional[Params] = None,
                 allow_add: bool = False,
                 breakeven_on_reduce: bool = True,
                 time_stop: bool = True,
                 reject_opp_lock_frac: float = 0.0,
                 giveback_atr_k: Optional[float] = None,
                 accept_reenter_box_exit: bool = False) -> None:

        self.p = params or Params()
        self.allow_add = allow_add
        self.accept_reenter_box_exit = bool(accept_reenter_box_exit)
        self.breakeven_on_reduce = breakeven_on_reduce  # v6 臂 A:False=禁保本
        self.time_stop = time_stop                      # v6 臂 B:False=关 §8.6
        self.reject_opp_lock_frac = float(reject_opp_lock_frac)
        # ATR 单位回吐预算(None ⇒ span 比例口径,逐位旧行为)
        self.giveback_atr_k = (None if giveback_atr_k is None
                               else float(giveback_atr_k))

        # 符号是 arming 口径(不是"两把尺子"):<0 收盘突破(acceptance),
        # >0 盘中触及(touch);|frac| 决定回吐预算 (1−|frac|)·|target−entry|。
        # 旧 `opp_lock_max_giveback_r`(以 R 为单位的第二把尺子)已删除:
        # 它与 |frac| 量纲不同、互相遮盖,且"取更紧者"会把锁利价推到来不及
        # 保护的位置(`reports/opp_lock_ab.md`:0.25R 在 dev/test 全为负)。
        self.positions: dict[str, Position] = {}
        self.last: list[str] = []            # 最近 on_bar 判定记录(解释性)

    # -- 建仓 ------------------------------------------------------------------

    def open(self, symbol: str, direction: int, entry: float, invalid: float,
             target_1: Optional[float], event_id: str, ts: object,
             index: int, *, reenter_invalid_px: Optional[float] = None) -> Position:
        risk = direction * (entry - invalid)
        if risk <= 0.0:
            raise ValueError(f"invalid not protective: entry={entry} "
                             f"invalid={invalid} direction={direction}")
        pos = Position(symbol=symbol, direction=direction, entry=float(entry),
                       invalid=float(invalid), target_1=target_1,
                       event_id=event_id, opened_ts=ts, opened_index=index,
                       initial_risk=float(risk),
                       reenter_invalid_px=(float(reenter_invalid_px)
                                           if reenter_invalid_px is not None else None))
        self.positions[symbol] = pos
        return pos

    def open_from_hypothesis(self, symbol: str, hyp: Hypothesis, entry: float,
                             ts: object, index: int) -> Position:
        """由 §7 假设建仓(entry 为下一 bar 开盘成交价)。"""
        return self.open(symbol, hyp.direction, entry, hyp.invalid,
                         hyp.target_1, hyp.event_id, ts, index)

    def retag_entry(self, symbol: str, fill_px: float) -> bool:
        """实盘**成交回执**校正:把建仓均价改为交易所成交均价(§6a)。

        实盘市价单下单瞬间拿不到成交价,只能先按参考价记账;回执到达后必须
        回写,否则 `mfe_r`、锁利预算 `(1−frac)·|target−entry|`、保本判定
        全部沿用参考价口径 —— 而参考价与真实成交价实测差 −120~+74 bps,
        足以让 1R 与实际风险脱钩。

        纪律:
        * `invalid`/`invalid0` **不动** —— 防线是结构事实,不因成交滑点放宽;
        * R 基准 `initial_risk` 随新 entry 重算(与 `invalid0` 配对);
        * `_best` 只在新 entry 之外才需要重设,绝不让 MFE 因回写而变小到大
          于已记录极值(那是同一段行情的既成事实)。
        * 成交价已越过防线(`direction·(px−invalid) ≤ 0`)或仓位已不在 ⇒ 不校正。
        """
        pos = self.positions.get(symbol)
        if pos is None:
            return False
        px = float(fill_px)
        if px <= 0.0 or pos.direction * (px - pos.invalid) <= 0.0:
            return False
        base = float(pos.invalid0 if pos.invalid0 is not None else pos.invalid)
        risk = abs(px - base)
        if risk <= 0.0:
            return False
        pos.entry = px
        pos._entry0 = px
        pos.initial_risk = risk
        if pos._best is None or pos.direction * (pos._best - px) < 0.0:
            pos._best = px
        return True

    # -- 逐 bar 裁决 -----------------------------------------------------------

    def on_bar(self, symbol: str, bar, atr: float,
               new_pivots: Sequence[Pivot] = (),
               new_events: Sequence[MarketEvent] = (),
               defer_reduce: bool = False,
               defer_add: bool = False,
               price_only: bool = False,
               skip_trail: bool = False) -> list[OrderIntent]:
        """按优先级 §8.1>§8.5>§8.2>§8.3>§8.6 评估;返回订单意图列表。

        price_only=True 是**执行细 bar 的价格型出场推进**(v21.6,默认关):
        只跑与价格直接相关的裁决 —— §8.1 破位兜底、§8.2 目标/锁利推进、
        §8.3 移动防线;不消费事件(§8.5/§8.4 由空事件序列自然跳过),也不推进
        时间类计数(bars_held)⇒ §8.6 时间止损仍按
        认知 bar 计。调用方只在**已收盘的执行细 bar**上、且持仓已过入场 bar
        时调用,策略链路(路由/准入/开仓)完全不经此路径。

        skip_trail=True(仅与 price_only 一起用)进一步只保留 §8.2 目标/锁利与
        §8.1 兜底、不动 §8.3 移动防线 —— 用来把「更早收网」拆成「更早锁利」与
        「更早收紧 trail」两个来源,判定哪一半在细尺度上真有钱。
        """
        pos = self.positions.get(symbol)
        if pos is None:
            return []
        self.last = []
        ts, o, h, low, c, _v = _bar_parts(bar)
        if not price_only:
            pos.bars_held += 1
        pos._best = min(pos._best, low) if pos.direction < 0 else max(pos._best, h)
        # §8.1: close beyond the structural invalidation exits the position.
        if (c > pos.invalid) if pos.direction < 0 else (c < pos.invalid):
            reason = (f"§8.1|invalidation close close={c:.8g} "
                      f"invalid={pos.invalid:.8g} exit all")
            return [self._exit(pos, ts, c, reason)]

        # 1.5 ACCEPT 重返箱内接受证伪退出:突破单在认知 bar 收盘重新跌穿被破箱沿内侧
        #     ⇒ "箱外接受"前提已证伪,不再死扛 2·ATR 初始止损
        if (self.accept_reenter_box_exit
                and not price_only
                and pos.event_id == "BREAKOUT_ACCEPTED"
                and pos.reenter_invalid_px is not None):
            re_px = float(pos.reenter_invalid_px)
            if (c > re_px) if pos.direction < 0 else (c < re_px):
                reason = (f"§8.1b|accept_reentered_box close={c:.8g} "
                          f"reenter_px={re_px:.8g} exit all")
                return [self._exit(pos, ts, c, reason)]

        # 2. §8.5 反向 STRUCTURE_BROKEN(CHoCH 出场)
        #    price_only(执行细 bar 价格型推进)不消费事件:事件只能在其决策
        #    时点(认知/trigger 收盘)被裁决一次,否则同一事件会被重复消费。
        for ev in (() if price_only else new_events):
            if ev.id == STRUCTURE_BROKEN and ev.direction == -pos.direction:
                reason = (f"§8.5|STRUCTURE_BROKEN reverse CHoCH event_ts={ev.ts} "
                          f"exit all")
                return [self._exit(pos, ts, c, reason)]

        intents: list[OrderIntent] = []

        # 3. §8.2 target_1 触及 ⇒ 对侧锁利 / 传统减仓 50%
        #    锁利唯一出口 `opp_lock_step`(与 live_runner._on_sub_bar 同一函数,
        #    禁止各自重算):
        #      stop = peak ∓ (1−|frac|)·|target−entry|,peak=`_best`(已含本根
        #      bar 极值,单调)。恒等式 `stop_protect_dist ≡ budget − 回撤`
        #      ⇒ "防线在保护侧"与"回吐未超预算"同真同假,构造上不会算出非法
        #      价位,因而不存在"拒单 ⇒ 原地裸奔"的态。
        #    三态:未 arming ⇒ 保持结构防线;保护侧 ⇒ 推进(可多次棘轮,
        #    不是一次性置位);已越过 ⇒ 立即市价离场。
        #    frac==0 才走传统 §8.2 减仓 50%(两模式按参数域互斥,不再共用
        #    `reduced` 这个"一次性额度"标志)。
        if pos.target_1 is not None and not pos.pending_reduce:

            frac_cfg = self.reject_opp_lock_frac
            if frac_cfg == 0.0:
                hit = (low <= pos.target_1) if pos.direction < 0 \
                    else (h >= pos.target_1)
                if hit and not pos.reduced:
                    extreme = low if pos.direction < 0 else h
                    if defer_reduce:
                        intents.append(self._defer_reduce(pos, ts, c, extreme))
                    else:
                        intents.append(self._reduce(pos, ts, c, extreme))
            else:
                # ATR 单位回吐预算(可选):建仓后首根冻结单位,封顶为 span
                # (不允许回吐超过整段计划行程 ⇒ 否则等于"不锁利")。
                budget_override = None
                if self.giveback_atr_k is not None and atr > 0.0:
                    if pos.giveback_unit is None:
                        pos.giveback_unit = float(atr)
                    span_px = abs(float(pos.target_1) - float(pos.entry))
                    budget_override = min(float(self.giveback_atr_k)
                                          * float(pos.giveback_unit), span_px)
                ld = opp_lock_step(
                    direction=pos.direction, entry=pos.entry,
                    target=pos.target_1, frac=frac_cfg, peak=pos._best,
                    high=h, low=low, close=c, ref_px=c, armed=pos.armed,
                    budget_override=budget_override)

                if ld.action == "off":
                    # 参数域不成立(目标落在 entry 反侧/行程退化):不锁利、
                    # 不减仓,保持结构防线。只记一次,避免逐 bar 刷台账。
                    if not pos.lock_off_logged:
                        pos.lock_off_logged = True
                        pos.log.append(
                            f"v21|opp_lock_off|{ld.reason}|"
                            f"entry={pos.entry:.8g} target={pos.target_1:.8g} "
                            f"frac={frac_cfg:+.2f} ⇒ 不锁利、不减仓,"
                            f"保持结构防线 invalid={pos.invalid:.8g}")
                elif ld.action == "exit":
                    pos.log.append(
                        f"v21|lock_giveback_exhausted|峰值={pos._best:.8g} "
                        f"回吐={ld.drawdown:.8g} ≥ 预算={ld.budget:.8g} "
                        f"(frac={abs(frac_cfg):.0%} "
                        f"span={abs(pos.target_1 - pos.entry):.8g}) "
                        f"ref={c:.8g} ⇒ 市价离场(不留永不触发的挂单)")
                    return [self._exit(
                        pos, ts, c,
                        f"v21|lock_giveback_exhausted|peak={pos._best:.8g} "
                        f"ref={c:.8g} budget={ld.budget:.8g} "
                        f"giveback={ld.drawdown:.8g} exit all")]
                else:
                    if ld.armed and not pos.armed:
                        pos.armed = True
                        pos.log.append(
                            f"v21|opp_lock_armed|"
                            f"{'收盘突破' if frac_cfg < 0 else '触及'}对侧缘 "
                            f"target={pos.target_1:.8g} "
                            f"预算={(1.0 - abs(frac_cfg)):.0%}·span "
                            f"⇒ 此后按峰值棘轮(可多次)")
                    if ld.action == "tighten":
                        old = pos.invalid
                        stop_px = float(ld.stop_px)
                        pos.invalid = (min(pos.invalid, stop_px)
                                       if pos.direction < 0
                                       else max(pos.invalid, stop_px))
                        if abs(pos.invalid - old) > 1e-12:
                            pos.log.append(
                                f"v21|opp_lock|峰值={pos._best:.8g} "
                                f"预算={ld.budget:.8g} 回吐={ld.drawdown:.8g} "
                                f"ref={c:.8g} "
                                f"invalid {old:.8g}->{pos.invalid:.8g}")

        # §8.3: confirmed counter-direction pivots only tighten the stop.
        if not skip_trail:
            self._trail(pos, new_pivots, atr)

        # 5. §8.4 加仓(v1 关闭;逻辑保留并独立测试)
        if (not price_only) and self.allow_add and not pos.added \
                and not pos.pending_add:
            for ev in new_events:
                if ev.id == TREND_RESUMED and ev.direction == pos.direction:
                    intent = self.try_add(pos, ev, c, add_frac=pos.size_frac,
                                          ts=ts, defer=defer_add)
                    if intent is not None:
                        intents.append(intent)

        # 6. §8.6 时间止损:N_time 根内 MFE < 0.5R(v6 臂 B 整条关闭)
        if self.time_stop and pos.symbol in self.positions \
                and pos.bars_held >= self.p.n_time \
                and pos.mfe_r < MFE_MIN_R:
            intents.append(self._exit(
                pos, ts, c,
                f"§8.6|time stop bars_held={pos.bars_held} >= "
                f"n_time={self.p.n_time} mfe={pos.mfe_r:.3f}R "
                f"< {MFE_MIN_R}R exit all"))
        return intents

    # -- 单条规则 ----------------------------------------------------------------

    def _exit(self, pos: Position, ts: object, ref: float,
              reason: str) -> OrderIntent:
        qty = pos.size_frac
        pos.size_frac = 0.0
        self.positions.pop(pos.symbol, None)
        self.last.append(reason)
        return OrderIntent(kind=OrderKind.EXIT, symbol=pos.symbol,
                           qty_frac=qty, reason=reason, ref_price=float(ref),
                           ts=ts)

    def _defer_reduce(self, pos: Position, ts: object, ref: float,
                      extreme: float) -> OrderIntent:
        """限价委托只锁定一次减仓意图,未成交前仓位和失效位保持真实。"""
        if pos.pending_reduce or pos.reduced:
            raise AssertionError("duplicate deferred reduce")
        pos.pending_reduce = True
        qty = pos.size_frac * 0.5
        reason = (f"§8.2|target_1 hit reduce 50% target={pos.target_1:.8g} "
                  f"extreme={extreme:.8g} awaiting fill before breakeven")
        self.last.append(reason)
        return OrderIntent(kind=OrderKind.REDUCE, symbol=pos.symbol,
                           qty_frac=qty, reason=reason, ref_price=float(ref), ts=ts)

    def confirm_reduce(self, symbol: str, actual_qty_frac: float) -> None:
        """经纪商实际成交后才减少仓位并收紧止损。数量以初始 OPEN 仓位为基准。"""
        pos = self.positions.get(symbol)
        if pos is None or not pos.pending_reduce:
            raise AssertionError(f"unexpected reduce fill without pending order: {symbol}")
        if not 0.0 < actual_qty_frac <= pos.size_frac:
            raise ValueError(f"invalid confirmed reduce fraction {actual_qty_frac}")
        pos.size_frac -= actual_qty_frac
        pos.pending_reduce = False
        pos.reduced = True
        old = pos.invalid
        if self.breakeven_on_reduce:
            pos.invalid = min(pos.invalid, pos.entry) if pos.direction < 0 \
                else max(pos.invalid, pos.entry)
        pos.log.append(f"§8.2|reduce FILLED qty_frac={actual_qty_frac:.8g} "
                       f"invalid {old:.8g}->{pos.invalid:.8g}")

    def cancel_reduce(self, symbol: str) -> None:
        """下单失败时撤销待成交标记;不凭未成交指令修改真实仓位。"""
        pos = self.positions.get(symbol)
        if pos is not None:
            pos.pending_reduce = False

    def confirm_add(self, symbol: str, actual_qty_frac: float,
                    fill_price: float) -> None:
        """加仓成交确认:金额和实际成交价来自 broker,而不是决策时 close。"""
        pos = self.positions.get(symbol)
        if pos is None or not pos.pending_add:
            raise AssertionError(f"unexpected add fill without pending order: {symbol}")
        if actual_qty_frac <= 0.0 or fill_price <= 0.0:
            raise ValueError("invalid add fill")
        new_size = pos.size_frac + actual_qty_frac
        pos.entry = ((pos.size_frac * pos.entry +
                      actual_qty_frac * fill_price) / new_size)
        pos.size_frac = new_size
        proposed = pos.pending_add_invalid
        if proposed is not None:
            pos.invalid = (min(pos.invalid, proposed) if pos.direction < 0
                           else max(pos.invalid, proposed))
        pos.added = True
        pos.pending_add = False
        pos.pending_add_invalid = None
        pos.log.append(
            f"§8.4|ADD FILLED qty_frac={actual_qty_frac:.8g} "
            f"actual_price={fill_price:.8g} avg={pos.entry:.8g}")

    def cancel_add(self, symbol: str) -> None:
        """拒单/撤单不得创造一个已经加仓的持仓。"""
        pos = self.positions.get(symbol)
        if pos is not None:
            pos.pending_add = False
            pos.pending_add_invalid = None

    def _reduce(self, pos: Position, ts: object, ref: float,
                extreme: float) -> OrderIntent:
        qty = pos.size_frac * 0.5
        pos.size_frac -= qty
        pos.reduced = True
        old = pos.invalid
        # 保本(§8.2 后半):invalid 移至 entry;只收紧不放松(已 trailing 更优则不动)。
        # v6 臂 A 禁令:breakeven_on_reduce=False ⇒ 保本移动整条关闭。
        if self.breakeven_on_reduce:
            if pos.direction < 0:
                pos.invalid = min(pos.invalid, pos.entry)
            else:
                pos.invalid = max(pos.invalid, pos.entry)
        reason = (f"§8.2|target_1 hit reduce 50% target={pos.target_1:.8g} "
                  f"extreme={extreme:.8g} breakeven invalid {old:.8g}"
                  f"->{pos.invalid:.8g}")
        self.last.append(reason)
        return OrderIntent(kind=OrderKind.REDUCE, symbol=pos.symbol,
                           qty_frac=qty, reason=reason,
                           ref_price=float(ref), ts=ts)

    def _trail(self, pos: Position, new_pivots: Sequence[Pivot],
               atr: float) -> None:
        """§8.3:逆向 pivot(空=pivot high / 多=pivot low)外侧 ±k_bm·atr。"""
        if atr <= 0.0:
            return
        buf = self.p.k_bm * atr
        for pv in new_pivots:
            if pos.direction < 0 and pv.kind is PivotKind.HIGH:
                cand = pv.price + buf
                if cand < pos.invalid:                 # 只收紧
                    pos.log.append(
                        f"§8.3|trail new pivot high={pv.price:.8g} "
                        f"invalid {pos.invalid:.8g}->{cand:.8g}")
                    pos.invalid = cand
            elif pos.direction > 0 and pv.kind is PivotKind.LOW:
                cand = pv.price - buf
                if cand > pos.invalid:
                    pos.log.append(
                        f"§8.3|trail new pivot low={pv.price:.8g} "
                        f"invalid {pos.invalid:.8g}->{cand:.8g}")
                    pos.invalid = cand

    def try_add(self, pos: Position, event: MarketEvent, price: float,
                add_frac: float, ts: object = None,
                defer: bool = False) -> Optional[OrderIntent]:
        """§8.4 风险中性加仓(v1 由 allow_add 门控)。

        条件:同向 TREND_RESUMED、浮盈 ≥ 1R、加仓仅一次。合并口径:合并仓位的失效价取
        min/max(现 invalid, TREND_RESUMED 建议 invalid)(只收紧),合并均价按名义加权;
        风险中性约束 = 合并后最坏情景损失 ≤ 加仓前,不满足则拒绝(记录理由)。
        """
        if (event.id != TREND_RESUMED or event.direction != pos.direction or
                pos.added or pos.pending_add):
            return None
        if pos.mfe_r < ADD_MIN_R:
            self.last.append(
                f"§8.4|add rejected: mfe={pos.mfe_r:.3f}R < {ADD_MIN_R}R")
            return None
        d = pos.direction
        size = pos.size_frac
        cand = event.attrs.get("invalid")
        new_inv = pos.invalid
        if cand is not None:
            new_inv = min(pos.invalid, float(cand)) if d < 0 \
                else max(pos.invalid, float(cand))
        merged_size = size + add_frac
        avg = (size * pos.entry + add_frac * price) / merged_size
        worst_before = d * (pos.entry - pos.invalid) * size
        worst_after = d * (avg - new_inv) * merged_size
        if worst_after > worst_before + 1e-9:
            self.last.append(
                f"§8.4|add rejected: risk-neutral violated worst "
                f"{worst_before:.8g}->{worst_after:.8g}")
            return None
        if defer:
            pos.pending_add = True
            pos.pending_add_invalid = new_inv
        else:
            pos.entry = avg
            pos.size_frac = merged_size
            pos.invalid = new_inv
            pos.added = True
        reason = (f"§8.4|TREND_RESUMED add risk-neutral avg_entry={avg:.8g} "
                  f"invalid={new_inv:.8g} worst {worst_before:.8g}"
                  f"->{worst_after:.8g}")
        self.last.append(reason)
        return OrderIntent(kind=OrderKind.ADD, symbol=pos.symbol,
                           qty_frac=add_frac, reason=reason,
                           ref_price=float(price), ts=ts)
