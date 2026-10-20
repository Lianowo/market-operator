"""v11 区域发育履历(docs/v11-protocol.md,冻结 2026-10-20):跨 episode 区域发育的归因测量。

命题:"过程"长在区域的履历上,不在单次 episode 里——区域被反复触碰时,防守是
衰减(被摸烂)还是累积(供给愈堆愈厚)。本模块只做测量(事件研究口径),不改
events/structure 任何行为:对每 (symbol, 1h) 重放结构层,登记每区域的每次触碰
(A2/A3 语义)及其履历快照(全部因果),前瞻量仅用于标注。

解释性决定(协议文字未完全展开处,取最符合因果语义与冻结 §4/§6A 语义的读法):
- [§1 触碰单元 = A3 接近过程] 一行履历 = **一次接近过程(dwell-run)**:自 A2
  进入 bar(前一根收盘在正面之外且本 bar 区间与带相交,§6A-A2/A1)起,至首次
  整根脱离带的连续段。冻结 `Zone.touches` 是 **per-bar** 计数(悬停段内每根
  A2 都增量);而协议字段 dwell_k("本次 episode 连续滞留 bar 数")以"本次
  episode"为单位,若逐 bar 建行,同一 episode 会产出多行且 dwell 逐行递减,
  与语义不自洽 ⇒ 每段接近只建一行,k 见下;悬停段内的 per-bar 增量只反映在
  `touches_frozen`(该行触碰 bar 收盘后的冻结 touches 值,§4.3 事实,供生产
  假设层元数据交叉引用,§3 路由接入时区龄/触碰口径以此注释为准)。
  "段开/段合"在事件层 A3 上做了测量化:段合于首次整根脱离带(正面或远侧
  皆可,附 exit_side;事件层纯 A3 只以离 ACTIVE/发射收 episode,本层为履历
  测量取价格几何段,文档化差异,不宣称等同;v9 探针已证事件层 episode 几乎
  全部 1-3 根内收场,两读法在真实数据上差异有界)。
- [§1 k 序数] k = **身份链上履历行序数**(接近过程计次;origin 出生反应为 1,
  §4.1"市场曾经在那里给出过明确反应";每段接近 +1;合并并集把双亲链行数相
  加续计;FLIPPED 反向新区为新身份重新起计——正反面互换了,接近语义从头
  算起)。`touches_frozen` 并列为冻结语义读数。
- [§1 origin 行] 全新 pivot 生成的独立区域登记 origin 行:k=1,
  touch_index = origin_known(区域知时 bar),无 A2 接近过程 ⇒ dwell/pen NaN,
  "触碰结束"即该 bar 本身(out 窗自 t+1)。被并集吸收的出生不单独建行(其
  "第 1 触"并入链行数,见上;冻结 touches 照含)。
- [触及语义] 沿 A2 精确定义。不复制语义——以观察者子类 `_BioZoneBook` 读取
  结构层自身 A2 增量的事实,保证与冻结判定逐位一致(测试断言重放 parity:
  观察者对结构行为零扰动)。本层不检测破位/事件,只读状态机结果。
- [§1 episode 标注] dwell_k = 自触入 bar 起、bar 区间仍与(触碰时刻快照)带
  相交的连续根数;结束于首个区间整体离开带的 bar(两侧任一侧;协议原文"首
  次整根脱离正面侧",普通(被拒绝)路径即正面侧离开,贯穿路径为远侧离开)。
  pen_k = 滞留窗 [t, t+dwell) 内向**外缘**刺入幅度(阻力:max high − z.hi;
  支撑镜像)/ atr(touch),符号保留(未达外缘为负 = 浅触)。dwell/pen 与 out
  同为标注类(触碰 bar 尚不可知),只用本触碰自身窗口内数据。
- [§1 out 前瞻] out 窗 = 触碰结束后 24 根 [t_end, t_end+24),t_end = t+dwell
  (origin 行 t_end = t+1)。拒绝方向 = 破位方向的反方向(阻力:向下);位移
  锚 = 外缘(阻力 z.hi / 支撑 z.lo,即被破边缘):out_rj_k 阻力 = (z.hi − 窗
  min low)/atr(touch);out_br_k 阻力 = (窗 max high − z.hi)/atr(touch),支撑
  镜像。数据末端窗不足按可得根数(n_fwd 记录),0 根 → NaN(不入相关/分桶均值)。
- [终止方式] 区域身份 = 结构层 zone 对象的连续体(§4.1 合并并集沿 A3"同一
  区域的扩张"续接为同一身份;FLIPPED 反向新区为**新身份**,经冻结继承
  (§4.2)touches/origin 延续为冻结事实,touches_frozen/age 随之延续)。终止
  ∈ {BROKEN, FLIPPED, DEAD, ALIVE}:ALIVE = 存活至数据末仍 ACTIVE;BROKEN =
  破位后至数据末仍未了结(含 RETESTED 悬挂);FLIPPED/DEAD 于各自发生 bar。
  破位事实(broken_index,首次 ACTIVE→BROKEN)沿身份独立记录。
- [24h 破位率] broken_24h = 身份链自触碰 bar 起首次破位 ∈ [t, t_end+24):含
  "这一摸就破"与滞留期破,窗与 out 对齐;存活率 = 1 − 破位率(协议 §2 次判别)。
- [身份链] 合并并集 ⇒ 被并入各区身份的 successor 指向并集身份(union-find
  链);破位/终止解析沿链:自触碰所属身份向链头收集 broken_index ≥ t 的最早者。
- [§2 判别] 主判别 Spearman(k 与 log k 各自对 out_rj_k)+ 自然日(UTC)分块
  block bootstrap CI(2000 次,种子 = research.BOOT_SEED,与既有研究同座);
  判定族只在 dev 段(dev_frac 同既有拐点,research.split_boundary);test 段复
  报同表一次性(v11 唯一一次,docs/v11-protocol.md §2)。数据单元 =
  zone-touch(接近过程行),跨品种合池(日块 = 跨品种同一自然日,保留跨品种
  同日相关性)。
- [k_* 断点] 候选 {2,3},只许 dev 判定:separation = median(out_rj | k ≤ c) −
  median(out_rj | k > c),取 separation 较大者;并列取较小 c。判定后冻结,
  test 不再择优(协议 §3"不再择优上 test")。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .research import BOOT_SEED, MS_PER_DAY
from .structure import (
    MarketStructure,
    Params,
    ZoneBook,
    ZoneRole,
    ZoneState,
)

H_FORWARD = 24                       # 协议 §1 out 前瞻根数(主视界,先验 N_time)
BREAK_WINDOW = 24                    # 协议 §2 存活率窗口(24h 内 acceptance 破位)
K_STAR_CANDIDATES = (2, 3)           # 协议 §3 k_* 候选集合(dev 判定,冻结)
BOOT_REPS = 2000                     # 协议 §2 自助次数(与 research.BOOT_REPS 同值)

TERMINATIONS = ("BROKEN", "FLIPPED", "DEAD", "ALIVE")


# ---------------------------------------------------------------------------
# 观察者 ZoneBook(只读事实登记;对结构层行为零改动,tests 断言 parity)
# ---------------------------------------------------------------------------


class _BioZoneBook(ZoneBook):
    """ZoneBook 观察者子类:登记 A2 触碰事实与合并/生成/翻转插入的因果痕迹。

    不复制、不修改任何生命周期判定;仅读取基类自身已完成的增量与状态转移
    (`z.touches` 增量 = A2 触碰成立;`_insert_merged` 前后对象集合差 =
    并集/吸收)。日志由 collect_biography 逐 bar 消费。
    """

    def __init__(self, params: Params) -> None:
        super().__init__(params)
        self.touch_log: list = []    # [(zone_object, index, touches_post_increment)]
        self.insert_log: list = []   # [(inserted_object, absorbed_ids, known)]

    def _advance(self, z, index, h, l, c, prev_close, atr_val) -> None:  # noqa: ANN001
        before = z.touches
        super()._advance(z, index, h, l, c, prev_close, atr_val)
        if z.touches > before:                   # A2 触碰成立(基类已增量)
            self.touch_log.append((z, index, z.touches))

    def _insert_merged(self, new, known, atr_val: float = 0.0) -> None:  # noqa: ANN001
        before = {id(z) for z in self.zones}
        super()._insert_merged(new, known, atr_val)
        after = {id(z) for z in self.zones}
        inserted = [z for z in self.zones if id(z) in (after - before)]
        assert len(inserted) == 1, "ZoneBook._insert_merged 应恰好追加一个对象"
        self.insert_log.append((inserted[0], before - after, known))


# ---------------------------------------------------------------------------
# 区域身份履历(链式;登记层全因果)
# ---------------------------------------------------------------------------


@dataclass
class ZoneBio:
    """一条区域身份履历(合并并集续接 successor;FLIPPED 反向新区开新身份)。"""

    bio_id: int
    role: ZoneRole
    born_index: int                # 身份入场 bar(pivot 知时 / 并集 bar / 翻转 bar)
    origin_known: int              # 冻结 origin_known(§4.1/§4.2 继承)
    via_flip: bool = False         # 由 FLIPPED 反向继承生成
    parents: tuple = ()            # 被并入的本链前驱 bio_id(并集)
    n_rows: int = 0                # 本链已建行数(k 序数来源)
    successor: Optional[int] = None
    broken_index: Optional[int] = None   # 本段身份首次 ACTIVE→BROKEN 的 bar
    end_index: Optional[int] = None
    termination: Optional[str] = None    # BROKEN/FLIPPED/DEAD/ALIVE


@dataclass
class ZoneTouch:
    """一次接近过程(触碰)履历行:k/age/role/merged/regime 因果;episode/out 为标注。"""

    symbol: str
    touch_index: int               # 接近过程首 bar(A2);origin 行 = origin_known bar
    ts: int
    bio_id: int
    k: int                         # 身份链履历行序数(A3 接近计次)
    touches_frozen: int            # 触碰 bar 收盘后冻结 Zone.touches(per-bar,§4.3)
    role: str
    merged: int
    regime: str
    age_days: float
    lo: float                      # 触碰时刻带快照(dwell/pen 基准)
    hi: float
    atr_touch: float
    origin: bool = False           # k=1 origin 行(无 A2 接近过程)
    dwell_k: Optional[int] = None
    pen_k: Optional[float] = None
    exit_side: Optional[str] = None      # front / far / data_end(origin 行 None)
    out_rj_k: Optional[float] = None
    out_br_k: Optional[float] = None
    n_fwd: Optional[int] = None
    broken_24h: Optional[bool] = None
    zone_end_state: Optional[str] = None
    zone_end_index: Optional[int] = None


def _chain_members(bios: list, bio_id: int) -> list:
    """自 bio_id 至链头的身份段列表(时间单调)。"""
    out = []
    b = bios[bio_id]
    while True:
        out.append(b)
        if b.successor is None:
            return out
        b = bios[b.successor]


def _chain_head(bios: list, bio_id: int) -> ZoneBio:
    return _chain_members(bios, bio_id)[-1]


# ---------------------------------------------------------------------------
# 履历登记(pass 1:因果重放;pass 2:episode/out 标注)
# ---------------------------------------------------------------------------


def collect_biography(df: pd.DataFrame, params: Params, symbol: str = "") -> tuple:
    """重放结构层,返回 (touches_df, zones_df)。

    touches_df:每接近过程一行(列 = ZoneTouch 字段);zones_df:每身份链头一行。
    """
    ms = MarketStructure(params)
    book = _BioZoneBook(params)
    ms._zones = book                           # 观察者注入(语义零改动,测试断言)

    ts_a = df["ts"].to_numpy(dtype=np.int64)
    h_a = df["high"].to_numpy(dtype=float)
    l_a = df["low"].to_numpy(dtype=float)
    n = len(df)
    vol = (df["volume"].astype(float).tolist() if "volume" in df.columns
           else [1.0] * n)
    bars = list(zip(ts_a.tolist(), df["open"].astype(float).tolist(),
                    h_a.tolist(), l_a.tolist(),
                    df["close"].astype(float).tolist(), vol))

    bios: list[ZoneBio] = []
    id2bio: dict = {}                          # zone 对象 id -> bio_id(含历史)
    live_bio_of: dict = {}                     # bio_id -> 在场链头 zone 对象
    open_run: dict = {}                        # bio_id -> (lo, hi) 未了接近段(带快照)
    touches: list[ZoneTouch] = []

    for t in range(n):
        ms.update(bars[t])
        atr = ms.atr()
        if atr <= 0.0:                         # ATR 未就绪:结构层全 no-op(§2)
            continue

        # (b0) 未了接近段收盘判定:本 bar 区间不再与段带相交 ⇒ 段合
        for bio_id, (rlo, rhi) in list(open_run.items()):
            if l_a[t] > rhi or h_a[t] < rlo:
                del open_run[bio_id]

        # (a) 插入(pivot 生成 / 并集 / 翻转继承):身份登记
        for inserted, absorbed_ids, known in book.insert_log:
            assert known == t, "插入知时应为当前 bar"
            parents = tuple(sorted(id2bio[a] for a in absorbed_ids))
            via_flip = any(
                fl.zone.role == inserted.role
                and fl.zone.lo >= inserted.lo - 1e-12
                and fl.zone.hi <= inserted.hi + 1e-12
                for fl in book.flips)
            bio = ZoneBio(bio_id=len(bios), role=inserted.role,
                          born_index=t, origin_known=inserted.origin_known,
                          via_flip=via_flip, parents=parents,
                          n_rows=sum(bios[p].n_rows for p in parents))
            bios.append(bio)
            id2bio[id(inserted)] = bio.bio_id
            live_bio_of[bio.bio_id] = inserted
            # 并集承接未了接近段(A3 合并续绑:同一区域扩张,接近过程随行)
            for pid in parents:
                bios[pid].successor = bio.bio_id
                live_bio_of.pop(pid, None)
                if pid in open_run:
                    open_run[bio.bio_id] = open_run.pop(pid)
            if not parents and not via_flip:
                # 全新 pivot 生成的独立区域:origin 触碰 k=1(§4.1)
                bio.n_rows += 1
                touches.append(ZoneTouch(
                    symbol=symbol, touch_index=t, ts=int(ts_a[t]),
                    bio_id=bio.bio_id, k=bio.n_rows,
                    touches_frozen=inserted.touches, role=inserted.role.value,
                    merged=inserted.merged, regime=ms.regime().value,
                    age_days=0.0, lo=inserted.lo, hi=inserted.hi,
                    atr_touch=atr, origin=True))
        book.insert_log = []

        # (b) A2 触碰:未在段的 ⇒ 开新接近行(A3 接近计次);在段的 ⇒ per-bar 增量不开行
        for z, index, tf in book.touch_log:
            assert index == t, "触碰知时应为当前 bar"
            bio = bios[id2bio[id(z)]]
            if bio.bio_id in open_run:
                continue                       # 悬停段内增量(frozen 照计,不开行)
            bio.n_rows += 1
            open_run[bio.bio_id] = (z.lo, z.hi)
            touches.append(ZoneTouch(
                symbol=symbol, touch_index=t, ts=int(ts_a[t]),
                bio_id=bio.bio_id, k=bio.n_rows, touches_frozen=tf,
                role=z.role.value, merged=z.merged,
                regime=ms.regime().value,
                age_days=(int(ts_a[t]) - int(ts_a[bio.origin_known])) / MS_PER_DAY,
                lo=z.lo, hi=z.hi, atr_touch=atr, origin=False))
        book.touch_log = []

        # (c) 破位事实与终止(对象离场 = FLIPPED/DEAD;并集离场已在 (a) 续接)
        for z in book.zones:
            if z.state is ZoneState.BROKEN and z.last_change_known == t:
                bio = bios[id2bio[id(z)]]
                if bio.broken_index is None:
                    bio.broken_index = t
        live_ids = {id(z) for z in book.zones}
        for bio_id, z in list(live_bio_of.items()):
            if id(z) in live_ids:
                continue
            bio = bios[bio_id]
            if bio.successor is not None:
                del live_bio_of[bio_id]
                continue                     # 并入并集(succ 在 (a) 处理)
            assert z.state in (ZoneState.FLIPPED, ZoneState.DEAD), \
                f"身份离场但非终止态(absorbed 未登记):state={z.state}"
            bio.end_index = t
            bio.termination = ("FLIPPED" if z.state is ZoneState.FLIPPED
                               else "DEAD")
            del live_bio_of[bio_id]
            open_run.pop(bio_id, None)

    # 数据末:仍在场的身份段定终止(存活 vs 破位悬挂)
    for bio_id, z in live_bio_of.items():
        bio = bios[bio_id]
        bio.end_index = n - 1
        bio.termination = "ALIVE" if z.state is ZoneState.ACTIVE else "BROKEN"

    # ---- pass 2:episode/前瞻标注(只用各触碰自身窗口内数据) ----------------
    for r in touches:
        head = _chain_head(bios, r.bio_id)
        r.zone_end_state = head.termination
        r.zone_end_index = head.end_index
        res = r.role == ZoneRole.RESISTANCE.value
        edge = r.hi if res else r.lo
        if r.origin:
            t_end = r.touch_index + 1
        else:
            j = r.touch_index
            while j < n and l_a[j] <= r.hi and h_a[j] >= r.lo:
                j += 1
            t_end = j
            r.dwell_k = j - r.touch_index
            seg_h = h_a[r.touch_index:t_end]
            seg_l = l_a[r.touch_index:t_end]
            pen_px = (seg_h.max() - edge) if res else (edge - seg_l.min())
            r.pen_k = float(pen_px / r.atr_touch)
            if t_end >= n:
                r.exit_side = "data_end"
            elif res:
                r.exit_side = "far" if l_a[t_end] > r.hi else "front"
            else:
                r.exit_side = "far" if h_a[t_end] < r.lo else "front"
        w_end = min(t_end + H_FORWARD, n)
        r.n_fwd = w_end - t_end
        if r.n_fwd > 0:
            wmax = float(h_a[t_end:w_end].max())
            wmin = float(l_a[t_end:w_end].min())
            r.out_rj_k = float(((edge - wmin) if res else (wmax - edge))
                               / r.atr_touch)
            r.out_br_k = float(((wmax - edge) if res else (edge - wmin))
                               / r.atr_touch)
        broken_from_t = [b.broken_index for b in _chain_members(bios, r.bio_id)
                         if b.broken_index is not None
                         and b.broken_index >= r.touch_index]
        b0 = min(broken_from_t) if broken_from_t else None
        r.broken_24h = bool(b0 is not None and b0 < t_end + BREAK_WINDOW)

    touches_df = pd.DataFrame([vars(r) for r in touches])
    zones_df = pd.DataFrame([{
        "bio_id": b.bio_id, "role": b.role.value, "born_index": b.born_index,
        "origin_known": b.origin_known, "via_flip": b.via_flip,
        "parents": b.parents, "n_rows": b.n_rows, "successor": b.successor,
        "broken_index": b.broken_index, "end_index": b.end_index,
        "termination": b.termination,
    } for b in bios if b.successor is None])
    return touches_df, zones_df


# ---------------------------------------------------------------------------
# 统计(协议 §2):Spearman + 自然日分块 bootstrap CI
# ---------------------------------------------------------------------------


def _avg_ranks(v: np.ndarray) -> np.ndarray:
    """平均秩(结内取均值;与 scipy.stats.rankdata 同约定,无 scipy 依赖)。"""
    v = np.asarray(v, dtype=np.float64)
    uniq, inv, counts = np.unique(v, return_inverse=True, return_counts=True)
    csum = np.cumsum(counts)
    avgs = (csum - counts) + (counts - 1.0) / 2.0
    return avgs[inv]


def spearman(x, y) -> float:
    """Spearman 秩相关(平均秩 + Pearson;常数输入 → NaN)。"""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if len(x) < 2 or np.ptp(x) == 0.0 or np.ptp(y) == 0.0:
        return float("nan")
    rx = _avg_ranks(x)
    ry = _avg_ranks(y)
    rx = rx - rx.mean()
    ry = ry - ry.mean()
    denom = float(np.sqrt((rx * rx).sum() * (ry * ry).sum()))
    if denom == 0.0:
        return float("nan")
    return float((rx * ry).sum() / denom)


def bootstrap_spearman_ci(x, y, ts_ms, reps: int = BOOT_REPS,
                          seed: int = BOOT_SEED) -> tuple:
    """自然日(UTC)分块 block bootstrap 的 Spearman 95% CI(协议 §2)。

    日块为抽样单元(块内触碰全体随块走),每次重复 = 有放回抽 n_days 个日块
    后合池重算 Spearman;CI = 重复分布的 [2.5, 97.5] 分位。返回 (点估计, CI)。
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    days = np.asarray(ts_ms, dtype=np.int64) // MS_PER_DAY
    point = spearman(x, y)
    uniq, inverse = np.unique(days, return_inverse=True)
    n_days = len(uniq)
    if n_days < 2 or point != point:
        return point, (float("nan"), float("nan"))
    members = [np.flatnonzero(inverse == d) for d in range(n_days)]
    rng = np.random.default_rng(seed)
    stats = np.empty(reps)
    for i in range(reps):
        draw = rng.integers(0, n_days, size=n_days)
        idx = np.concatenate([members[d] for d in draw])
        stats[i] = spearman(x[idx], y[idx])
    stats = stats[~np.isnan(stats)]
    if len(stats) < reps // 2:
        return point, (float("nan"), float("nan"))
    return point, (float(np.percentile(stats, 2.5)),
                   float(np.percentile(stats, 97.5)))


