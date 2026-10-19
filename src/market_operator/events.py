"""市场行为事件 E1-E6(docs/methodology.md §6,精确定义 §6A 附录 A1-A9)。

检测器与 MarketStructure 同步逐 bar 喂数据(内部持有结构实例),所有事件在
`known_index = t` 的 bar 收盘判出,仅用 [0,t](§6 表头纪律)。

解释性决定(文字未完全展开处,取最符合因果语义的读法,标注条款):
- [A3] episode 以 zone 对象身份界定:zone 离开 ACTIVE(BROKEN/翻转/消亡)或事件
  发射即终结;每个 episode 至多产生 E1/E2/E3 之一。"zone 返回 ACTIVE(新一轮从正面
  接近)才开始新 episode"读作:事件发射后,只要价格仍在带内悬停(prev_close 未回到
  正面之外),不再开 episode;价格退回正面之外后重新(A2)触碰,即"新一轮从正面
  接近",开新 episode(可再次发射)——去重的语义单位是"接近过程",而非 zone 一生。
  §4.1 合并时,若存在同向 ACTIVE 新区间 ⊇ 旧区间,episode 续绑到该合并结果上
  (语义上同一区域的扩张,而非新接近);否则 episode 终结。E1 破位候选不依赖
  episode(A4 只要求收盘破位,首次触碰直接破位的路径同样发 E1)。
- [A4] 破位候选 bar 计为连续 N_a 根的第 1 根;之后任一收盘未收在 zone 外侧即作废。
- [A5] 刺穿 bar 计入窗口:E2 窗口为刺穿 bar 起共 N_f 根(offset ∈ [0, N_f-1]);
  "期间未发生 E1 接受":一旦出现 A4 破位收盘(候选成立)即终止 E2 检测——
  竞争以破位 bar 先后为准。
- [A6] E3 回退窗口 offset ∈ [0, N_f](含 ex 建立 bar 起的 N_f 根);同根 bar 上
  "ex 建立→回退"的先后按 §8.4 路径启发式:close < open(阻力侧)视为先见 high
  后见 low;反之本 bar 的 low 先于 high,只许对更早 bar 建立的 ex 判回退。
  同 bar 竞争优先级 E1 > E2 > E3(A6:"E1/E2 先发生则终止")。
- [§6 表/A1] 方向:阻力侧事件为空(-1),支撑侧为多(+1);E5 跟随其来源 E3;
  E6 为趋势破坏警告(TREND_UP 破坏 → -1,TREND_DOWN 破坏 → +1)。
- [§4.3] E2/E3(拒绝类)成立时给对应 zone `rejects` +1(事实记录)。
- [A7] E4 在回调终点 pivot(L_pb)确认 bar 激活(需该 bar regime 为趋势且深度、
  守 HL 条件成立),触发窗口为 (known, known+N_f];深度与守位以确认时刻 atr 度量。
- [A8] E5 在来源 E3 的同一根 bar 收盘发射;band 由最近至多 range_max_pivots 个
  confirmed pivots 合成;"对侧空间"以事件 bar 收盘至对侧边缘距离度量。
- [§5/A9] regime 谓词本身含"价格未破坏最近抬升低点"条款(以当前收盘判定,
  见 structure.py),故 E6 以 `regime(t-1)` 为"TREND_UP/…中"之判定,恰好发射于
  regime 退出趋势的那一根。E6 的 L*/H* 取最近 confirmed 低/高 pivot。
- [§7] `attrs["invalid"]` 为失效价建议:E1 取 zone 正缘外侧(lo - buf / hi + buf,
  buf = k_bm*atr);E2/E3 取外缘外侧;E4 取回调终点外侧。均仅建议,不撮合。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .structure import (
    MarketStructure,
    Params,
    PivotKind,
    Regime,
    Zone,
    ZoneRole,
    ZoneState,
)

E1 = "E1"  # BREAKOUT_ACCEPTANCE 突破接受
E2 = "E2"  # BREAKOUT_FAILURE 失败突破
E3 = "E3"  # REJECTION 关键位拒绝
E4 = "E4"  # TREND_RESUMPTION 趋势恢复
E5 = "E5"  # RANGE_ROTATION 区间回归
E6 = "E6"  # STRUCTURE_BREAK 结构破坏


@dataclass(frozen=True)
class MarketEvent:
    """§6 事件。`ts` 为知时 bar 的时间戳,`known_index` 为知时 bar 序号(A10)。"""

    id: str
    direction: int            # +1 多 / -1 空
    ts: object
    known_index: int
    attrs: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 内部追踪状态
# ---------------------------------------------------------------------------


@dataclass
class _Episode:
    """A3 approach episode:zone 自第一次(A2)触碰起被接近;至多产出一个事件。"""

    zone: Zone                # 活引用(与 ZoneBook 中的对象同一)
    opened: int
    ex: float                 # episode 内进入极值(A6);阻力=最高 high,支撑=最低 low
    ex_index: int
    puncture_index: Optional[int] = None  # A5 刺穿 bar


@dataclass
class _AcceptCandidate:
    """A4/E1 破位候选:zone 收盘破位起,要求连续 N_a 根收在 zone 外侧。"""

    zone: Zone
    lo: float                 # 快照边界(破位判定基准)
    hi: float
    role: ZoneRole
    count: int                # 已连续收在外侧的根数(候选 bar = 第 1 根)
    started: int


@dataclass
class _E4Candidate:
    """A7/E4:回调终点 pivot 确认后激活,等待收盘回收 50% 回调腿。"""

    direction: int            # +1: TREND_UP 恢复做多; -1: TREND_DOWN 恢复做空
    leg_start: float          # 回调腿起点(前高 pivot 价;镜像为前低)
    pb_price: float           # 回调终点 pivot 价(L_pb;镜像为 H_pb)
    depth: float              # D
    activated: int
    deadline: int             # activated + N_f


def _reject(eid: str, direction: int, z: Zone, t: int, ts: object, bm: float,
            extra: dict, edge: float) -> MarketEvent:
    """E2/E3(拒绝类)发射:先记 §4.3 事实(rejects+1、知时),再快照 zone 进 attrs。"""
    z.rejects += 1
    z.last_change_known = t
    attrs = {"zone": z.clone(), "ref": edge, "invalid": edge - direction * bm}
    attrs.update(extra)
    return MarketEvent(id=eid, direction=direction, ts=ts, known_index=t, attrs=attrs)


# ---------------------------------------------------------------------------
# EventDetector
# ---------------------------------------------------------------------------


class EventDetector:
    """与 MarketStructure 同步逐 bar 喂数据;`update(bar) -> list[MarketEvent]`。

    内部持有结构实例并随 update 推进;同一根 bar 的检测因果序:
    结构推进 → episode 开/关(A2/A3)→ 破位候选登记(A4)→ E1 推进(A4)
    → E2/A5、E3/A6 逐 episode 判 → E5 合成(A8)→ E4 激活/触发(A7)→ E6(A9)。
    """

    def __init__(self, params: Params) -> None:
        self.params = params
        self.structure = MarketStructure(params)
        self.events: list[MarketEvent] = []
        self._episodes: list[_Episode] = []
        self._candidates: list[_AcceptCandidate] = []
        self._e4: list[_E4Candidate] = []
        self._prev_regime: Regime = Regime.TRANSITION

    # -- 主入口 ---------------------------------------------------------------

    def update(self, bar) -> list[MarketEvent]:
        self.structure.update(bar)
        s = self.structure
        t = s.bars_seen - 1
        atr = s.atr()
        emitted: list[MarketEvent] = []
        if t > 0 and atr > 0.0:
            ts, o, h, l, c, _ = s._bars[t]
            prev_close = s._bars[t - 1][4]
            emitted.extend(self._zone_events(t, ts, o, h, l, c, prev_close, atr))
            emitted.extend(self._trend_resumption(t, ts, c, atr))
            emitted.extend(self._structure_break(t, ts, c, atr))
        self._prev_regime = s.regime()
        self.events.extend(emitted)
        return emitted

    # -- E1/E2/E3/E5:区域事件(A1-A6, A8) ---------------------------------------

    def _zone_events(self, t: int, ts: object, o: float, h: float, l: float,
                     c: float, prev_close: float, atr: float) -> list[MarketEvent]:
        p = self.params
        s = self.structure
        bm, rj = p.k_bm * atr, p.k_rj * atr
        n_a, n_f = p.n_a, p.n_f
        res_role = ZoneRole.RESISTANCE

        # A3 收尸:zone 离开 ACTIVE(破位/翻转/消亡)即 episode 终结;
        # 被合并取代(A3 未列为结束条件)则续绑到包含旧边界的同向 ACTIVE 区间。
        live = {id(z): z for z in s._zones.zones}
        kept_eps: list[_Episode] = []
        for ep in self._episodes:
            z = live.get(id(ep.zone))
            if z is not None:
                if z.state is ZoneState.ACTIVE:
                    kept_eps.append(ep)
                continue
            for z2 in s._zones.zones:
                if z2.role is ep.zone.role and z2.state is ZoneState.ACTIVE \
                        and z2.lo <= ep.zone.lo and z2.hi >= ep.zone.hi:
                    ep.zone = z2
                    kept_eps.append(ep)
                    break
        self._episodes = kept_eps

        # A2 新触碰开 episode(前收在正面之外,且本 bar [low,high] 与带相交)。
        for z in s._zones.zones:
            if z.state is not ZoneState.ACTIVE:
                continue
            if any(ep.zone is z for ep in self._episodes):
                continue
            res = z.role is res_role
            front_out = (prev_close < z.lo) if res else (prev_close > z.hi)
            if front_out and h >= z.lo and l <= z.hi:
                ex = h if res else l
                ep = _Episode(zone=z, opened=t, ex=ex, ex_index=t)
                # 触碰 bar 本身即刺穿(A5)
                if (h > z.hi + bm) if res else (l < z.lo - bm):
                    ep.puncture_index = t
                self._episodes.append(ep)

        # A4 破位候选登记:本 bar 收盘破位(结构已把 zone 置为 BROKEN)。
        # 破位即 episode 终结、E2/E3 竞争终止(A5/A6);候选 bar = 第 1 根。
        for z in s._zones.zones:
            if z.state is ZoneState.BROKEN and z.last_change_known == t:
                self._episodes = [ep for ep in self._episodes if ep.zone is not z]
                self._candidates = [cd for cd in self._candidates if cd.zone is not z]
                self._candidates.append(_AcceptCandidate(
                    zone=z, lo=z.lo, hi=z.hi, role=z.role, count=1, started=t))

        events: list[MarketEvent] = []

        # E1 推进(A4):任一收盘未在外侧即作废;第 N_a 根收盘发射。
        kept: list[_AcceptCandidate] = []
        for cd in self._candidates:
            hold = (c > cd.hi) if cd.role is res_role else (c < cd.lo)
            if t > cd.started:
                if not hold:
                    continue                  # 候选作废,回到 episode 检测
                cd.count += 1
            if cd.count >= n_a:
                direction = +1 if cd.role is res_role else -1
                ref = cd.hi if cd.role is res_role else cd.lo
                events.append(MarketEvent(
                    id=E1, direction=direction, ts=ts, known_index=t,
                    attrs={"zone": cd.zone.clone(), "ref": ref,
                           "invalid": (cd.lo - bm) if direction > 0 else (cd.hi + bm),
                           "started": cd.started},
                ))
                continue
            kept.append(cd)
        self._candidates = kept

        # E2(A5)/ E3(A6):逐 episode 判;每 episode 至多产出其一(A3)。
        survivors: list[_Episode] = []
        for ep in self._episodes:
            z = ep.zone
            res = z.role is res_role
            fired: Optional[MarketEvent] = None
            edge = z.hi if res else z.lo
            # §8.4 路径启发式:本 bar 极值是否先于回退(模块 docstring A6 条款)
            extreme_first = (c < o) if res else (c >= o)

            # 先序情形:先以本 bar 极值推进 ex
            if extreme_first:
                new_ex = max(ep.ex, h) if res else min(ep.ex, l)
                if new_ex != ep.ex:
                    ep.ex, ep.ex_index = new_ex, t

            # A5 刺穿登记(同一 episode 只记第一次)
            if ep.puncture_index is None:
                if (h > z.hi + bm) if res else (l < z.lo - bm):
                    ep.puncture_index = t

            # E2:刺穿起 ≤ N_f 根内收盘回到带内(破位则 episode 已在上文终结)
            if ep.puncture_index is not None and t - ep.puncture_index < n_f:
                returned = (c < z.hi) if res else (c > z.lo)
                if returned:
                    direction = -1 if res else +1
                    fired = _reject(E2, direction, z, t, ts, bm,
                                    {"puncture_index": ep.puncture_index}, edge)

            # E3:自 ex 向正面回退 ≥ k_rj*atr;后序 bar 不许对本 bar 新建 ex 判回退
            if fired is None and (extreme_first or ep.ex_index < t) \
                    and t - ep.ex_index <= n_f:
                retreat = (ep.ex - min(l, c)) if res else (max(h, c) - ep.ex)
                if retreat >= rj:
                    direction = -1 if res else +1
                    fired = _reject(E3, direction, z, t, ts, bm,
                                    {"ex": ep.ex, "ex_index": ep.ex_index}, edge)

            if fired is not None:                        # A3:episode 终结,去重
                events.append(fired)
            else:
                # 后序情形:判完回退后再以本 bar 极值推进 ex
                if not extreme_first:
                    new_ex = max(ep.ex, h) if res else min(ep.ex, l)
                    if new_ex != ep.ex:
                        ep.ex, ep.ex_index = new_ex, t
                survivors.append(ep)
        self._episodes = survivors

        events.extend(self._range_rotation(
            t, ts, c, atr, [e for e in events if e.id == E3]))
        return events

    # -- E5:RANGE_ROTATION(A8) ------------------------------------------------

    def _range_rotation(self, t: int, ts: object, c: float, atr: float,
                        e3_events: list[MarketEvent]) -> list[MarketEvent]:
        if not e3_events or self.structure.regime() is not Regime.RANGE:
            return []
        band = self._band()
        if band is None:
            return []
        band_lo, band_hi = band
        edge_tol = self.params.e5_edge_atr * atr
        room_min = self.params.k_room * atr
        out: list[MarketEvent] = []
        for e3 in e3_events:
            z: Zone = e3.attrs["zone"]
            zc = 0.5 * (z.lo + z.hi)
            near_top = abs(zc - band_hi) <= edge_tol    # 阻力侧贴带上缘
            near_bot = abs(zc - band_lo) <= edge_tol    # 支撑侧贴带下缘
            if not (near_top or near_bot):
                continue
            room = (c - band_lo) if near_top else (band_hi - c)
            if room < room_min:
                continue
            out.append(MarketEvent(
                id=E5, direction=e3.direction, ts=ts, known_index=t,
                attrs={"zone": z.clone(), "ref": e3.attrs["ref"],
                       "invalid": e3.attrs["invalid"],
                       "band": (band_lo, band_hi), "room": room,
                       "via_known": e3.known_index},
            ))
        return out

    def _band(self) -> Optional[tuple]:
        """A8 合成带:最近至多 range_max_pivots 个 confirmed pivots。"""
        recent = self.structure.pivots[-self.params.range_max_pivots:]
        if len(recent) < 4:
            return None
        highs = [pv.price for pv in recent if pv.kind is PivotKind.HIGH]
        lows = [pv.price for pv in recent if pv.kind is PivotKind.LOW]
        if not highs or not lows:
            return None
        return min(lows), max(highs)

    # -- E4:TREND_RESUMPTION(A7) -----------------------------------------------

    def _trend_resumption(self, t: int, ts: object, c: float,
                          atr: float) -> list[MarketEvent]:
        p = self.params
        regime = self.structure.regime()
        pivots = self.structure.pivots
        lo_d, hi_d = p.e4_depth_atr

        # 激活:本 bar 刚确认的回调终点 pivot(需 regime 为趋势、深度与守位成立)
        for pv in pivots:
            if pv.known_index != t:
                continue
            if pv.kind is PivotKind.LOW and regime is Regime.TREND_UP:
                highs = [x for x in pivots if x.kind is PivotKind.HIGH]
                lows = [x for x in pivots if x.kind is PivotKind.LOW]
                if len(highs) < 2 or len(lows) < 2:
                    continue
                leg_start = highs[-1].price      # 回调腿起点 H_start(前高 pivot)
                hl_prev = lows[-2].price         # 定义本趋势的最近 HL(A7)
                d = leg_start - pv.price
                if pv.price > hl_prev and lo_d * atr <= d <= hi_d * atr:
                    self._e4.append(_E4Candidate(
                        direction=+1, leg_start=leg_start, pb_price=pv.price,
                        depth=d, activated=t, deadline=t + p.n_f))
            elif pv.kind is PivotKind.HIGH and regime is Regime.TREND_DOWN:
                highs = [x for x in pivots if x.kind is PivotKind.HIGH]
                lows = [x for x in pivots if x.kind is PivotKind.LOW]
                if len(highs) < 2 or len(lows) < 2:
                    continue
                leg_start = lows[-1].price       # 镜像:反弹腿起点(前低 pivot)
                lh_prev = highs[-2].price        # 定义本趋势的最近 LH
                d = pv.price - leg_start
                if pv.price < lh_prev and lo_d * atr <= d <= hi_d * atr:
                    self._e4.append(_E4Candidate(
                        direction=-1, leg_start=leg_start, pb_price=pv.price,
                        depth=d, activated=t, deadline=t + p.n_f))

        # 触发:窗口 (activated, activated+N_f] 内收盘回收 50% 回调腿(A7)
        events: list[MarketEvent] = []
        kept: list[_E4Candidate] = []
        for cd in self._e4:
            if t <= cd.activated:
                kept.append(cd)
                continue
            if t > cd.deadline:
                continue
            level = cd.pb_price + cd.direction * p.e4_recovery * cd.depth
            if (c > level) if cd.direction > 0 else (c < level):
                events.append(MarketEvent(
                    id=E4, direction=cd.direction, ts=ts, known_index=t,
                    attrs={"leg_start": cd.leg_start, "pb_price": cd.pb_price,
                           "depth": cd.depth, "ref": level,
                           "invalid": cd.pb_price - cd.direction * p.k_bm * atr,
                           "regime": regime.value},
                ))
                continue
            kept.append(cd)
        self._e4 = kept
        return events

    # -- E6:STRUCTURE_BREAK(A9) -------------------------------------------------

    def _structure_break(self, t: int, ts: object, c: float,
                         atr: float) -> list[MarketEvent]:
        # "TREND_UP/…中"以 regime(t-1) 判定(模块 docstring §5/A9 条款)
        bm = self.params.k_bm * atr
        pivots = self.structure.pivots
        if self._prev_regime is Regime.TREND_UP:
            lows = [pv for pv in pivots if pv.kind is PivotKind.LOW]
            if lows:
                l_star = lows[-1].price          # 最近 confirmed HL(A9)
                if c < l_star - bm:
                    return [MarketEvent(
                        id=E6, direction=-1, ts=ts, known_index=t,
                        # §7: invalid = 结构价 + buf(与 E1-E4 一致;研究日志#5 修正)
                        attrs={"ref": l_star, "invalid": l_star + bm,
                               "pivot_forming": lows[-1].forming_index,
                               "regime_before": Regime.TREND_UP.value},
                    )]
        elif self._prev_regime is Regime.TREND_DOWN:
            highs = [pv for pv in pivots if pv.kind is PivotKind.HIGH]
            if highs:
                h_star = highs[-1].price         # 镜像:最近 confirmed LH
                if c > h_star + bm:
                    return [MarketEvent(
                        id=E6, direction=+1, ts=ts, known_index=t,
                        # §7: invalid = 结构价 - buf(与 E1-E4 一致;研究日志#5 修正)
                        attrs={"ref": h_star, "invalid": h_star - bm,
                               "pivot_forming": highs[-1].forming_index,
                               "regime_before": Regime.TREND_DOWN.value},
                    )]
        return []
