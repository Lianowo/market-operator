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

v16 增量(docs/v16-protocol.md,收割婚配;加性开关,默认参数行为逐位不变):
- harvest_family(缺省 None ⇒ 关):开仓 event_id 命中 fade 家族(BREAKOUT_FAILED/LEVEL_REJECTED/EDGE_RECLAIMED)
  的持仓进入收割模式 —— **到达对面结构缘(target_1)⇒ 全部落袋**(结构
  目标收割,EXIT 全平,不走 §8.2 50% 减仓),**收割模式禁用 §8.3 trailing**
  (协议持仓第 2/4 条:fade 走 target-harvest,trail 只对续涨保留);续涨
  (及任何非家族事件)行为逐位同 canonical。
- time_cap_bars(= k_cap_hours,缺省 None ⇒ 关):时间帽 = k_cap×TF 小时,
  实现为 bars_held ≥ k_cap 根认知 bar(1h→48h、12h→24d,同协议);**到期且
  MFE < cap_mfe_min(0.5R)⇒ 出;MFE ≥ cap_mfe_min 不放逐**(恰界 0.5R
  inclusive 持有;到期后逐收盘 bar 复核,同 §8.6 的 bars_held >= idiom,
  如实声明)。
- v16 模式下出场 reason 带机器可读桶标签前缀 "v16|<bucket>|*(target /
  choch / trail / invalid / time_cap;trail = 防线已被 §8.3 收紧后的破位
  出场,invalid = 防线未动的原始结构失效);标签只在前置开关开启时出现,
  canonical(reason 逐位旧文本)不受影响。

v18 增量(docs/v18-protocol.md §R4,冻结 2026-10-20;加性开关,默认参数
行为逐位不变):
- harvest_ladder3(缺省 False ⇒ 关):fade 收割模式(harvest)持仓的收割
  改为**三段**:缘前 25%/50% 各落袋半仓的 1/3(=全仓 1/6,REDUCE 市价
  意图,次一执行细 bar 开盘成交,摊薄均价入账由 broker/reconstruct 同
  既有口径),到缘 EXIT 收净剩余(**收割目标缘的口径不变**);不理睬任何
  中间 trail 与保本(invalid 不动 —— 收割模式本来就禁 §8.3 trail;R4 的
  减仓也不触发 §8.2 保本移动)。段序:25% 先于 50% 先于缘(错峰语义);
  同一 bar 同时触及多段 ⇒ 段序内同批成组(REDUCE×N → EXIT);
  bar 极值口径(h/l 恰触 inclusive,与 v16 收割 hit 口径一致);收割序
  的落袋段带机器可读桶标签 "v16|target_partial|",缘收标签
  "v16|target|R4|"(出场桶沿用 v16 target)。
- 目标方向不齐(d·(target−entry) ≤ 0,退化)⇒ 退回 v16 单段收割(如实
  声明);无收割目标(target_1=None)照旧无收割结构行(与 v16 相同)。

v19 增量(docs/v19-protocol.md §3 夭折处置,冻结 2026-10-20;加性开关
early_exit=False ⇒ 逐位旧行为,parity 钉死):
- **夭折处置(独立组件)**:持仓期间两条离场规则,与 §8 六条共存、先触发
  先算(结构性出场 §8.1/§8.5/收割目标/§8.2/§8.3 先行裁决,其后评估;
  两条规则内半窗先于逆根):
  ① 半窗:bars_held ≥ k_cap/2(=time_cap_bars//2,24 根认知 bar)且
  MFE < early_half_mfe_r(0.3R)⇒ 出全仓(MFE 恰 0.3R inclusive 持有;
  过窗后逐收盘 bar 复核,同时间帽 idiom);桶标签 "v16|early_half_mfe|";
  ② 连续 ≥early_consec_n(3)根收盘全逆 ⇒ 出全仓:逆根 = d·(c−o) < 0
  (收盘认认知帧 —— 协议"(认微帧)"按 §8 裁决粒度落在认知帧解读,微帧
  粒度离场不在 §8 既有机制,如实声明),恰 0 不计逆(=0 收平复位);
  桶标签 "v16|early_consec|"。
- 全家族持仓同受(含 fade 收割模式);early_exit 要求 time_cap_bars(k_cap)
  在场(半窗基数),缺场 raise。

v20 增量(v20 收尾 S1/S2,冻结 2026-10-20;加性开关,默认参数行为逐位
不变,parity 钉死):
- **S1 结构脉冲激活 trail**(trail_arm_gate=False ⇒ 逐位旧行为):扶手对
  象 = cont 家族(BREAKOUT_ACCEPTED/STRUCTURE_BROKEN/FLIP_RETESTED)与 TREND_RESUMED(trail_arm_family);该族持仓的 §8.3
  trail 扶手只在 (a) MFE ≥ s1_mfe_r(1R,恰界 inclusive 上线)或 (b) 本
  bar 出现新确认**顺向** pivot(多=pivot high 新高 / 空=pivot low 新
  低 —— 脉冲向结构确认,与 §8.3 收紧消费的**逆向** pivot 互补;若与
  §8.3 同种则条件 (b) 恒与基线同步,闸退化为 no-op,实证钉死)之后才
  上线;未上线整段不裁决(扶手不收紧,破位 §8.1 / CHoCH §8.5 / 到目
  标收割照先行不受限);一旦上线恢复只紧不松(§8.3 原文义,arm 一经
  True 不回落,上线 bar 后首个逆向 pivot 起收紧,如实声明)。桶标签:
  §8.1 出场对扶手族改贴 "v16|trail_armed|"(已上线)/ "v16|
  trail_pending|"(未上线),替换 trail/invalid 旧贴(非扶手族与 fade
  收割模式照旧);S1 只做扶手闸,不改 §8.1 谓词本身。
