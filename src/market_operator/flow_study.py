"""v12 判别主实验计算核心(docs/v12-protocol.md §2-§3,冻结)。

对 (事件×方向) 桶按触发时 delta_z 三瓜分切片,比较最高/最低分位的
"net@1x H24 相对盘中对照的增量"之差:

  diff = mean(incr | top tercile) − mean(incr | bottom tercile)

其中 incr_i = event net_atr@1x(H=24)_i − ctrl_net_atr_mean@1x_i
(增量口径与 v1-v3/v9 完全一致;对照 = §9.1-5 同机制排除窗、42 固定种子,
在 run_flow_study 内由 research.measure_controls 原样产生)。

统计:两组独立日块 block bootstrap(与 research.block_bootstrap_ci 同日块
语义;top/bottom 两组各自重抽,差为统计量),2000 次、固定种子 ⇒ 95% CI。
CI 不含 0 ⇒ 该桶"delta_z 强度分位显著改变事件后期望"(v12-protocol §3
H1 判别单元);全部桶 CI 含 0 ⇒ 切不出差异,H0(bar 层流不解决拥挤)。

纯函数,无网络无文件;确定性由显式 rng 种子保证(测试守护)。
"""

from __future__ import annotations

import numpy as np

from .research import BOOT_REPS, MS_PER_DAY

MIN_TERCILE_N = 6            # 三瓜分切片最小桶量(每分位 ≥2;写死在先)
DIFF_BOOT_SEED = 2026_1020   # v12 判别 bootstrap 固定种子基(v12 专属,非 §9 常量)


def tercile_labels(delta_z: np.ndarray, order_key: np.ndarray) -> np.ndarray:
    """按 (delta_z 升序, order_key 升序) 秩三瓜分,返回每件样本的分位标签。

    标签: 0 = 最低分位(bottom), 1 = 中, 2 = 最高分位(top)。
    order_key 作确定性平局裁决(调用方传事件 ts;同 z 早者排前)。
    切点: bottom = 秩 [0, n//3), top = 秩 [n − n//3, n) —— n//3 = 1 时每分位
    恰好 1 件;n // 3 = 0(n < 3)时 top/bottom 为空,标签全 1。
    """
    z = np.asarray(delta_z, dtype=np.float64)
    k = np.asarray(order_key, dtype=np.int64)
    if len(z) != len(k):
        raise ValueError("delta_z / order_key length mismatch")
    n = len(z)
    order = np.lexsort((k, z))
    labels = np.ones(n, dtype=np.int64)
    third = n // 3
    if third == 0:
        return labels
    labels[order[:third]] = 0
    labels[order[n - third:]] = 2
    return labels


def _day_block_draws(values: np.ndarray, ts_ms: np.ndarray,
                     rng: np.random.Generator, reps: int) -> np.ndarray:
    """单日块 bootstrap 均值统计量的全部抽样(与 research 同日块聚合语义)。"""
    vals = np.asarray(values, dtype=np.float64)
    days = np.asarray(ts_ms, dtype=np.int64) // MS_PER_DAY
    uniq, inverse = np.unique(days, return_inverse=True)
    n_days = len(uniq)
    day_sums = np.bincount(inverse, weights=vals)
    day_counts = np.bincount(inverse).astype(np.float64)
    draws = rng.integers(0, n_days, size=(reps, n_days))
    return day_sums[draws].sum(axis=1) / day_counts[draws].sum(axis=1)


def tercile_diff_ci(top_incr: np.ndarray, top_ts: np.ndarray,
                    bot_incr: np.ndarray, bot_ts: np.ndarray,
                    reps: int = BOOT_REPS,
                    seed: int = DIFF_BOOT_SEED) -> tuple:
    """top/bottom 分位增量之差的点估计与 95% CI(两组独立日块 bootstrap)。

    两组来自同一桶的互斥子集,抽样各自独立;一个 rng 顺序产生 top 再
    bottom 的抽样(种子固定 ⇒ 完全确定)。空组 ⇒ NaN(桶量不足)。
    """
    if len(top_incr) == 0 or len(bot_incr) == 0:
        return (float("nan"), float("nan"), float("nan"))
    diff = float(np.mean(top_incr) - np.mean(bot_incr))
    rng = np.random.default_rng(seed)
    top_stats = _day_block_draws(top_incr, top_ts, rng, reps)
    bot_stats = _day_block_draws(bot_incr, bot_ts, rng, reps)
    stats = top_stats - bot_stats
    return (diff, float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5)))