# ---------------------------------------------------------------------------
# 分桶表(协议 §2 次判别:k × role;区龄混缠复核)
# ---------------------------------------------------------------------------

K_BUCKET_LABELS = ("1", "2", "3", "4", "≥5")   # 协议 §2 k 分桶
AGE_BINS_D = (1.0, 7.0)                        # 区龄分桶:≤1d / 1-7d / 7+d


def age_label(age_days: float) -> str:
    if age_days <= AGE_BINS_D[0]:
        return "≤1d"
    if age_days <= AGE_BINS_D[1]:
        return "1-7d"
    return "7+d"


def _subset(g0: pd.DataFrame, kl: str) -> pd.DataFrame:
    return g0[g0["k"] >= 5] if kl == "≥5" else g0[g0["k"] == int(kl)]


def bucket_table(touches: pd.DataFrame) -> pd.DataFrame:
    """k × role 分桶:n、out_rj/out_br 中位与均值、24h 破位率、存活率。"""
    rows = []
    for role in ("all", ZoneRole.RESISTANCE.value, ZoneRole.SUPPORT.value):
        g0 = touches if role == "all" else touches[touches["role"] == role]
        for kl in K_BUCKET_LABELS:
            g = _subset(g0, kl)
            o_rj = g["out_rj_k"].dropna()
            o_br = g["out_br_k"].dropna()
            brk = g["broken_24h"]
            rows.append({
                "role": role, "bucket": kl, "n": int(len(g)),
                "out_rj_med": float(o_rj.median()) if len(o_rj) else None,
                "out_rj_mean": float(o_rj.mean()) if len(o_rj) else None,
                "out_br_med": float(o_br.median()) if len(o_br) else None,
                "out_br_mean": float(o_br.mean()) if len(o_br) else None,
                "break24_rate": float(brk.mean()) if len(brk) else None,
                "survive24_rate": float(1.0 - brk.mean()) if len(brk) else None,
                "dwell_med": float(g["dwell_k"].dropna().median())
                if g["dwell_k"].notna().any() else None,
                "pen_med": float(g["pen_k"].dropna().median())
                if g["pen_k"].notna().any() else None,
            })
    return pd.DataFrame(rows)