- **S2 fade 双格收割**(harvest_fade_2leg=False ⇒ 逐位 v16 收割行为):
  fade 收割模式持仓的收割改**两格**:缘前 s2_level(25%,entry→target
  行程)落袋 s2_bank_frac(40%)仓(以 OPEN 为 1.0,REDUCE 市价意图,次
  一执行细 bar 开盘成交),到缘(EXIT)收净剩 60%(收割目标缘口径不
  变);不是 R4 三段(层级兑现不回锅)。段序:25% 先于缘;同一 bar 同触
  ⇒ 段序内同批(REDUCE → EXIT);bar 极值口径(h/l 恰触 inclusive,同
  v16 收割 hit 口径);不睬中间 trail 与保本(invalid 不动,同收割模式铁
  律);部分段桶标签 "v16|target_partial|S2|",缘收标签 "v16|target|S2|"
  (出场桶沿用 v16 target)。目标方向不齐(d·(target−entry) ≤ 0)⇒ 退
  回 v16 单段收割(同 R4 纪律);harvest_fade_2leg 与 harvest_ladder3 互
  斥(同栈 raise ValueError;R4 死亡名单不接线,双栏只在 v20 S2 臂接)。
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
# v18 R4 三段收割(协议 §R4 写死;恰界 inclusive,config/v18.json 硬对账)
R4_LEVEL_1 = 0.25        # 缘前 25%(entry→target 行程)
R4_LEVEL_2 = 0.50        # 缘前 50%
R4_PARTIAL_FRAC = 1.0 / 6.0   # 各段落袋半仓的 1/3 = 全仓 1/6(以 OPEN 为 1.0)
# v19 夭折处置(协议 §3 写死;恰界 inclusive 持有,config/v19.json 硬对账)
EARLY_HALF_MFE_R = 0.3   # 半窗(k_cap/2 后)MFE < 0.3R ⇒ 出
EARLY_CONSEC_N = 3       # 连续 ≥3 根收盘全逆(认知帧,d·(c−o)<0)⇒ 出
# v20 收尾 S1/S2(任务书写死;恰界 inclusive 触/上线,config/v20.json 硬对账)
S1_TRAIL_ARM_MFE_R = 1.0          # S1:trail 上线 MFE 闸(恰 1R 上线)
S1_FAMILY = ("BREAKOUT_ACCEPTED", "STRUCTURE_BROKEN", "FLIP_RETESTED", "TREND_RESUMED")   # S1 扶手对象:cont 家族 + TREND_RESUMED
S2_LEVEL = 0.25                   # S2:缘前 25%(entry→target 行程,恰触 inclusive)
S2_BANK_FRAC = 0.4                # S2:缘前 25% 落袋 40% 仓(缘收剩 60%)


# ---------------------------------------------------------------------------
# v21 锁利口径(唯一出口;position.py 与 live_runner.py 共用)
# ---------------------------------------------------------------------------


def opp_lock_px(entry: float, target: float, frac: float) -> Optional[float]:
    """REJECT 对侧锁利价(**计划口径**):沿 entry→target 行程推进 |frac|。

    `entry→target_1` 的行程才是"这段交易打算赚多少"的口径;协议 §5.2 的
    `reject_opp_lock_frac` 就是它的比例 ⇒ 锁利价 = entry + |frac|·(target−entry)。
    frac=0.5 即"锁住计划行程的一半",与箱体跨度无关(箱体跨度口径在 entry
    深入箱体时会把锁利价算到建仓价的盈利侧)。
    frac<=0 / span 退化 ⇒ None(调用方不得推进防线)。
    """
    entry_px, target_px = float(entry), float(target)
    frac_abs = abs(float(frac))
    span = target_px - entry_px
    if frac_abs <= 0.0 or abs(span) < 1e-12:
        return None
    return entry_px + frac_abs * span


