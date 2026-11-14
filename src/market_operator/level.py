"""纯二维 (t, P) 时空势场与双视野(广角 Macro + 近景聚焦 Foveal)水平结构引擎 —— 零魔法参数。

核心数学与市场拍卖第一性原理(彻底废除一切 ATR 比例系数与一维滞后道氏计数):
1. 内生光学分辨率(Intrinsic Pixel Step σ_0):
   画布自身的最小自然价格尺度由相邻收盘价绝对跳动的中位数 σ_0 = median(|ΔC|) 唯一内生决定,
   不引入任何 0.35*ATR、0.30*ATR、0.80*ATR 等人为拍脑袋倍数。
2. 时间透视与双视野嵌套感知(Macro Peripheral + Meso Foveal 2D Vision):
   人类不会把两年的图等权画满,也不会只看一个月的外框而漏掉眼前的顺势台阶:
   - 广角画布(窗口 W)使用距离反比时间衰减核 w(Δt) = 1 / (1 + Δt / (W/4)),提取统治大格局的外包络与大颈线;
   - 近景中央凹画布(右侧局部窗口 W_near = max(24, W // 4))在近期局部价格跨度上独立归一化,
     敏锐提取过去数日正在推进的顺势中继平台与回踩阶梯!
3. 自标定反 POC 泥潭层析(Self-Calibrated Anti-POC Tomography):
   将画布价格轴投射为均匀网格计算每层价格的 K 线覆盖密度(TPO Occupancy),以画布自身的【中位 TPO 密度】
   作为客观分水岭:凡驻留密度超过画布中位数的高度均属于成交密集区肚子内部(Value Mud),自动剔除!
4. 二维时空状态直读(2D Balance-Imbalance Tensor):
   无需滞后的道氏两高两低计数,直接对比右侧近景重心序列与左侧背景四分位价值盒 [Q25, Q75] 的空间脱离度与纵向迁移方向,
   第一拍零滞后识别市场处于失衡趋势(TREND_UP / TREND_DOWN)、价值区回归转换(TRANSITION)还是箱体均衡(RANGE)。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

import numpy as np


_LEVEL_UID = itertools.count(1)


class LevelRole(str, Enum):
    SUPPORT = "support"
    RESISTANCE = "resistance"


class LevelKind(str, Enum):
    SWING = "SWING"        # 高二维留白统治面积的单波段边界
    EQUAL = "EQUAL"        # 同极性多顶点水平共线颈线(EQH / EQL)
    FLIPPED = "FLIPPED"    # 波峰+波谷跨极性水平共线颈线(已验证支阻互换轴)
    ORIGIN = "ORIGIN"      # 当前画布最高二维统治势能的战略起点边界


class LevelState(str, Enum):
    FRESH = "fresh"
    TESTED = "tested"
    FLIPPED = "flipped"
    CONSUMED = "consumed"


@dataclass
class Level:
    """二维时空场提取的活跃原子水平势垒(具备跨帧持久身份锚点)。"""

    uid: int
    role: LevelRole
    kind: LevelKind
    extreme: float
    neck: float
    state: LevelState
    origin_index: int
    known_index: int
    origin_ts: int
    last_change_known: int
    touches: int = 1
    flip_count: int = 0
    geometry_version: int = 1
    departed: bool = True
    prominence_2d: float = 0.0
    vacuum_ahead: float = 1.0  # 该势垒优势侧邻域的二维净空通畅度

    @property
    def price(self) -> float:
        return self.extreme

    @property
    def band_lo(self) -> float:
        return min(self.extreme, self.neck)

    @property
    def band_hi(self) -> float:
        return max(self.extreme, self.neck)

    @property
    def inner(self) -> float:
        return self.neck

    @property
    def outer(self) -> float:
        return self.extreme

    @property
    def lo(self) -> float:
        return self.band_lo

    @property
    def hi(self) -> float:
        return self.band_hi

    @property
    def origin_known(self) -> int:
        return self.known_index

    @property
    def source_kind(self) -> str:
        return self.kind.value

    @property
    def is_active(self) -> bool:
        return self.state in (LevelState.FRESH, LevelState.TESTED, LevelState.FLIPPED)

    @property
    def claim_id(self) -> tuple[tuple[int, int], str, int]:
        return ((self.origin_index, self.flip_count), self.role.value, self.geometry_version)

    def clone(self) -> "Level":
        return Level(
            uid=self.uid,
            role=self.role,
            kind=self.kind,
            extreme=self.extreme,
            neck=self.neck,
            state=self.state,
            origin_index=self.origin_index,
            known_index=self.known_index,
            origin_ts=self.origin_ts,
            last_change_known=self.last_change_known,
            touches=self.touches,
            flip_count=self.flip_count,
            geometry_version=self.geometry_version,
            departed=self.departed,
            prominence_2d=self.prominence_2d,
            vacuum_ahead=self.vacuum_ahead,
        )


def normalize_band(role: LevelRole, extreme: float, neck: float) -> tuple[float, float]:
    ext = float(extreme)
    nk = float(neck)
    eps = max(abs(ext) * 1e-6, 1e-9)
    if role is LevelRole.SUPPORT:
        nk = max(nk, ext + eps)
    else:
        nk = min(nk, ext - eps)
    return ext, nk


def nearest_opposing_level(
    levels: Sequence[Level],
    direction: int,
    ref_price: float,
    *,
    exclude_uid: Optional[int] = None,
) -> Optional[Level]:
    if direction > 0:
        cands = [
            lv for lv in levels
            if lv.is_active
            and lv.role is LevelRole.RESISTANCE
            and lv.uid != exclude_uid
            and lv.neck > ref_price
        ]
        return min(cands, key=lambda lv: lv.neck) if cands else None
    cands = [
        lv for lv in levels
        if lv.is_active
        and lv.role is LevelRole.SUPPORT
        and lv.uid != exclude_uid
        and lv.neck < ref_price
    ]
    return max(cands, key=lambda lv: lv.neck) if cands else None


def _extract_subcanvas_vertices(
    H: np.ndarray,
    L: np.ndarray,
    B_lo: np.ndarray,
    B_hi: np.ndarray,
    global_offset: int,
    px_unit: float,
) -> list[tuple[int, str, float, float, float, int]]:
    """在二维画布切片上向量化预筛并因果提取局部转折顶点及其时间透视二维留白面积。"""
    W = len(H)
    if W < 8:
        return []
    p_span = max(float(H.max() - L.min()), 1e-9)
    denom_t = max(4.0, W / 4.0)

    verts: list[tuple[int, str, float, float, float, int]] = []
    pk_mask = (H[1 : W - 1] >= H[0 : W - 2]) & (H[1 : W - 1] > H[2:W])
    for idx_m in np.flatnonzero(pk_mask):
        i = int(idx_m) + 1
        hi_i = float(H[i])
        if i >= 2 and hi_i < float(H[i - 2]):
            continue
        gt_right = np.flatnonzero(H[i + 1 :] > hi_i)
        r_cross = (i + 1 + int(gt_right[0])) if len(gt_right) > 0 else (W - 1)
        if (r_cross - i) >= 2:
            gt_left = np.flatnonzero(H[:i] > hi_i)
            l_cross = int(gt_left[-1]) if len(gt_left) > 0 else 0
            dp = hi_i - float(L[i : r_cross + 1].min())
            if dp > px_unit:
                tw = 1.0 / (1.0 + ((W - 1) - i) / denom_t)
                area = ((r_cross - l_cross) / W) * (dp / p_span) * tw
                nk = max(float(B_hi[i]), hi_i - px_unit)
                verts.append((global_offset + i, "PEAK", hi_i, nk, area, i))

    tr_mask = (L[1 : W - 1] <= L[0 : W - 2]) & (L[1 : W - 1] < L[2:W])
    for idx_m in np.flatnonzero(tr_mask):
        i = int(idx_m) + 1
        lo_i = float(L[i])
        if i >= 2 and lo_i > float(L[i - 2]):
            continue
        lt_right = np.flatnonzero(L[i + 1 :] < lo_i)
        r_cross = (i + 1 + int(lt_right[0])) if len(lt_right) > 0 else (W - 1)
        if (r_cross - i) >= 2:
            lt_left = np.flatnonzero(L[:i] < lo_i)
            l_cross = int(lt_left[-1]) if len(lt_left) > 0 else 0
            dp = float(H[i : r_cross + 1].max()) - lo_i
            if dp > px_unit:
                tw = 1.0 / (1.0 + ((W - 1) - i) / denom_t)
                area = ((r_cross - l_cross) / W) * (dp / p_span) * tw
                nk = min(float(B_lo[i]), lo_i + px_unit)
                verts.append((global_offset + i, "TROUGH", lo_i, nk, area, i))
    return verts


class Canvas2DLevelScanner:
    """无尺度二进制倍频程金字塔(32/64/128/256)二维时空势场、状态直读与内生强弱引擎。"""

    OCTAVE_WINDOWS: tuple[int, ...] = (40, 80, 160)

    def __init__(self, canvas_bars: int = 160, max_active_per_side: int = 8) -> None:
        self.canvas_bars = max(32, int(canvas_bars))
        self.max_active_per_side = max(3, int(max_active_per_side))
        self._registry: dict[tuple[int, str], Level] = {}
        self._active: list[Level] = []
        self._regime_2d: str = "range"
        self._imbalance_strength: float = 0.0
        self._px_unit: float = 0.0
        self._grid_p: Optional[np.ndarray] = None
        self._grid_tpo: Optional[np.ndarray] = None
        self._med_tpo: float = 0.0

    @property
    def active_levels(self) -> tuple[Level, ...]:
        return tuple(self._active)

    @property
    def regime_2d(self) -> str:
        return self._regime_2d

    @property
    def imbalance_strength(self) -> float:
        """纯 K 线内生导出的无量纲二维失衡强度(正=多头领涨相对强度,负=空头领跌相对弱度),跨任何市场通用。"""
        return self._imbalance_strength

    def path_vacuum_ratio(self, p_from: float, p_to: float) -> float:
        """积分计算从 p_from 到 p_to 路径上的二维净空通畅度(>1 表示比画布中位更畅通的历史流动性真空腔)。"""
        if self._grid_p is None or self._grid_tpo is None or self._med_tpo <= 0.0:
            return 1.0
        lo, hi = (p_from, p_to) if p_from <= p_to else (p_to, p_from)
        mask = (self._grid_p >= lo) & (self._grid_p <= hi)
        if not np.any(mask):
            return 1.0
        path_mud = float(np.mean(self._grid_tpo[mask]))
        return self._med_tpo / max(path_mud, 1e-6)

    def _compute_2d_regime(
        self, C: np.ndarray, B_mid: np.ndarray, W: int
    ) -> str:
        """二维时空状态直读(彻底取代滞后的道氏两高两低计数):
        对比右侧近景活动窗(宽度 round(sqrt(W)))与左侧背景价值四分位盒 [Q25, Q75]:
        - 脱离背景价值盒进入上方/下方净空且重心净迁移与路径同向 -> TREND_UP / TREND_DOWN;
        - 身处背景净空极值外但近景重心已反向折返 -> TRANSITION;
        - 身处背景价值盒 [Q25, Q75] 重叠区内 -> RANGE。
        """
        k_rec = max(6, int(round(float(np.sqrt(W)))))
        bg = B_mid[:-k_rec] if W > 2 * k_rec else B_mid
        bg_s = np.sort(bg)
        nb = len(bg_s)
        q25 = float(bg_s[nb // 4])
        q50 = float(bg_s[nb // 2])
        q75 = float(bg_s[(3 * nb) // 4])
        iqr = max(q75 - q25, self._px_unit, 1e-9)
        cur_c = float(C[-1])
        rec_c = C[-k_rec:]
        path_len = float(np.sum(np.abs(np.diff(rec_c))))
        net_disp = float(rec_c[-1] - rec_c[0])
        rw_baseline = 1.0 / np.sqrt(float(k_rec))
        eff = (net_disp / path_len) if path_len > 0.0 else 0.0
        # 无量纲二维相对强弱得分:偏离背景价值中枢的 IQR 倍数 × 方向迁移效率绝对值
        z_iqr = (cur_c - float(q50)) / iqr
        self._imbalance_strength = float(z_iqr * (0.5 + abs(eff)))

        if cur_c > q75:
            if eff >= rw_baseline:
                return "trend_up"
            if eff <= -rw_baseline:
                return "transition"
            return "trend_up" if net_disp > 0.0 else "range"
        if cur_c < q25:
            if eff <= -rw_baseline:
                return "trend_down"
            if eff >= rw_baseline:
                return "transition"
            return "trend_down" if net_disp < 0.0 else "range"
        return "range"

    def update_canvas(
        self, bars: Sequence[tuple[int, float, float, float, float, float]], atr: float = 0.0
    ) -> list[tuple[str, Level, Optional[Level]]]:
        n_total = len(bars)
        t_now = n_total - 1
        if n_total < 10:
            self._active = []
            self._regime_2d = "range"
            return []

        start = max(0, n_total - self.canvas_bars)
        win = bars[start:]
        W = len(win)

        O = np.fromiter((b[1] for b in win), dtype=np.float64, count=W)
        H = np.fromiter((b[2] for b in win), dtype=np.float64, count=W)
        L = np.fromiter((b[3] for b in win), dtype=np.float64, count=W)
        C = np.fromiter((b[4] for b in win), dtype=np.float64, count=W)
        B_hi = np.maximum(O, C)
        B_lo = np.minimum(O, C)
        B_mid = 0.5 * (B_hi + B_lo)

        dc = np.abs(np.diff(C))
        pos_dc = dc[dc > 0.0]
        p_min, p_max = float(L.min()), float(H.max())
        p_span = max(p_max - p_min, 1e-9)
        if len(pos_dc) > 0:
            dc_s = np.sort(pos_dc)
            px_unit = float(dc_s[len(dc_s) // 2])
        else:
            px_unit = p_span * 1e-3
        self._px_unit = px_unit

        # 1) 二维时空状态直读与内生相对强弱
        self._regime_2d = self._compute_2d_regime(C, B_mid, W)

        # 2) 向量化构建全画布二维 TPO 覆盖剖面:以上四分位密度 Q75 作为反泥潭分水岭
        grid_p = np.linspace(p_min, p_max, 64)
        grid_tpo = np.mean((L[None, :] <= grid_p[:, None]) & (H[None, :] >= grid_p[:, None]), axis=1)
        self._grid_p = grid_p
        self._grid_tpo = grid_tpo
        gt_s = np.sort(grid_tpo)
        self._med_tpo = float(gt_s[(3 * len(gt_s)) // 4])

        # 3) 双视野独立归一化融合:广角主画布(W) + 近景中央凹画布(右侧 W//4)取较显著者
        macro_verts = _extract_subcanvas_vertices(H, L, B_lo, B_hi, start, px_unit)
        w_near = min(W, max(24, W // 4))
        foveal_verts: list[tuple[int, str, float, float, float, int]] = []
        if W > w_near + 8:
            near_off = W - w_near
            raw_foveal = _extract_subcanvas_vertices(
                H[near_off:], L[near_off:], B_lo[near_off:], B_hi[near_off:], start + near_off, px_unit
            )
            foveal_verts = [
                (g_i, vk, ex, nk, sc, near_off + li)
                for (g_i, vk, ex, nk, sc, li) in raw_foveal
            ]

        by_gidx: dict[tuple[int, str], tuple[int, str, float, float, float, int]] = {}
        for v in macro_verts + foveal_verts:
            key = (v[0], v[1])
            prev = by_gidx.get(key)
            if prev is None or v[4] > prev[4]:
                by_gidx[key] = v
        verts = list(by_gidx.values())
        if not verts:
            self._active = []
            return []

        sc_arr = np.sort(np.fromiter((v[4] for v in verts), dtype=np.float64, count=len(verts)))
        med_area = float(sc_arr[len(sc_arr) // 2])
        verts.sort(key=lambda v: v[4], reverse=True)

        clusters: list[dict] = []
        for v in verts:
            g_idx, v_kind, ext, neck, area, local_i = v
            e_n, n_n = normalize_band(
                LevelRole.RESISTANCE if v_kind == "PEAK" else LevelRole.SUPPORT, ext, neck
            )
            lo, hi = min(e_n, n_n), max(e_n, n_n)
            placed = False
            for cl in clusters:
                c_lo, c_hi = cl["lo"], cl["hi"]
                if max(c_lo - px_unit, lo) <= min(c_hi + px_unit, hi):
                    cl["verts"].append(v)
                    cl["score"] += area
                    cl["last_local_i"] = max(cl["last_local_i"], local_i)
                    cl["first_local_i"] = min(cl["first_local_i"], local_i)
                    placed = True
                    break
            if not placed:
                clusters.append({
                    "anchor_idx": g_idx,
                    "anchor_ts": int(bars[g_idx][0]),
                    "anchor_kind": v_kind,
                    "lo": lo,
                    "hi": hi,
                    "verts": [v],
                    "score": area,
                    "first_local_i": local_i,
                    "last_local_i": local_i,
                })

        return self._project_active_levels(t_now, W, H, L, C, B_lo, B_hi, float(C[-1]), px_unit, med_area, clusters)

    def _has_saddle_void_between(self, p1: float, p2: float, L: np.ndarray, H: np.ndarray) -> bool:
        """二维拓扑鞍点分割:两条同侧候选线独立共存须具备真实二维波段留白间隔。"""
        gap = abs(p1 - p2)
        if gap < 2.0 * self._px_unit:
            return False
        mid_g = 0.5 * (p1 + p2)
        mid_occ = float(np.mean((L <= mid_g) & (H >= mid_g)))
        return gap >= 3.0 * self._px_unit or mid_occ <= self._med_tpo

    def _project_active_levels(
        self,
        t_now: int,
        W: int,
        H: np.ndarray,
        L: np.ndarray,
        C: np.ndarray,
        B_lo: np.ndarray,
        B_hi: np.ndarray,
        cur_c: float,
        px_unit: float,
        med_area: float,
        clusters: list[dict],
    ) -> list[tuple[str, Level, Optional[Level]]]:
        prev_by_anchor = {lv.origin_index: lv for lv in self._active}
        max_score = max((cl["score"] for cl in clusters), default=0.0)
        sup_cands: list[Level] = []
        res_cands: list[Level] = []
        transitions: list[tuple[str, Level, Optional[Level]]] = []

        for cl in clusters:
            touches = len(cl["verts"])
            score = float(cl["score"])
            if touches < 2 and score < med_area:
                continue

            lo, hi = float(cl["lo"]), float(cl["hi"])
            mid = 0.5 * (lo + hi)
            first_i = int(cl["first_local_i"])
            last_i = int(cl["last_local_i"])

            if (W - first_i) >= 12:
                occ = float(np.mean((L[first_i:] <= hi) & (H[first_i:] >= lo)))
                if occ > self._med_tpo:
                    continue

            span_c = C[first_i + 1 :]
            if len(span_c) > 0:
                sides = np.where(span_c > hi + px_unit, 1, np.where(span_c < lo - px_unit, -1, 0))
                nz = sides[sides != 0]
                if len(nz) >= 2 and int(np.sum(nz[1:] != nz[:-1])) >= 2:
                    continue

            if last_i + 1 < W:
                if int(np.sum((B_lo[last_i + 1 :] < lo) & (B_hi[last_i + 1 :] > hi))) >= 1:
                    continue
            self._append_level_candidate(
                cl, touches, score, max_score, lo, hi, mid, last_i, W, H, L, C, cur_c,
                t_now, prev_by_anchor, sup_cands, res_cands, transitions,
            )

        sup_cands.sort(key=lambda z: abs(cur_c - z.inner))
        res_cands.sort(key=lambda z: abs(cur_c - z.inner))
        self._active = sup_cands[: self.max_active_per_side] + res_cands[: self.max_active_per_side]
        return transitions

    def _append_level_candidate(
        self,
        cl: dict,
        touches: int,
        score: float,
        max_score: float,
        lo: float,
        hi: float,
        mid: float,
        last_i: int,
        W: int,
        H: np.ndarray,
        L: np.ndarray,
        C: np.ndarray,
        cur_c: float,
        t_now: int,
        prev_by_anchor: dict[int, Level],
        sup_cands: list[Level],
        res_cands: list[Level],
        transitions: list[tuple[str, Level, Optional[Level]]],
    ) -> None:
        anchor_idx = int(cl["anchor_idx"])
        prev_lv = prev_by_anchor.get(anchor_idx)

        if cur_c > hi:
            role = LevelRole.SUPPORT
        elif cur_c < lo:
            role = LevelRole.RESISTANCE
        else:
            if prev_lv is not None:
                role = prev_lv.role
            else:
                prev_c = float(C[-2]) if W >= 2 else cur_c
                role = LevelRole.SUPPORT if prev_c >= mid else LevelRole.RESISTANCE

        target_pool = sup_cands if role is LevelRole.SUPPORT else res_cands
        cand_neck = hi if role is LevelRole.SUPPORT else lo
        if any(not self._has_saddle_void_between(ex_c.neck, cand_neck, L, H) for ex_c in target_pool):
            return

        peak_idxs = [v[5] for v in cl["verts"] if v[1] == "PEAK"]
        trough_idxs = [v[5] for v in cl["verts"] if v[1] == "TROUGH"]
        has_peaks = bool(peak_idxs)
        has_troughs = bool(trough_idxs)
        pure_cross_flip = (
            (role is LevelRole.SUPPORT and has_peaks and not has_troughs)
            or (role is LevelRole.RESISTANCE and has_troughs and not has_peaks)
        )
        if pure_cross_flip:
            return

        is_flipped = False
        if has_peaks and has_troughs:
            # 时间严格分离极性互换判据(剔除区间内部波峰波谷来回交错混杂的拉锯伪 FLIPPED!):
            # - 真多头支阻互换(左顶右底):所有历史波峰严格发生在所有近期回踩波谷之前!
            # - 真空头阻支互换(左底右顶):所有历史波谷严格发生在所有近期反抽波峰之前!
            if role is LevelRole.SUPPORT and max(peak_idxs) < min(trough_idxs):
                is_flipped = True
            elif role is LevelRole.RESISTANCE and max(trough_idxs) < min(peak_idxs):
                is_flipped = True
            else:
                return
        if is_flipped:
            kind, state, flip_cnt = LevelKind.FLIPPED, LevelState.FLIPPED, 1
        elif touches >= 2:
            kind, state, flip_cnt = LevelKind.EQUAL, LevelState.TESTED, 0
        elif score >= 0.75 * max_score and max_score > 0.0:
            kind, state, flip_cnt = LevelKind.ORIGIN, LevelState.FRESH, 0
        else:
            kind, state, flip_cnt = LevelKind.SWING, LevelState.FRESH, 0

        ext = lo if role is LevelRole.SUPPORT else hi
        neck = hi if role is LevelRole.SUPPORT else lo
        departed = (
            bool(np.any(L[last_i + 1 :] > hi) if role is LevelRole.SUPPORT else np.any(H[last_i + 1 :] < lo))
            if (last_i + 1 < W)
            else False
        )
        vac = self.path_vacuum_ratio(neck, cur_c)

        reg_key = (anchor_idx, role.value)
        existing = self._registry.get(reg_key)
        if existing is not None:
            existing.kind = kind
            existing.state = state
            existing.extreme = ext
            existing.neck = neck
            existing.touches = touches
            existing.flip_count = flip_cnt
            existing.departed = departed
            existing.prominence_2d = score
            existing.vacuum_ahead = vac
            existing.last_change_known = t_now
            lv_obj = existing
        else:
            lv_obj = Level(
                uid=next(_LEVEL_UID),
                role=role,
                kind=kind,
                extreme=ext,
                neck=neck,
                state=state,
                origin_index=anchor_idx,
                known_index=t_now,
                origin_ts=int(cl["anchor_ts"]),
                last_change_known=t_now,
                touches=touches,
                flip_count=flip_cnt,
                geometry_version=1,
                departed=departed,
                prominence_2d=score,
                vacuum_ahead=vac,
            )
            self._registry[reg_key] = lv_obj

        if prev_lv is not None and prev_lv.role is not role:
            transitions.append(("FLIPPED", lv_obj.clone(), prev_lv.clone()))

        target_pool.append(lv_obj)


LevelBook = Canvas2DLevelScanner




