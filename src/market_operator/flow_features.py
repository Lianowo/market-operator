"""v12 K线级订单流(delta)特征(docs/v12-protocol.md §2,冻结;全部因果)。

数据单元 = 收盘已知的 1h bar(tok = 收盘知时,与事件研究入场口径一致:
特征在 bar t 收盘即可知,供 t+1 开盘入场的测量层使用,无未来信息)。

字段语义(vision /fapi um kline):
- taker_buy_base_volume = 该 bar 主动买(taker buy)成交的基币量;
- 主动卖量 = volume − taker_buy_base_volume(余量即卖压)。

特征定义(v12-protocol §2 逐字):
- delta_vol_k   = 2·taker_buy − total(主动买卖差,基币量)
- delta_share_k = delta_vol / total ∈ [−1, 1]
- cvd_k(W)     = delta_vol 累计和的近 W 根均值化(∑delta 的 rolling mean,W ∈ {24,48})
- delta_z_k    = (delta_share − 近48均值) / 近48标准差(滚动窗含当根)

因果性:所有 rolling 窗口只含 [t−W+1, t] 历史(含当根),全部归约是先
cumsum/rolling 再取值,不存在引用未来 bar 的环节;截断前缀上的特征
与全长序列对应索引的特征逐值恒等(测试保证)。

warmup 声明(先于结果观察):rolling 统计不足 W 根时按 min_periods=1
(累计和天然无 warmup;z/share 用扩张窗均值/方差),由此前 W−1 根
为 warmup 区,如实声明;样本内的成熟度差异不变因果方向。
std 用 ddof=0(均值同窗总体方差)。bar 成交量 ≤ 0 在数据层已被
validate 拒绝;防御:total ≤ 0 ⇒ delta_share 记 0.0。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

CVD_WINDOWS = (24, 48)                 # v12-protocol §2(冻结)
Z_WINDOW = 48                          # delta_z 滚动窗(冻结)


def delta_vol(taker_buy_base_volume: np.ndarray, volume: np.ndarray) -> np.ndarray:
    """主动买卖差(基币量)= 2·taker_buy − total。逐 bar,自身即因果。"""
    tb = np.asarray(taker_buy_base_volume, dtype=np.float64)
    tot = np.asarray(volume, dtype=np.float64)
    return 2.0 * tb - tot


def delta_share(taker_buy_base_volume: np.ndarray, volume: np.ndarray) -> np.ndarray:
    """主动买卖差占比 = delta_vol / total ∈ [−1,1];total ≤ 0 ⇒ 0.0(防御)。"""
    dv = delta_vol(taker_buy_base_volume, volume)
    tot = np.asarray(volume, dtype=np.float64)
    out = np.zeros_like(dv)
    np.divide(dv, tot, out=out, where=tot > 0.0)
    return out


def cvd(taker_buy_base_volume: np.ndarray, volume: np.ndarray,
        window: int) -> np.ndarray:
    """CVD 均值化:delta_vol 累计和的近 `window` 根 rolling mean(含当根)。

    返回与 min_periods=1 的 rolling mean 一致(前缀扩张窗),前 window−1
    根属 warmup 区(如实声明,见模块 docstring)。
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    dv = pd.Series(delta_vol(taker_buy_base_volume, volume))
    return dv.cumsum().rolling(window, min_periods=1).mean().to_numpy(np.float64)


def delta_z(taker_buy_base_volume: np.ndarray, volume: np.ndarray,
            window: int = Z_WINDOW) -> np.ndarray:
    """delta_share 的近 `window` 根 z-score(含当根;无脑 z 无未来)。

    窗内 std = 0 ⇒ z 记 0.0(常数窗,z 未定义,0 是最保守可读值)。
    """
    if window < 2:
        raise ValueError("window must be >= 2")
    s = pd.Series(delta_share(taker_buy_base_volume, volume))
    mean = s.rolling(window, min_periods=1).mean()
    std = s.rolling(window, min_periods=1).std(ddof=0)
    z = (s - mean) / std.replace(0.0, np.nan)
    return z.fillna(0.0).to_numpy(np.float64)


def attach(df: pd.DataFrame) -> pd.DataFrame:
    """在带 taker_buy_base_volume 的 bar 帧上追加全部 v12 流特征列。

    输入契约: 列含 ts, open, high, low, close, volume, taker_buy_base_volume
    (fetch_binance_klines 落地格式);输出新增列:
    delta_vol, delta_share, cvd_24, cvd_48, delta_z。原列不动。
    """
    tb = df["taker_buy_base_volume"].to_numpy(np.float64)
    v = df["volume"].to_numpy(np.float64)
    out = df.copy()
    out["delta_vol"] = delta_vol(tb, v)
    out["delta_share"] = delta_share(tb, v)
    for w in CVD_WINDOWS:
        out[f"cvd_{w}"] = cvd(tb, v, w)
    out["delta_z"] = delta_z(tb, v)
    return out