def stop_protect_dist(direction: int, stop_px: float, ref_px: float) -> float:
    """止损位相对现价的有向距离:**> 0 才是有效保护单**。

    多头(d=+1)止损必须低于现价 ⇒ ref−stop;空头(d=−1)止损必须高于现价
    ⇒ stop−ref。统一写成 −d·(stop−ref),两个方向共用一个式子,避免
    "某一侧写反"这类不可测的灾难。
    """
    return -float(direction) * (float(stop_px) - float(ref_px))


def lock_is_protective(direction: int, lock_px: Optional[float],
                       ref_px: float, min_buf: float = 0.0) -> bool:
    """锁利/推进价落位闸门:必须在现价保护侧且留 min_buf 缓冲。

    ref_px 取"当下可见的参考价"(逐 bar 口径用收盘价 c;实盘换单前可用
    mark/最新价复核)。闸门不通过 ⇒ 调用方**不得**推进防线(保留原结构
    止损),宁可不锁利也不能把仓位唯一的一张保护单挂到错误一侧。
    """
    if lock_px is None:
        return False
    return stop_protect_dist(direction, float(lock_px), ref_px) > \
        float(min_buf) + 1e-12


def opp_lock_live_px(direction: int, ref_px: float, giveback_r: float,
                     risk_px: float) -> Optional[float]:
    """**行情口径**锁利价:锚在现价,只允许回吐 `giveback_r` 个 R。

    空头 ref + giveback_r·R / 多头 ref − giveback_r·R(单位与 R 同尺度,
    2h/12h/不同币种自动同权,不依赖 ATR 管线)。

    存在的理由:锁利裁决只发生在**收盘**(12h 认知帧或 2h 子帧),而那一刻
    价格往往已经走过了 `opp_lock_px` 按 entry→target 行程算出的位置。
    2026-10-23 GOOGLUSDT 即样本:计划口径锁利 323.34,而 2h 收盘 321.33
    ⇒ 锁利价落在现价**身后 2.01(0.63%)**:行情不回踩,这张止损等价于
    不存在(什么也没锁住);回踩触发时,已把 1.08R 的行程回吐到 0.24R
    (实际成交 324.01)。所以锁利必须以**当下可见价**为锚。
    `giveback_r<=0` / risk<=0 ⇒ None(关闭,退回纯计划口径 = 历史 parity)。
    """
    g, risk = float(giveback_r), float(risk_px)
    if g <= 0.0 or risk <= 0.0 or float(ref_px) <= 0.0:
        return None
    return float(ref_px) - float(direction) * g * risk