def age_cross_table(touches: pd.DataFrame) -> pd.DataFrame:
    """区龄 × k 混缠复核:out_rj 中位/均值(协议交付:区龄分桶复核混缠)。"""
    rows = []
    age = touches["age_days"].map(age_label)
    for al in ("≤1d", "1-7d", "7+d"):
        g0 = touches[age == al]
        for kl in K_BUCKET_LABELS:
            g = _subset(g0, kl)
            o = g["out_rj_k"].dropna()
            rows.append({
                "age": al, "bucket": kl, "n": int(len(g)),
                "out_rj_med": float(o.median()) if len(o) else None,
                "out_rj_mean": float(o.mean()) if len(o) else None,
            })
    return pd.DataFrame(rows)


def decide_k_star(dev_touches: pd.DataFrame,
                  candidates: tuple = K_STAR_CANDIDATES) -> dict:
    """k_* 断点 dev 判定(协议 §3,冻结规则,不许挪):

    separation(c) = median(out_rj | k ≤ c) − median(out_rj | k > c),取大者;
    并列取较小 c。"新鲜区防得住"对应 separation > 0(低 k 拒绝位移更大)。
    """
    out = {}
    for c in candidates:
        fresh = dev_touches[dev_touches["k"] <= c]["out_rj_k"].dropna()
        stale = dev_touches[dev_touches["k"] > c]["out_rj_k"].dropna()
        entry = {"n_fresh": int(len(fresh)), "n_stale": int(len(stale))}
        if len(fresh) == 0 or len(stale) == 0:
            entry.update(med_fresh=None, med_stale=None, separation=None)
        else:
            entry.update(med_fresh=float(fresh.median()),
                         med_stale=float(stale.median()),
                         separation=float(fresh.median() - stale.median()))
        out[str(c)] = entry
    valid = {int(c): v["separation"] for c, v in out.items()
             if v["separation"] is not None}
    k_star = (min(candidates) if not valid
              else max(valid, key=lambda c: (valid[c], -c)))
    return {"candidates": out, "k_star": int(k_star)}
