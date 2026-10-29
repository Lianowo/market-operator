"""市场行为事件 BREAKOUT_ACCEPTED-STRUCTURE_BROKEN(docs/methodology.md §6,精确定义 §6A 附录 A1-A9)+
v2 增量 FLIP_RETESTED(docs/v2-protocol.md §1,冻结;默认关闭,`enable_e7=True` 开启,
发射与否不影响 BREAKOUT_ACCEPTED-STRUCTURE_BROKEN 任何输出)。

检测器与 MarketStructure 同步逐 bar 喂数据(内部持有结构实例),所有事件在
`known_index = t` 的 bar 收盘判出,仅用 [0,t](§6 表头纪律)。

解释性决定(文字未完全展开处,取最符合因果语义的读法,标注条款):
- [A3] episode 以 zone 对象身份界定:zone 离开 ACTIVE(BROKEN/翻转/消亡)或事件
  发射即终结;每个 episode 至多产生 BREAKOUT_ACCEPTED/BREAKOUT_FAILED/LEVEL_REJECTED 之一。"zone 返回 ACTIVE(新一轮从正面
  接近)才开始新 episode"读作:事件发射后,只要价格仍在带内悬停(prev_close 未回到
  正面之外),不再开 episode;价格退回正面之外后重新(A2)触碰,即"新一轮从正面
  接近",开新 episode(可再次发射)——去重的语义单位是"接近过程",而非 zone 一生。
  §4.1 合并时,若存在同向 ACTIVE 新区间 ⊇ 旧区间,episode 续绑到该合并结果上
  (语义上同一区域的扩张,而非新接近);否则 episode 终结。BREAKOUT_ACCEPTED 破位候选不依赖
  episode(A4 只要求收盘破位,首次触碰直接破位的路径同样发 BREAKOUT_ACCEPTED)。
- [A4] 破位候选 bar 计为连续 N_a 根的第 1 根;之后任一收盘未收在 zone 外侧即作废。
- [A5] 刺穿 bar 计入窗口:BREAKOUT_FAILED 窗口为刺穿 bar 起共 N_f 根(offset ∈ [0, N_f-1]);
  "期间未发生 BREAKOUT_ACCEPTED 接受":一旦出现 A4 破位收盘(候选成立)即终止 BREAKOUT_FAILED 检测——
  竞争以破位 bar 先后为准。
- [A6] LEVEL_REJECTED 回退窗口 offset ∈ [0, N_f](含 ex 建立 bar 起的 N_f 根);同根 bar 上
  "ex 建立→回退"的先后按 §8.4 路径启发式:close < open(阻力侧)视为先见 high
  后见 low;反之本 bar 的 low 先于 high,只许对更早 bar 建立的 ex 判回退。
  同 bar 竞争优先级 BREAKOUT_ACCEPTED > BREAKOUT_FAILED > LEVEL_REJECTED(A6:"BREAKOUT_ACCEPTED/BREAKOUT_FAILED 先发生则终止")。
- [§6 表/A1] 方向:阻力侧事件为空(-1),支撑侧为多(+1);RANGE_ROTATED 跟随其来源 LEVEL_REJECTED;
  STRUCTURE_BROKEN 为趋势破坏警告(TREND_UP 破坏 → -1,TREND_DOWN 破坏 → +1)。
- [§4.3] BREAKOUT_FAILED/LEVEL_REJECTED(拒绝类)成立时给对应 zone `rejects` +1(事实记录)。
- [A7] TREND_RESUMED 在回调终点 pivot(L_pb)确认 bar 激活(需该 bar regime 为趋势且深度、
  守 HL 条件成立),触发窗口为 (known, known+N_f];深度与守位以确认时刻 atr 度量。
- [A8] RANGE_ROTATED 在来源 LEVEL_REJECTED 的同一根 bar 收盘发射;band 由最近至多 range_max_pivots 个
  confirmed pivots 合成;"对侧空间"以事件 bar 收盘至对侧边缘距离度量。
- [§5/A9] regime 谓词本身含"价格未破坏最近抬升低点"条款(以当前收盘判定,
  见 structure.py),故 STRUCTURE_BROKEN 以 `regime(t-1)` 为"TREND_UP/…中"之判定,恰好发射于
  regime 退出趋势的那一根。STRUCTURE_BROKEN 的 L*/H* 取最近 confirmed 低/高 pivot。
- [v2/docs/v2-protocol.md §1] FLIP_RETESTED FLIP_RETEST:某 zone 状态转为 FLIPPED 的 bar
  (破位→首次回踩→回踩被拒绝→角色互换,控制权易手确认)发射。信息来源是
  structure.ZoneBook 的只读事实记录 `flips`(转移发生时 zone 尚在场内,终止后
  移除前已由记录携带旧边界/旧角色/新角色)。方向:role_new=resistance ⇒ -1
  (旧支撑破位),role_new=support ⇒ +1(旧阻力破位)。invalid(v2-protocol §1):
  short = zone.hi + k_bm·atr,long = zone.lo − k_bm·atr;ref 取破位边缘
  (short = zone.lo,long = zone.hi,与 BREAKOUT_ACCEPTED 的 ref=被破边缘一致,供 §7 目标搜索)。
- [§7] `attrs["invalid"]` 为失效价建议:BREAKOUT_ACCEPTED 取 zone 正缘外侧(lo - buf / hi + buf,
  buf = k_bm*atr);BREAKOUT_FAILED/LEVEL_REJECTED 取外缘外侧;TREND_RESUMED 取回调终点外侧。均仅建议,不撮合。

v8 增量(docs/v8-protocol.md §1,冻结;默认关闭,`enable_e8=True` 开启,
发射与否不影响 BREAKOUT_ACCEPTED-FLIP_RETESTED 任何输出):EDGE_RECLAIMED EDGE_RECLAIM 边缘收复。解释性决定
(协议文字未完全展开处,取最符合因果语义的读法,标注条款):
- [§1 触发] EDGE_RECLAIMED 的"区域刺穿+≤N_f 根收复"判据逐字复用 A2/A3/A5(协议"沿用
  BREAKOUT_FAILED/LEVEL_REJECTED 的区域触碰判据"原话):即与 BREAKOUT_FAILED 同一 episode、同一刺穿、同一收复窗口
  (刺穿 bar 计入窗口,offset ∈ [0, N_f-1])、同一竞争次序(A4 破位候选在先即
  终止)。因此 EDGE_RECLAIMED 以"伴随 BREAKOUT_FAILED 发射"实现:BREAKOUT_FAILED 成立的 episode 才可能有 EDGE_RECLAIMED;
  A3 去重随之继承(每 episode 至多一枚 EDGE_RECLAIMED)。**多区并存取舍** = 逐 zone
  episode 独立判定(与 BREAKOUT_FAILED 完全同语义):同 bar 不同 zone 的 episode 可各自
  发射 EDGE_RECLAIMED,各携本 zone;"哪个 zone 计为触发区"即"哪个 episode 的 A5 收复
  成立",不存在跨 zone 的任选取舍。A3 的"BREAKOUT_ACCEPTED/BREAKOUT_FAILED/LEVEL_REJECTED 之一"清单不含 EDGE_RECLAIMED(与 RANGE_ROTATED
  伴随 LEVEL_REJECTED 同构):episode 终结体现在 BREAKOUT_FAILED 上,EDGE_RECLAIMED 是该 episode 拒绝事实的第二枚
  只读记录。
- [§1 语境] band B=[L*,H*] = 已 confirmed pivots 名单中最后 low/high pivot
  价(全名单,非 range_max_pivots 窗口,口径同 scripts/analyze_entry_position.py
  的结构尺;pivot 只在确认 bar 收盘知时入库(A10),故 L*/H* 与 pos 恒为因果量)。
  pos = (close(t) − L*) / (H* − L*),"当时 pos" = 收复(候选发射)bar 收盘值,
  含该 bar 新确认 pivot 对 band 的更新(同 bar 因果序:结构推进在前)。
  L*/H* 缺一侧或 H*−L* ≤ 0 ⇒ 门不可判,不发(fail-closed,不编造语境)。
- [§1 位置门] 多头候选需 pos ≤ pos_edge(冻结先验 0.20,敏感性 {0.15, 0.25}
  仅 dev);空头镜像 pos ≥ 1 − pos_edge。边界取值 inclusive(协议符号 ≤/≥)。
  regime 无关(协议§1:band 语境是唯一语境条件)。
- [§1 失效] invalid = "刺穿极值 ∓ k_bm·atr" 读作:自 episode 起至收复 bar(
  含)远侧最深极值(episode ex ∪ 本 bar 极值,收听盘知时)外侧 k_bm·atr;
  止损锚在被扫流动性最深价之外,而非 zone 边缘(BREAKOUT_FAILED 的 invalid=edge∓bm 不变)。
  ref = 收复边缘(多 zone.lo / 空 zone.hi,与 BREAKOUT_FAILED 的 ref 口径一致)。
- attrs:zone(收复边缘所在区,克隆)/ref/invalid/pos/band (L*, H*)/atr(收复
  bar 的 ATR)/puncture_index(A5 刺穿 bar)/ex(上述刺穿极值,用于 invalid)。

v9 增量(docs/v9-protocol.md §1-§3,冻结;默认关闭,`enable_e9e10=True` 开启,
发射与否不改变 BREAKOUT_ACCEPTED-EDGE_RECLAIMED 任何输出):PROCESS_ACCEPTED PROCESS_ACCEPTED(过程式接受,顺延续)
与 PROCESS_REJECTED PROCESS_REJECTED(过程式拒绝,反向)。过程量随 episode 每根 bar 收
盘累计(证据单位从"点"换为"段")。解释性决定(协议文字未完全展开处,取最
符合因果语义的读法,标注条款):
- [§1 年龄] a = t − opened(首次触碰 bar 的 a=0);"含当根"落在全部过程量的
  累计与归一分母 = a+1 = episode 内 bar 数(自触碰 bar 起,含本 bar)——occ
  的分子分母同一集合,协议 "occ = 根数/(a+1)" 只有在此读法下内部一致。
- [§1 过程量] occ = episode 内收盘越出远缘(阻力 close>hi / 支撑镜像
  close<lo)的根数 / (a+1);centroid = (episode 收盘均值 − 远缘)/atr(t) 按
  面子取符号(阻力 mean−hi;支撑 lo−mean,只用 episode 内收盘价);wickfail
  = 影线越过远缘而收盘未能越出的根数(阻力 high>hi 且 close≤hi;支撑镜像);
  ex = episode 内纯极值(max high / min low,含当根)——注意独立于 A6 的
  ex(ep.ex):后者带 §8.4 路径启发式、为 BREAKOUT_FAILED/LEVEL_REJECTED 回退次序专用;协议 §1 的
  ex(t) 定义为简单累计过程极值,不附次序假设(PROCESS_REJECTED 判据是"距极值回退多少"
  的静态比较,非"极值之后发生"的时序断言)。全部量只用 [opened, t] 收盘
  已知数据累计(A10;严禁 episode 之后数据,测试断言)。
- [§1 episode 生命周期] 沿用 A2/A3 既有语义,不许扩张:episode = 自 zone
  正面(A2)首次触碰起,直至 zone 离开 ACTIVE(BROKEN/翻转/消亡,或合并
  续绑失败)或事件成交(BREAKOUT_ACCEPTED/BREAKOUT_FAILED/LEVEL_REJECTED 发射即终结);PROCESS_ACCEPTED/PROCESS_REJECTED 为研究目录事件(不
  入任何生产白名单 ⇒ 不成交),其发射不终结 episode,去重由"各 episode
  至多各一次"承担;A3 合并续绑沿用不变(过程量随 episode 走,语义 = 同
  一接近过程的延续)。与破位竞争的残余:PROCESS_ACCEPTED 的"收穿远缘"只在未破位残余
  区间(阻力 close ∈ (hi, hi+k_bm·atr])可发——A4 破位收盘先行终结
  episode 走 BREAKOUT_ACCEPTED 候选路径(既有语义,不绕过)。
- [§2 判定与同 bar 竞争] 同 bar 内既有事件(BREAKOUT_ACCEPTED/BREAKOUT_FAILED/LEVEL_REJECTED,伴随 EDGE_RECLAIMED)优先:
  老轨道发射即 episode 终结,该 bar 不再判 PROCESS_ACCEPTED/PROCESS_REJECTED。PROCESS_REJECTED 先于 PROCESS_ACCEPTED 判:同
  episode PROCESS_REJECTED 已发 ⇒ PROCESS_ACCEPTED 永不再发(协议"无效化"条款,含同 bar/时序竞争);
  PROCESS_ACCEPTED 先发不阻止 PROCESS_REJECTED(事实上 occ=0 与 occ≥2/3 互斥,自然不可达)。
- [§2 PROCESS_ACCEPTED] a ≥ min_age(3) 且 occ ≥ 2/3 且 centroid ≥ 0.3 且当前收穿远缘,
  方向=延续(阻力 +1 多 / 支撑 −1 空);invalid = 正缘整深外侧(协议§3:
  阻力 zone.lo − k_bm·atr / 支撑 zone.hi + k_bm·atr,整区深度+buf)。
- [§2 PROCESS_REJECTED] a ≥ min_age(3) 且 occ = 0 且 wickfail ≥ 2 且 close ≤ ex −
  k_rj·atr(支撑镜像 close ≥ ex + k_rj·atr) 且 centroid ≤ 0,方向=反转
  (阻力 −1 空 / 支撑 +1 多);invalid = ex ± k_bm·atr(刺穿极值+buf,协
  议§3)。k_rj 沿用 config 先验(params.k_rj,非新参数)。
- [attrs] zone(触发区克隆)/edge(远缘)/ref(=edge,供 hypothesis §7
  目标搜索锚)/occ/centroid/wickfail/ex/age/invalid/atr(发射 bar ATR,
  供 F1 闸复用)。
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

# 事件标识 = 语义名:值即标识,配置、报告、日志里都是同一个可读字符串,
# 不再有"编号 → 含义"的隐式对照表需要记忆。
BREAKOUT_ACCEPTED = "BREAKOUT_ACCEPTED"  # 突破接受:收盘越出区带并连续站稳外侧
BREAKOUT_FAILED = "BREAKOUT_FAILED"      # 失败突破:刺穿后窗口内未获接受
LEVEL_REJECTED = "LEVEL_REJECTED"        # 关键位拒绝:建立刺穿极值后回退回带内
TREND_RESUMED = "TREND_RESUMED"          # 趋势恢复:回调终点确认后顺原方向续行
RANGE_ROTATED = "RANGE_ROTATED"          # 区间回归:触边后折返对侧
STRUCTURE_BROKEN = "STRUCTURE_BROKEN"    # 结构破坏:趋势中的最近结构点被收盘击穿
FLIP_RETESTED = "FLIP_RETESTED"          # 角色互换回踩:破位→回踩被拒→角色互换
EDGE_RECLAIMED = "EDGE_RECLAIMED"        # 边缘收复:摆动带边缘刺穿后被收复
PROCESS_ACCEPTED = "PROCESS_ACCEPTED"    # 过程式接受:停留率/重心确认的顺延续
PROCESS_REJECTED = "PROCESS_REJECTED"    # 过程式拒绝:试探失败密度确认的反转

# 配置里的极简原语(箱体边缘两件事),指向具体事件标识。
ACCEPT = BREAKOUT_ACCEPTED   # 箱体边缘突破接受(顺迁移)
REJECT = LEVEL_REJECTED      # 箱体边缘试探拒绝(箱内回归)
CONSUMED = "CONSUMED"        # 边界超时粘连钝化淘汰态(非交易垃圾回收态)

#: 历史编号 → 语义名。**只**用于读取 2026-10 之前的配置与报告;
#: 新代码、新配置不得使用键名侧的内容,也不得反向映射。
LEGACY_EVENT_CODES = {
    "E1": BREAKOUT_ACCEPTED,
    "E2": BREAKOUT_FAILED,
    "E3": LEVEL_REJECTED,
    "E4": TREND_RESUMED,
    "E5": RANGE_ROTATED,
    "E6": STRUCTURE_BROKEN,
    "E7": FLIP_RETESTED,
    "E8": EDGE_RECLAIMED,
    "E9": PROCESS_ACCEPTED,
    "E10": PROCESS_REJECTED,
    "ACCEPT": BREAKOUT_ACCEPTED,
    "REJECT": LEVEL_REJECTED,
    "CONSUMED": CONSUMED,
}


#: 全部事件标识(顺序 = 检测优先序:同 bar 竞争时靠前者优先)。
EVENT_IDENTIFIERS = (
    BREAKOUT_ACCEPTED, BREAKOUT_FAILED, LEVEL_REJECTED, TREND_RESUMED,
    RANGE_ROTATED, STRUCTURE_BROKEN, FLIP_RETESTED, EDGE_RECLAIMED,
    PROCESS_ACCEPTED, PROCESS_REJECTED,
)


def canonical_event_id(event_id: object) -> str:
    """把历史编号或极简原语归一为语义标识;未知标识原样返回(便于暴露拼写错误)。

    归一化只有这一处:配置解析、路由白名单、计数口径都调它,因此不会出现
   "路由认别名而计数不认"的分裂。
    """
    text = str(event_id)
    return LEGACY_EVENT_CODES.get(text, text)


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
    """A3 approach episode:zone 自第一次(A2)触碰起被接近;至多产出一个事件。

    v9 过程量字段(enable_e9e10=False 时恒为默认值、不参与任何读写 ⇒ BREAKOUT_ACCEPTED-EDGE_RECLAIMED
    行为零变化;开启后仅 PROCESS_ACCEPTED/PROCESS_REJECTED 判定读写,BREAKOUT_ACCEPTED-EDGE_RECLAIMED 轨道同样不读):occ_count/
    close_sum/wickfail/ex_proc 自触碰 bar 收盘起逐 bar 累计(协议 §1);
    e9_fired/e10_fired 为各 episode 至多各一次的去重标记(协议 §2)。
    """

    zone: Zone                # 活引用(与 ZoneBook 中的对象同一)
    opened: int
    ex: float                 # episode 内进入极值(A6);阻力=最高 high,支撑=最低 low
    ex_index: int
    puncture_index: Optional[int] = None  # A5 刺穿 bar
    outside_closes: int = 0   # v22 R3:episode 期间收盘越过外沿(hi+bm / lo-bm)的根数

    # --- v9 过程量(docs/v9-protocol.md §1,默认关;非 v9 字段以上不读) ---
    occ_count: int = 0        # episode 内收盘越出远缘的根数(occ 的分子)
    close_sum: float = 0.0    # episode 内收盘价累计(centroid 的均值分子)
    wickfail: int = 0         # 影线越缘而收盘未越缘的根数(试探失败密度)
    ex_proc: float = 0.0      # episode 内纯过程极值(含当根;独立于 A6 ex)
    e9_fired: bool = False
    e10_fired: bool = False


@dataclass
class _AcceptCandidate:
    """A4/BREAKOUT_ACCEPTED 破位候选:zone 收盘破位起,要求连续 N_a 根收在 zone 外侧。"""

    zone: Zone
    lo: float                 # 快照边界(破位判定基准)
    hi: float
    role: ZoneRole
    count: int                # 已连续收在外侧的根数(候选 bar = 第 1 根)
    started: int


@dataclass
class _E4Candidate:
    """A7/TREND_RESUMED:回调终点 pivot 确认后激活,等待收盘回收 50% 回调腿。"""

    direction: int            # +1: TREND_UP 恢复做多; -1: TREND_DOWN 恢复做空
    leg_start: float          # 回调腿起点(前高 pivot 价;镜像为前低)
    pb_price: float           # 回调终点 pivot 价(L_pb;镜像为 H_pb)
    depth: float              # D
    activated: int
    deadline: int             # activated + N_f


def _reject(eid: str, direction: int, z: Zone, t: int, ts: object, bm: float,
            extra: dict, edge: float) -> MarketEvent:
    """BREAKOUT_FAILED/LEVEL_REJECTED(拒绝类)发射:先记 §4.3 事实(rejects+1、知时),再快照 zone 进 attrs。"""
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
    结构推进 → episode 开/关(A2/A3)→ 破位候选登记(A4)→ BREAKOUT_ACCEPTED 推进(A4)
    → BREAKOUT_FAILED/A5、LEVEL_REJECTED/A6 逐 episode 判 → RANGE_ROTATED 合成(A8)→ TREND_RESUMED 激活/触发(A7)→ STRUCTURE_BROKEN(A9)。
    """

    def __init__(self, params: Params, *, enable_e7: bool = False,
                 enable_e8: bool = False, e8_pos_edge: float = 0.20,
                 enable_e9e10: bool = False, e9e10_min_age: int = 3,
                 e9_occ_min: float = 2.0 / 3.0,
                 e9_centroid_min: float = 0.3) -> None:
        self.params = params
        # enable_e7 = False ⇒ 只发 v1 目录(BREAKOUT_ACCEPTED-STRUCTURE_BROKEN,行为与 v1 完全一致);
        # True ⇒ 追加 v2 FLIP_RETESTED(docs/v2-protocol.md §1),BREAKOUT_ACCEPTED-STRUCTURE_BROKEN 不受影响。
        self.enable_e7 = enable_e7
        # enable_e8 = False ⇒ 不发 v8 目录;True ⇒ 追加 EDGE_RECLAIMED(docs/v8-protocol.md
        # §1,伴随 BREAKOUT_FAILED 发射,BREAKOUT_ACCEPTED-FLIP_RETESTED 不受影响)。e8_pos_edge = 边缘带位置门
        # (冻结先验 0.20;敏感性 {0.15, 0.25} 仅 dev,见协议§1)。
        self.enable_e8 = enable_e8
        self.e8_pos_edge = float(e8_pos_edge)
        # enable_e9e10 = False ⇒ 不发 v9 目录(BREAKOUT_ACCEPTED-EDGE_RECLAIMED 逐位不变);True ⇒ 追加
        # PROCESS_ACCEPTED/PROCESS_REJECTED(docs/v9-protocol.md §1-§3 过程式接受/拒绝)。三个数值先验全
        # 来自 config(min_age=3、occ 阈 2/3、centroid 阈 0.3;k_rj 直接沿用
        # params.k_rj 非新参数)。敏感性 {min_age 2/4, occ 0.6/0.75, centroid
        # 0.2/0.5} 仅 dev,由调用方显式传入,本层不做任何择优。
        self.enable_e9e10 = enable_e9e10
        self.e9e10_min_age = int(e9e10_min_age)
        self.e9_occ_min = float(e9_occ_min)
        self.e9_centroid_min = float(e9_centroid_min)
        self.structure = MarketStructure(params)
        self.events: list[MarketEvent] = []
        self._episodes: list[_Episode] = []
        self._candidates: list[_AcceptCandidate] = []
        self._e4: list[_E4Candidate] = []
        self._prev_regime: Regime = Regime.TRANSITION
        self.consumed_episodes: int = 0


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
            if self.enable_e7:
                emitted.extend(self._flip_retest(t, ts, atr))
        self._prev_regime = s.regime()
        self.events.extend(emitted)
        return emitted

    # -- BREAKOUT_ACCEPTED/BREAKOUT_FAILED/LEVEL_REJECTED/RANGE_ROTATED:区域事件(A1-A6, A8) ---------------------------------------

    def _zone_events(self, t: int, ts: object, o: float, h: float, l: float,
                     c: float, prev_close: float, atr: float) -> list[MarketEvent]:
        p = self.params
        s = self.structure
        bm, rj = p.k_bm * atr, p.k_rj * atr
        n_a, n_f = p.n_a, p.n_f
        res_role = ZoneRole.RESISTANCE

        # A3 收尸:zone 离开 ACTIVE(破位/翻转/消亡)即 episode 终结;
        # v22 R3 扩展:当 use_balance_detector=True 时,允许 1~2 根收盘假突破
        # (只要未发生 > 1.25*ATR 单边逃逸且在 <= n_f 窗口内)暂保 _Episode 观察吞没收复!
        live = {id(z): z for z in s._zones.zones}
        kept_eps: list[_Episode] = []
        for ep in self._episodes:
            z = live.get(id(ep.zone))
            if z is not None:
                if z.state is ZoneState.ACTIVE:
                    kept_eps.append(ep)
                elif (p.use_balance_detector
                      and z.state in (ZoneState.BROKEN, ZoneState.RETESTED)
                      and ep.outside_closes <= 2
                      and (t - (ep.puncture_index if ep.puncture_index is not None else ep.opened)) <= n_f):
                    res = z.role is res_role
                    escape = (ep.ex - z.hi) if res else (z.lo - ep.ex)
                    if escape <= 1.25 * atr:
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
        # v22:若当根同时触碰并直接收盘破位(z 在当根刚转为 BROKEN),同样开启 _Episode 供 R3 假突破观察。
        for z in s._zones.zones:
            just_broken = (
                p.use_balance_detector
                and z.state is ZoneState.BROKEN
                and z.last_change_known == t
            )
            if z.state is not ZoneState.ACTIVE and not just_broken:
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
                if self.enable_e9e10:
                    # v9 §1:过程量自触碰 bar 收盘起累计(含当根)
                    ep.close_sum = c
                    ep.ex_proc = ex
                    if (c > z.hi) if res else (c < z.lo):
                        ep.occ_count = 1
                    if (h > z.hi and c <= z.hi) if res else (l < z.lo and c >= z.lo):
                        ep.wickfail = 1
                self._episodes.append(ep)

        # A4 破位候选登记:本 bar 收盘破位(结构已把 zone 置为 BROKEN)。
        # v22:若 use_balance_detector=True 且极值未逃逸(>1.25*ATR),暂不销毁 _Episode 供 R3 观察。
        for z in s._zones.zones:
            if z.state is ZoneState.BROKEN and z.last_change_known == t:
                if p.use_balance_detector:
                    res = z.role is res_role
                    escape = (h - z.hi) if res else (z.lo - l)
                    if escape > 1.25 * atr:
                        self._episodes = [ep for ep in self._episodes if ep.zone is not z]
                else:
                    self._episodes = [ep for ep in self._episodes if ep.zone is not z]
                self._candidates = [cd for cd in self._candidates if cd.zone is not z]
                cd_new = _AcceptCandidate(
                    zone=z, lo=z.lo, hi=z.hi, role=z.role, count=1, started=t)
                cd_new._start_close = c
                self._candidates.append(cd_new)

        events: list[MarketEvent] = []
        rng = max(h - l, 1e-12)
        cur_regime = s.regime()

        # BREAKOUT_ACCEPTED 推进(A4):任一收盘未在外侧即作废;第 N_a 根收盘发射。
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
                ext_dist = (c - cd.hi) if direction > 0 else (cd.lo - c)
                if p.use_balance_detector:
                    # 拍卖市场真实接受三要素(纯第一性原理):
                    # ① 绝不逆大级别反向趋势追单: 多头突破要求当前非 TREND_DOWN; 空头突破要求当前非 TREND_UP
                    not_counter = (cur_regime is not Regime.TREND_DOWN) if direction > 0 else (cur_regime is not Regime.TREND_UP)
                    # ② 确认 K 线必须比破位首根继续推进(c >= start_c), 且收盘在强势区(非冲高回落大阴线假站稳)
                    start_c = getattr(cd, "_start_close", c)
                    close_pos = (c - l) / rng if direction > 0 else (h - c) / rng
                    progressing = (c >= start_c and c > o and close_pos >= 0.50) if direction > 0 else (c <= start_c and c < o and close_pos >= 0.50)
                    # ③ 未极端透支高潮 (距离突破口 <= 1.5 * ATR)
                    not_climax = ext_dist <= 1.50 * atr
                    if not_counter and progressing and not_climax:
                        sub_kind = "A2_OVEREXTENDED" if ext_dist > 0.80 * atr else "A1_TIGHT"
                        events.append(MarketEvent(
                            id=BREAKOUT_ACCEPTED, direction=direction, ts=ts, known_index=t,
                            attrs={"zone": cd.zone.clone(), "ref": ref,
                                   "invalid": (cd.lo - bm) if direction > 0 else (cd.hi + bm),
                                   "started": cd.started, "sub_kind": sub_kind},
                        ))
                else:
                    events.append(MarketEvent(
                        id=BREAKOUT_ACCEPTED, direction=direction, ts=ts, known_index=t,
                        attrs={"zone": cd.zone.clone(), "ref": ref,
                               "invalid": (cd.lo - bm) if direction > 0 else (cd.hi + bm),
                               "started": cd.started},
                    ))
                continue
            kept.append(cd)
        self._candidates = kept



        # BREAKOUT_FAILED(A5)/ LEVEL_REJECTED(A6):逐 episode 判;每 episode 至多产出其一(A3)。
        survivors: list[_Episode] = []
        for ep in self._episodes:
            z = ep.zone
            res = z.role is res_role
            fired: Optional[MarketEvent] = None
            edge = z.hi if res else z.lo
            extreme_first = (c < o) if res else (c >= o)

            if extreme_first:
                new_ex = max(ep.ex, h) if res else min(ep.ex, l)
                if new_ex != ep.ex:
                    ep.ex, ep.ex_index = new_ex, t

            if ep.puncture_index is None:
                if (h > z.hi + bm) if res else (l < z.lo - bm):
                    ep.puncture_index = t

            closed_outside = (c > z.hi + bm) if res else (c < z.lo - bm)
            close_pos = (h - c) / rng if res else (c - l) / rng

            # v22 R3 假突破吞没收复:此前有 1~2 根收盘越过外沿,本根收盘反向砸回内沿以内 -> 发射 LEVEL_REJECTED(R3_TRAP)
            if (p.use_balance_detector and 1 <= ep.outside_closes <= 2
                    and ((c < z.lo and close_pos >= 0.50) if res else (c > z.hi and close_pos >= 0.50))):
                direction = -1 if res else +1
                inv_r3 = (max(z.hi, ep.ex) + bm) if res else (min(z.lo, ep.ex) - bm)
                fired = _reject(
                    LEVEL_REJECTED, direction, z, t, ts, bm,
                    {"ex": ep.ex, "ex_index": ep.ex_index,
                     "puncture_index": ep.puncture_index,
                     "outside_closes": ep.outside_closes,
                     "sub_kind": "R3_TRAP", "invalid": inv_r3},
                    edge,
                )
                z.state = ZoneState.ACTIVE
                z.last_change_known = t
                self._candidates = [cd for cd in self._candidates if cd.zone is not z]

            # BREAKOUT_FAILED:刺穿起 ≤ N_f 根内收盘回到带内
            if fired is None and not p.use_balance_detector and ep.puncture_index is not None and t - ep.puncture_index < n_f:
                returned = (c < z.hi) if res else (c > z.lo)
                if returned:
                    direction = -1 if res else +1
                    fired = _reject(BREAKOUT_FAILED, direction, z, t, ts, bm,
                                    {"puncture_index": ep.puncture_index}, edge)

            # LEVEL_REJECTED(R1 真实收盘抽离拒绝 vs 历史单带回退)
            if fired is None and ep.outside_closes == 0 and z.state is ZoneState.ACTIVE \
                    and (extreme_first or ep.ex_index < t) and t - ep.ex_index <= n_f:
                if p.use_balance_detector:
                    real_retreat = (ep.ex - c) if res else (c - ep.ex)
                    back_inside = (c <= z.lo) if res else (c >= z.hi)
                    against_trend = (cur_regime is Regime.TREND_UP) if res else (cur_regime is Regime.TREND_DOWN)
                    bar_dir_ok = ((c < o) if res else (c > o)) if against_trend else (close_pos >= 0.45)
                    if real_retreat >= rj and back_inside and close_pos >= 0.45 and bar_dir_ok:
                        direction = -1 if res else +1
                        extra_rej: dict = {
                            "ex": ep.ex, "ex_index": ep.ex_index,
                            "invalid": (max(z.hi, ep.ex) + bm) if res else (min(z.lo, ep.ex) - bm),
                            "sub_kind": "R1_WICK" if ep.puncture_index is not None else "R2_STALL",
                        }
                        fired = _reject(LEVEL_REJECTED, direction, z, t, ts, bm,
                                        extra_rej, edge)
                else:
                    retreat = (ep.ex - min(l, c)) if res else (max(h, c) - ep.ex)
                    if retreat >= rj:
                        direction = -1 if res else +1
                        fired = _reject(LEVEL_REJECTED, direction, z, t, ts, bm,
                                        {"ex": ep.ex, "ex_index": ep.ex_index}, edge)



            if fired is not None:                        # A3:episode 终结,去重
                events.append(fired)
                # v8 EDGE_RECLAIMED:伴随 BREAKOUT_FAILED 发射(模块 docstring v8 条款);与 A3 去重正交互
                if self.enable_e8 and fired.id == BREAKOUT_FAILED:
                    reclaimed = self._edge_reclaim(ep, z, res, t, ts, c, h, l, atr)
                    if reclaimed is not None:
                        events.append(reclaimed)
            else:
                # 后序情形:判完回退后再以本 bar 极值推进 ex
                if not extreme_first:
                    new_ex = max(ep.ex, h) if res else min(ep.ex, l)
                    if new_ex != ep.ex:
                        ep.ex, ep.ex_index = new_ex, t
                if closed_outside:
                    ep.outside_closes += 1
                    if p.use_balance_detector and ep.outside_closes > 2:
                        continue
                # v9 PROCESS_ACCEPTED/PROCESS_REJECTED:老轨道未发射的 episode 才进入过程判定
                if self.enable_e9e10:
                    proc = self._process_verdict(ep, z, res, t, ts, h, l, c, atr)
                    if proc is not None:
                        events.append(proc)
                # v22 CONSUMED 钝化淘汰态:存续超过 N_f + 1 根 K 线仍未决出胜负,淘汰销毁不交易
                if p.use_balance_detector and (t - ep.opened) > (n_f + 1):
                    self.consumed_episodes += 1
                    continue
                survivors.append(ep)

        self._episodes = survivors

        events.extend(self._range_rotation(
            t, ts, c, atr, [e for e in events if e.id == LEVEL_REJECTED]))
        return events

    # -- PROCESS_ACCEPTED/PROCESS_REJECTED:过程式接受/拒绝(docs/v9-protocol.md §1-§3,v9 增量) -------------

    def _process_verdict(self, ep: _Episode, z: Zone, res: bool, t: int,
                         ts: object, h: float, l: float, c: float, atr: float
                         ) -> Optional[MarketEvent]:
        """episode 存活 bar 上的过程量累计与 PROCESS_REJECTED/PROCESS_ACCEPTED 判定(一个 bar 至多产出一枚)。

        触碰 bar(t == ep.opened)的量已在 A2 开 episode 时累计;此处只累计
        (opened, t] 的 bar,判定只用 [opened, t] 收盘已知量(A10,严禁 episode
        之后数据)。PROCESS_REJECTED 先于 PROCESS_ACCEPTED 判(协议§2:同 episode 已发 PROCESS_REJECTED ⇒ 不再发 PROCESS_ACCEPTED,
        含同 bar 时序竞争);PROCESS_ACCEPTED 先发不终结 episode,PROCESS_REJECTED 后发亦各至多一次。
        """
        if t > ep.opened:
            ep.close_sum += c
            if (c > z.hi) if res else (c < z.lo):
                ep.occ_count += 1
            if (h > z.hi and c <= z.hi) if res else (l < z.lo and c >= z.lo):
                ep.wickfail += 1
            ep.ex_proc = max(ep.ex_proc, h) if res else min(ep.ex_proc, l)
        a = t - ep.opened
        if a < self.e9e10_min_age:
            return None
        n_bars = a + 1                             # 含当根的 episode 根数(§1)
        p = self.params
        occ = ep.occ_count / n_bars
        mean_c = ep.close_sum / n_bars
        centroid = ((mean_c - z.hi) if res else (z.lo - mean_c)) / atr
        edge = z.hi if res else z.lo
        bm, rj = p.k_bm * atr, p.k_rj * atr
        ex = ep.ex_proc
        # PROCESS_REJECTED 过程式拒绝(反向):停留率零 + 试探失败已成习 + 收远距极值
        if not ep.e10_fired and ep.occ_count == 0 and ep.wickfail >= 2 \
                and ((c <= ex - rj) if res else (c >= ex + rj)) \
                and centroid <= 0.0:
            ep.e10_fired = True
            direction = -1 if res else +1
            return MarketEvent(
                id=PROCESS_REJECTED, direction=direction, ts=ts, known_index=t,
                attrs={"zone": z.clone(), "edge": edge, "ref": edge,
                       "occ": occ, "centroid": centroid,
                       "wickfail": ep.wickfail, "ex": ex, "age": a,
                       "invalid": (ex + bm) if res else (ex - bm),
                       "atr": atr})
        # PROCESS_ACCEPTED 过程式接受(顺延续):停留率 + 重心已搬去远侧 + 当前收穿远缘
        if not ep.e9_fired and not ep.e10_fired \
                and occ >= self.e9_occ_min \
                and centroid >= self.e9_centroid_min \
                and ((c > z.hi) if res else (c < z.lo)):
            ep.e9_fired = True
            direction = +1 if res else -1
            return MarketEvent(
                id=PROCESS_ACCEPTED, direction=direction, ts=ts, known_index=t,
                attrs={"zone": z.clone(), "edge": edge, "ref": edge,
                       "occ": occ, "centroid": centroid,
                       "wickfail": ep.wickfail, "ex": ex, "age": a,
                       "invalid": (z.lo - bm) if res else (z.hi + bm),
                       "atr": atr})
        return None

    # -- EDGE_RECLAIMED:EDGE_RECLAIM(docs/v8-protocol.md §1,v8 增量) ------------------------

    def _edge_reclaim(self, ep: _Episode, z: Zone, res: bool, t: int,
                      ts: object, c: float, h: float, l: float, atr: float
                      ) -> Optional[MarketEvent]:
        """BREAKOUT_FAILED 成立的 episode 上过 pos 门 ⇒ EDGE_RECLAIMED(多=支撑区收复,空=阻力区收复)。

        band L*/H* = 已 confirmed pivots 全名单最后 low/high 价(口径同
        analyze_entry_position.py 结构尺,因果);pos 以本 bar 收盘计门
        (多 pos ≤ pos_edge;空 pos ≥ 1 − pos_edge,inclusive)。invalid =
        刺穿行程极值(episode ex ∪ 本 bar 极值)∓ k_bm·atr;ref = 收复边缘。
        """
        pivots = self.structure.pivots
        l_star = h_star = None
        for pv in pivots:                            # 全名单,升序,末位 = 最近知时
            if pv.kind is PivotKind.LOW:
                l_star = pv.price
            else:
                h_star = pv.price
        if l_star is None or h_star is None:
            return None                              # 语境带不完整 ⇒ 门不可判
        rg = h_star - l_star
        if rg <= 0.0:
            return None
        pos = (c - l_star) / rg
        direction = -1 if res else +1
        edge = self.e8_pos_edge
        if direction > 0:
            if pos > edge:
                return None
        elif pos < 1.0 - edge:
            return None
        bm = self.params.k_bm * atr
        ex = max(ep.ex, h) if res else min(ep.ex, l)  # 刺穿极值(含本 bar,因果)
        return MarketEvent(
            id=EDGE_RECLAIMED, direction=direction, ts=ts, known_index=t,
            attrs={"zone": z.clone(),
                   "ref": (z.hi if res else z.lo),
                   "invalid": (ex + bm) if res else (ex - bm),
                   "pos": pos, "band": (l_star, h_star), "atr": atr,
                   "puncture_index": ep.puncture_index, "ex": ex},
        )

    # -- RANGE_ROTATED:RANGE_ROTATION(A8) ------------------------------------------------

    def _range_rotation(self, t: int, ts: object, c: float, atr: float,
                        rejected_events: list[MarketEvent]) -> list[MarketEvent]:
        if not rejected_events or self.structure.regime() is not Regime.RANGE:
            return []
        band = self._band()
        if band is None:
            return []
        band_lo, band_hi = band
        edge_tol = self.params.e5_edge_atr * atr
        room_min = self.params.k_room * atr
        out: list[MarketEvent] = []
        for rejection in rejected_events:
            z: Zone = rejection.attrs["zone"]
            zc = 0.5 * (z.lo + z.hi)
            near_top = abs(zc - band_hi) <= edge_tol    # 阻力侧贴带上缘
            near_bot = abs(zc - band_lo) <= edge_tol    # 支撑侧贴带下缘
            if not (near_top or near_bot):
                continue
            room = (c - band_lo) if near_top else (band_hi - c)
            if room < room_min:
                continue
            out.append(MarketEvent(
                id=RANGE_ROTATED, direction=rejection.direction, ts=ts, known_index=t,
                attrs={"zone": z.clone(), "ref": rejection.attrs["ref"],
                       "invalid": rejection.attrs["invalid"],
                       "band": (band_lo, band_hi), "room": room,
                       "via_known": rejection.known_index},
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

    # -- TREND_RESUMED:TREND_RESUMPTION(A7) -----------------------------------------------

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
                    id=TREND_RESUMED, direction=cd.direction, ts=ts, known_index=t,
                    attrs={"leg_start": cd.leg_start, "pb_price": cd.pb_price,
                           "depth": cd.depth, "ref": level,
                           "invalid": cd.pb_price - cd.direction * p.k_bm * atr,
                           "regime": regime.value},
                ))
                continue
            kept.append(cd)
        self._e4 = kept
        return events

    # -- STRUCTURE_BROKEN:STRUCTURE_BREAK(A9) -------------------------------------------------

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
                        id=STRUCTURE_BROKEN, direction=-1, ts=ts, known_index=t,
                        # §7: invalid = 结构价 + buf(与 BREAKOUT_ACCEPTED-TREND_RESUMED 一致;研究日志#5 修正)
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
                        id=STRUCTURE_BROKEN, direction=+1, ts=ts, known_index=t,
                        # §7: invalid = 结构价 - buf(与 BREAKOUT_ACCEPTED-TREND_RESUMED 一致;研究日志#5 修正)
                        attrs={"ref": h_star, "invalid": h_star - bm,
                               "pivot_forming": highs[-1].forming_index,
                               "regime_before": Regime.TREND_DOWN.value},
                    )]
        return []

    # -- FLIP_RETESTED:FLIP_RETEST(docs/v2-protocol.md §1,v2 增量) -----------------------

    def _flip_retest(self, t: int, ts: object, atr: float) -> list[MarketEvent]:
        """本 bar 发生的全部 FLIPPED 角色互换 → FLIP_RETESTED(破位→回踩→被拒绝→控制权易手)。

        role_new=resistance(旧支撑破位)⇒ -1;role_new=support ⇒ +1(v2-protocol
        §1,对称测、不做方向挑选)。invalid = zone 远端边缘 ± k_bm·atr;ref = 破位
        边缘(short=lo,long=hi);atr = 转移 bar 的 ATR(供 F1 查验)。
        """
        bm = self.params.k_bm * atr
        events: list[MarketEvent] = []
        for flip in self.structure._zones.flips:
            if flip.index != t:
                continue
            z = flip.zone                       # 新角色区快照(边界 = 旧区边界)
            direction = -1 if z.role is ZoneRole.RESISTANCE else +1
            ref = z.lo if direction < 0 else z.hi
            invalid = (z.hi + bm) if direction < 0 else (z.lo - bm)
            events.append(MarketEvent(
                id=FLIP_RETESTED, direction=direction, ts=ts, known_index=t,
                attrs={"zone": z.clone(), "ref": ref, "invalid": invalid,
                       "from_role": flip.from_role.value, "atr": atr},
            ))
        return events