def tighten_lock(direction: int, plan_px: Optional[float],
                 live_px: Optional[float]) -> Optional[float]:
    """合并两种口径:**取更贴近现价的一侧**(空头取更小 / 多头取更大)。

    `plan_px`(协议:至少锁到 entry→target 行程的 frac)与 `live_px`
    (行情:不许回吐超过给予的缓冲)取紧者,保证锁利价既是有效的
    (不会落到现价身后)、又不会比协议要求更松。
    """
    if plan_px is None:
        return live_px
    if live_px is None:
        return plan_px
    return (min(float(plan_px), float(live_px)) if direction < 0
            else max(float(plan_px), float(live_px)))


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
    harvest: bool = False          # v16:收割模式(fade 家族;target 全平+禁 trail)
    invalid0: Optional[float] = None  # v16:开仓时原始结构失效位(trail/invalid 分桶)
    r4_stage: int = 0              # v18 R4:已完成落袋段数(0/1/2;错峰段序)
    consec_adverse: int = 0        # v19:连续逆根计数(d·(c−o)<0;恰 0 复位)
    trail_armed: bool = False      # v20 S1:trail 扶手已上线(一经 True 不回落)
    s2_banked: bool = False        # v20 S2:缘前 25% 段已落袋

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
                 harvest_family: Optional[tuple] = None,
                 time_cap_bars: Optional[int] = None,
                 cap_mfe_min: float = MFE_MIN_R,
                 harvest_ladder3: bool = False,
                 early_exit: bool = False,
                 early_half_mfe_r: float = EARLY_HALF_MFE_R,
                 early_consec_n: int = EARLY_CONSEC_N,
                 trail_arm_gate: bool = False,
                 trail_arm_family: tuple = S1_FAMILY,
                 trail_arm_mfe_r: float = S1_TRAIL_ARM_MFE_R,
                 harvest_fade_2leg: bool = False,
                 s2_level: float = S2_LEVEL,
                 s2_bank_frac: float = S2_BANK_FRAC,
                 reject_opp_lock_frac: float = 0.0,
                 opp_lock_max_giveback_r: float = 0.0) -> None:
        self.p = params or Params()
        self.allow_add = allow_add
        self.breakeven_on_reduce = breakeven_on_reduce  # v6 臂 A:False=禁保本
        self.time_stop = time_stop                      # v6 臂 B:False=关 §8.6
        # v16 收割婚配开关(全 None ⇒ 逐位 canonical 旧行为,回归钉死)
        self.harvest_family = harvest_family            # fade 家族(BREAKOUT_FAILED/LEVEL_REJECTED/EDGE_RECLAIMED)
        self.time_cap_bars = time_cap_bars              # k_cap_hours(48)
        self.cap_mfe_min = float(cap_mfe_min)           # MFE 恰界 inclusive 持有
        # v18 R4 三段收割开关(False ⇒ 逐位 v16 收割行为,回归钉死)
        self.harvest_ladder3 = bool(harvest_ladder3)
        # v19 夭折处置开关(False ⇒ 逐位旧行为,回归钉死;要求 k_cap 在场)
        self.early_exit = bool(early_exit)
        self.early_half_mfe_r = float(early_half_mfe_r)
        self.early_consec_n = int(early_consec_n)
        if self.early_exit and time_cap_bars is None:
            raise ValueError("v19 夭折处置要求 time_cap_bars(k_cap)在场"
                             "(半窗基数;协议 §3)")
        self._early_half_bars = (int(time_cap_bars) // 2
                                 if time_cap_bars is not None else None)
        # v20 S1 扶手闸 / S2 双格收割(全默认关 ⇒ 逐位旧行为,parity 钉死)
        self.trail_arm_gate = bool(trail_arm_gate)
        self.trail_arm_family = tuple(trail_arm_family)
        self.trail_arm_mfe_r = float(trail_arm_mfe_r)
        self.harvest_fade_2leg = bool(harvest_fade_2leg)
        self.s2_level = float(s2_level)
        self.s2_bank_frac = float(s2_bank_frac)
        self.reject_opp_lock_frac = float(reject_opp_lock_frac)
        # 锁利"行情口径"缓冲:锁利价与决策时现价的最大距离(以 R 为单位)。
        # 0 = 关闭(纯计划口径 = 历史 parity);>0 时取计划/行情两口径中更紧者。
        self.opp_lock_max_giveback_r = max(0.0, float(opp_lock_max_giveback_r))
        if self.harvest_fade_2leg and self.harvest_ladder3:
            raise ValueError("v20 S2 双格收割与 v18 R4 三段收割互斥"
                             "(R4 死亡名单不接线;同栈即语义冲突)")
        self.positions: dict[str, Position] = {}
        self.last: list[str] = []            # 最近 on_bar 判定记录(解释性)

    @property
    def _v16(self) -> bool:
        """v16 收割婚配任一开关在场(决定出场 reason 是否带桶标签前缀)。"""
        return self.harvest_family is not None or self.time_cap_bars is not None

    # -- 建仓 ------------------------------------------------------------------

    def open(self, symbol: str, direction: int, entry: float, invalid: float,
             target_1: Optional[float], event_id: str, ts: object,
             index: int) -> Position:
        risk = direction * (entry - invalid)
        if risk <= 0.0:
            raise ValueError(f"invalid not protective: entry={entry} "
                             f"invalid={invalid} direction={direction}")
        pos = Position(symbol=symbol, direction=direction, entry=float(entry),
                       invalid=float(invalid), target_1=target_1,
                       event_id=event_id, opened_ts=ts, opened_index=index,
                       initial_risk=float(risk))
        # v16:fade 家族持仓进入收割模式(达对面结构缘全平;trail 禁用)
        if self.harvest_family is not None and str(event_id) in self.harvest_family:
            pos.harvest = True
        self.positions[symbol] = pos
        return pos

    def open_from_hypothesis(self, symbol: str, hyp: Hypothesis, entry: float,
                             ts: object, index: int) -> Position:
        """由 §7 假设建仓(entry 为下一 bar 开盘成交价)。"""
        return self.open(symbol, hyp.direction, entry, hyp.invalid,
                         hyp.target_1, hyp.event_id, ts, index)

    # -- 逐 bar 裁决 -----------------------------------------------------------

    def on_bar(self, symbol: str, bar, atr: float,
               new_pivots: Sequence[Pivot] = (),
               new_events: Sequence[MarketEvent] = (),
               defer_reduce: bool = False,
               defer_add: bool = False) -> list[OrderIntent]:
        """按优先级 §8.1>§8.5>§8.2>§8.3>§8.6 评估;返回订单意图列表。"""
        pos = self.positions.get(symbol)
        if pos is None:
            return []
        self.last = []
        ts, o, h, l, c, _v = _bar_parts(bar)
        pos.bars_held += 1
        pos._best = min(pos._best, l) if pos.direction < 0 else max(pos._best, h)
        if self.early_exit:
            # v19 逆根计数:收盘全逆(d·(c−o)<0)+1,否则复位(恰 0 收平非逆)
            if pos.direction * (float(c) - float(o)) < 0.0:
                pos.consec_adverse += 1
            else:
                pos.consec_adverse = 0

        # 1. §8.1 破位退出(结构失效,收盘口径;v16:防线已收紧 ⇒ trail 桶,
        #    防线未动 ⇒ invalid 原始结构失效桶;v20 S1:扶手族改贴
        #    trail_armed/trail_pending —— 破位判定本身不受 S1 限)
        if (c > pos.invalid) if pos.direction < 0 else (c < pos.invalid):
            reason = (f"§8.1|invalidation close close={c:.8g} "
                      f"invalid={pos.invalid:.8g} exit all")
            if self._v16:
                tag = "trail" if pos.invalid != pos.invalid0 else "invalid"
                if self._s1_gated(pos):
                    tag = "trail_armed" if pos.trail_armed else "trail_pending"
                reason = f"v16|{tag}|" + reason
            return [self._exit(pos, ts, c, reason)]

        # 2. §8.5 反向 STRUCTURE_BROKEN(CHoCH 出场)
        for ev in new_events:
            if ev.id == STRUCTURE_BROKEN and ev.direction == -pos.direction:
                reason = (f"§8.5|STRUCTURE_BROKEN reverse CHoCH event_ts={ev.ts} "
                          f"exit all")
                if self._v16:
                    reason = "v16|choch|" + reason
                return [self._exit(pos, ts, c, reason)]

        # 2.5 v16 收割婚配:fade 家族达对面结构缘 ⇒ 全部落袋(结构目标收割,
        #    EXIT 全平,不走 §8.2 50% 减仓;协议持仓第 2 条;不加仓)
        #    v18 R4(harvest_ladder3):改三段收割(缘前 25%/50% 各 1/6 仓,
        #    缘收净;目标方向不齐 ⇒ 退回 v16 单段收割)
        ladder_intents: list[OrderIntent] = []
        if pos.harvest and pos.target_1 is not None:
            delta = float(pos.target_1) - float(pos.entry)
            # v20 S2 双格收割(缘前 25% 落袋 40%,缘收剩 60%;与 R4 互斥)
            if self.harvest_fade_2leg and pos.direction * delta > 0.0:
                ladder_intents = self._s2_ladder(pos, ts, c, h, l)
                if ladder_intents and ladder_intents[-1].kind is OrderKind.EXIT:
                    return ladder_intents
            elif self.harvest_ladder3 and pos.direction * delta > 0.0:
                ladder_intents = self._r4_ladder(pos, ts, c, h, l)
                if ladder_intents and ladder_intents[-1].kind is OrderKind.EXIT:
                    return ladder_intents
            else:
                hit = (l <= pos.target_1) if pos.direction < 0 \
                    else (h >= pos.target_1)
                if hit:
                    extreme = l if pos.direction < 0 else h
                    return [self._exit(
                        pos, ts, c,
                        f"v16|target|对面结构缘收割 target={pos.target_1:.8g} "
                        f"extreme={extreme:.8g} exit all(结构目标全平,禁 trail)")]

        intents: list[OrderIntent] = list(ladder_intents)

        # 3. §8.2 target_1 触及:
        #    - 若启用了 reject_opp_lock_frac < 0:仅当收盘真正突破对侧(c 越过 target_1)
        #      才将防线收紧锁利(|reject_opp_lock_frac| * span);触及对侧但未突破前完全
        #      保持原结构防线 + Turtle,绝不因对侧震荡提前扫保本;
        #    - 若启用了 reject_opp_lock_frac > 0:触及对侧即锁利推进防线;
        #    - 否则按传统 §8.2 减仓 50%(仅一次),余仓 invalid 移至 entry。
        #    v21 锁利口径(两条锁利分支共用,详见模块顶部 helper):
        #      lock = tighten_lock(计划口径, 行情口径) —— 计划口径锁 entry→target
        #      行程的 frac,行情口径只允许回吐 opp_lock_max_giveback_r 个 R;
        #      二者取更贴近现价者,再由 lock_is_protective 闸门保证落在现价
        #      保护侧(否则不推进、不置 reduced)。
        if pos.target_1 is not None and not pos.reduced and not pos.pending_reduce:
            if self.reject_opp_lock_frac < 0.0:
                broke = (c < pos.target_1) if pos.direction < 0 else (c > pos.target_1)
                if broke:
                    frac = abs(self.reject_opp_lock_frac)
                    plan_px = opp_lock_px(pos.entry, pos.target_1, frac)
                    live_px = opp_lock_live_px(pos.direction, c,
                                               self.opp_lock_max_giveback_r,
                                               pos.initial_risk)
                    lock_px = tighten_lock(pos.direction, plan_px, live_px)
                    if not lock_is_protective(pos.direction, lock_px, c):
                        # 落位闸门(1):算出的锁利价已落到现价盈利侧/身后
                        # (收盘虽破对侧,行情早已走远或已回抽越过锁利价)。
                        # 推进它等于把唯一保护单变成"止盈单",交易所立即触发
                        # ⇒ 保持原结构防线,下一根 bar 再判(不置 reduced)。
                        pos.log.append(
                            f"v21|opp_break_lock_reject|收盘突破对侧缘 "
                            f"target={pos.target_1:.8g} 锁利({frac:.0%}) 算得 "
                            f"{fmt_px(lock_px)} 不在现价 {c:.8g} 保护侧 "
                            f"⇒ 保持原防线 invalid={pos.invalid:.8g}")
                    else:
                        pos.reduced = True
                        old = pos.invalid
                        pos.invalid = (min(pos.invalid, lock_px) if pos.direction < 0
                                       else max(pos.invalid, lock_px))
                        pos.log.append(
                            f"v21|opp_break_lock|收盘突破对侧缘 target={pos.target_1:.8g} "
                            f"锁利({frac:.0%}) 计划={fmt_px(plan_px)} "
                            f"行情={fmt_px(live_px)} ref={c:.8g} "
                            f"invalid {old:.8g}->{pos.invalid:.8g}")
            else:
                hit = (l <= pos.target_1) if pos.direction < 0 \
                    else (h >= pos.target_1)
                if hit:
                    if self.reject_opp_lock_frac > 0.0:
                        broke_through = (c < pos.target_1) if pos.direction < 0 else (c > pos.target_1)
                        frac = self.reject_opp_lock_frac if broke_through else min(0.10, self.reject_opp_lock_frac)
                        plan_px = opp_lock_px(pos.entry, pos.target_1, frac)
                        live_px = opp_lock_live_px(pos.direction, c,
                                                   self.opp_lock_max_giveback_r,
                                                   pos.initial_risk)
                        lock_px = tighten_lock(pos.direction, plan_px, live_px)
                        if not lock_is_protective(pos.direction, lock_px, c):
                            # 落位闸门(2):仅影线触及分支最易踩到"锁利价回落
                            # 到现价盈利侧"—— 极值刺到目标后收盘已反抽回 near
                            # entry,把防线推到 frac(<10%) 位只是把止损变成贴着
                            # 建仓价的止盈单。闸门不通过 ⇒ 不推进、不减仓。
                            pos.log.append(
                                f"v21|opp_lock_reject|触及对侧缘 "
                                f"target={pos.target_1:.8g} 锁利({frac:.0%}) 算得 "
                                f"{fmt_px(lock_px)} 不在现价 {c:.8g} 保护侧 "
                                f"⇒ 保持原防线 invalid={pos.invalid:.8g}")
                        else:
                            pos.reduced = True
                            old = pos.invalid
                            pos.invalid = (min(pos.invalid, lock_px) if pos.direction < 0
                                           else max(pos.invalid, lock_px))
                            pos.log.append(
                                f"v21|opp_lock|触及对侧缘 target={pos.target_1:.8g} "
                                f"不减仓锁利({frac:.0%}) "
                                f"计划={fmt_px(plan_px)} 行情={fmt_px(live_px)} "
                                f"invalid {old:.8g}->{pos.invalid:.8g}")
                    elif defer_reduce:
                        intents.append(self._defer_reduce(pos, ts, c, l if pos.direction < 0 else h))
                    else:
                        intents.append(self._reduce(pos, ts, c, l if pos.direction < 0 else h))

        # 4. §8.3 移动防线:新确认逆向 pivot ⇒ invalid 收紧(永不放松);
        #    v16 fade 收割模式禁用 trail(协议持仓第 4 条:trail 只对续涨保留)
        #    v20 S1:扶手族(BREAKOUT_ACCEPTED/STRUCTURE_BROKEN/FLIP_RETESTED/TREND_RESUMED)trail 扶手只在 (a) MFE≥1R 或 (b) 本
        #    bar 新确认顺向 pivot(多=pivot high 新高 / 空=pivot low 新低 ——
        #    脉冲向结构确认,与 §8.3 收紧消费的逆向 pivot 互补)之后上线;未
        #    上线 §8.3 整段不裁决(扶手不收紧,破位/CHoCH/到目标照先行);
        #    一经上线恢复只紧不松
        # fade 收割模式禁用 trail;但无结构目标(target_1=None)时回退 trail,
        # 避免收割与 trailing 双失、只剩静态 2·ATR 防线(①fade 收割的护栏)
        if not pos.harvest or pos.target_1 is None:
            gated = self._s1_gated(pos)
            if gated and not pos.trail_armed:
                arm_mfe = pos.mfe_r >= self.trail_arm_mfe_r     # 恰 1R 上线
                arm_pv = any(
                    (pos.direction > 0 and pv.kind is PivotKind.HIGH)
                    or (pos.direction < 0 and pv.kind is PivotKind.LOW)
                    for pv in new_pivots)
                if arm_mfe or arm_pv:
                    pos.trail_armed = True
                    pos.log.append(
                        f"v20 S1|trail_armed mfe={pos.mfe_r:.4g}R "
                        f"(cond:{'mfe' if arm_mfe else 'pivot'};扶手上线,"
                        "一经上线只紧不松)")
            if not gated or pos.trail_armed:
                self._trail(pos, new_pivots, atr)

        # 5. §8.4 加仓(v1 关闭;逻辑保留并独立测试)
        if self.allow_add and not pos.added and not pos.pending_add:
            for ev in new_events:
                if ev.id == TREND_RESUMED and ev.direction == pos.direction:
                    intent = self.try_add(pos, ev, c, add_frac=pos.size_frac,
                                          ts=ts, defer=defer_add)
                    if intent is not None:
                        intents.append(intent)

        # 6.5 v19 夭折处置(docs/v19-protocol.md §3;与 §8 共存、先触发先算 ——
        #     结构性出场(§8.1/§8.5/收割目标/§8.2)与 trail 已先行裁决,其后
        #     半窗先于逆根;early_exit 关 ⇒ 本块整段不生效,逐位旧行为)
        if self.early_exit and pos.symbol in self.positions:
            if pos.bars_held >= self._early_half_bars \
                    and pos.mfe_r < self.early_half_mfe_r:
                intents.append(self._exit(
                    pos, ts, c,
                    f"v16|early_half_mfe|夭折处置 半窗 bars_held="
                    f"{pos.bars_held} >= {self.time_cap_bars}/2 "
                    f"(={self._early_half_bars}) mfe={pos.mfe_r:.3f}R "
                    f"< {self.early_half_mfe_r}R exit all(恰 "
                    f"{self.early_half_mfe_r}R inclusive 持有,过窗逐 bar 复核)"))
            elif pos.consec_adverse >= self.early_consec_n:
                intents.append(self._exit(
                    pos, ts, c,
                    f"v16|early_consec|夭折处置 连续 {pos.consec_adverse} 根"
                    f"收盘全逆 >= {self.early_consec_n}(d·(c−o)<0 计根,"
                    f"恰 0 复位)exit all(先触发先算:半窗先于逆根)"))
        # 6. §8.6 时间止损:N_time 根内 MFE < 0.5R(v6 臂 B 整条关闭)
        #     (v19:夭折处置先行出场后本仓已弹簿,加在簿护栏;早期块不触发时
        #     与旧谓词恒等,canonical 逐位不变)
        if self.time_stop and pos.symbol in self.positions \
                and pos.bars_held >= self.p.n_time \
                and pos.mfe_r < MFE_MIN_R:
            intents.append(self._exit(
                pos, ts, c,
                f"§8.6|time stop bars_held={pos.bars_held} >= "
                f"n_time={self.p.n_time} mfe={pos.mfe_r:.3f}R "
                f"< {MFE_MIN_R}R exit all"))
        # 7. v16 时间帽:k_cap_hours×TF 认知 bar;到期且 MFE < cap_mfe_min ⇒ 出,
        #    MFE ≥ cap_mfe_min(恰界 inclusive)不放逐,到期后逐收盘 bar 复核
        if self.time_cap_bars is not None and pos.symbol in self.positions \
                and pos.bars_held >= self.time_cap_bars \
                and pos.mfe_r < self.cap_mfe_min:
            intents.append(self._exit(
                pos, ts, c,
                f"v16|time_cap|时间帽 bars_held={pos.bars_held} >= "
                f"k_cap_hours={self.time_cap_bars} mfe={pos.mfe_r:.3f}R "
                f"< {self.cap_mfe_min}R exit all(MFE ≥ "
                f"{self.cap_mfe_min}R 不放逐)"))
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

    def _r4_ladder(self, pos: Position, ts: object, c: float, h: float,
                   l: float) -> list[OrderIntent]:
        """v18 R4 三段收割(协议 §R4;调用前提:harvest ∧ target 方向一致)。

        缘前 25%/50% 各落袋 1/6 仓(以 OPEN 为 1.0;错段只判未做段,同一
        根 bar 两段同触 ⇒ 段序内同批 REDUCE×2),到缘 EXIT 收净剩余;不睬
        中间 trail/保本(invalid 不动,不调 _reduce/§8.2 路径)。返回意图
        列表(可能空);若末位为 EXIT ⇒ 持仓已清(调用方直接 return)。
        """
        d = pos.direction
        delta = float(pos.target_1) - float(pos.entry)
        out: list[OrderIntent] = []
        for stage, frac_lvl in ((1, R4_LEVEL_1), (2, R4_LEVEL_2)):
            if pos.r4_stage >= stage:
                continue
            lvl = float(pos.entry) + frac_lvl * delta
            touched = (l <= lvl) if d < 0 else (h >= lvl)
            if not touched:
                continue
            pos.r4_stage = stage
            qty = min(R4_PARTIAL_FRAC, pos.size_frac)
            pos.size_frac -= qty
            reason = (f"v16|target_partial|R4|段{stage}(缘前 "
                      f"{int(round(frac_lvl * 100))}%)落袋 1/6 仓 "
                      f"level={lvl:.8g} target={pos.target_1:.8g} "
                      f"size_frac→{pos.size_frac:.8f}(错峰段序,无 trail "
                      f"无保本)")
            self.last.append(reason)
            out.append(OrderIntent(kind=OrderKind.REDUCE, symbol=pos.symbol,
                                   qty_frac=qty, reason=reason,
                                   ref_price=float(c), ts=ts))
        hit = (l <= pos.target_1) if d < 0 else (h >= pos.target_1)
        if hit:
            extreme = l if d < 0 else h
            out.append(self._exit(
                pos, ts, c,
                f"v16|target|R4|缘起收净 target={pos.target_1:.8g} "
                f"extreme={extreme:.8g} exit 剩余全部(三段收割:缘口径不"
                f"变,已落袋至段 {pos.r4_stage})"))
        return out

    def _s1_gated(self, pos: Position) -> bool:
        """v20 S1 扶手对象判定:扶手闸开 ∧ 持仓事件 ∈ 扶手族(cont+TREND_RESUMED)。"""
        return self.trail_arm_gate and str(pos.event_id) in self.trail_arm_family

    def _s2_ladder(self, pos: Position, ts: object, c: float, h: float,
                   l: float) -> list[OrderIntent]:
        """v20 S2 双格收割(任务书 §S2;调用前提:harvest ∧ target 方向一致)。

        缘前 s2_level(25%)落袋 s2_bank_frac(40%)仓(以 OPEN 为 1.0;同
        一根 bar 段与缘同触 ⇒ 段序内同批 REDUCE → EXIT),到缘 EXIT 收净剩
        60%(缘口径不变);不睬中间 trail/保本(invalid 不动,不调 _reduce/
        §8.2 路径)。返回意图列表(可能空);若末位为 EXIT ⇒ 持仓已清(调
        用方直接 return)。
        """
        d = pos.direction
        delta = float(pos.target_1) - float(pos.entry)
        out: list[OrderIntent] = []
        if not pos.s2_banked:
            lvl = float(pos.entry) + self.s2_level * delta
            touched = (l <= lvl) if d < 0 else (h >= lvl)
            if touched:
                pos.s2_banked = True
                qty = min(self.s2_bank_frac, pos.size_frac)
                pos.size_frac -= qty
                reason = (f"v16|target_partial|S2|缘前 "
                          f"{int(round(self.s2_level * 100))}% 落袋 "
                          f"{self.s2_bank_frac:.0%} 仓 level={lvl:.8g} "
                          f"target={pos.target_1:.8g} "
                          f"size_frac→{pos.size_frac:.8f}(双格收割,"
                          f"无 trail 无保本)")
                self.last.append(reason)
                out.append(OrderIntent(kind=OrderKind.REDUCE,
                                       symbol=pos.symbol, qty_frac=qty,
                                       reason=reason, ref_price=float(c),
                                       ts=ts))
        hit = (l <= pos.target_1) if d < 0 else (h >= pos.target_1)
        if hit:
            extreme = l if d < 0 else h
            out.append(self._exit(
                pos, ts, c,
                f"v16|target|S2|缘收剩 {1.0 - self.s2_bank_frac:.0%} 仓 "
                f"target={pos.target_1:.8g} extreme={extreme:.8g} "
                f"exit 剩余全部(双格收割:缘口径不变,25% 段落袋="
                f"{'已' if pos.s2_banked else '未'})"))
        return out

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
