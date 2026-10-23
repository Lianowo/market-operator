"""dev/test 分界:生产契约(不是研究工具)。

为什么单独成模块
----------------
``split_boundary`` 曾经住在 ``research.py``(555 行的事件研究方法学)里,但它是
**回放与战役的判分契约**:``replay`` 用它切认知帧、``campaign`` 用它决定某个
OPEN 决策是否落在 dev 段。研究脚本可以归档,这条契约不能跟着一起进档案柜。

把它放在 20 行的纯函数模块里,生产闭包就再也不必 import 研究方法学模块——
``tests/test_layering.py`` 把这条边界变成断言。
"""

from __future__ import annotations


def split_boundary(n_bars: int, dev_frac: float) -> int:
    """dev/test 分界 bar 索引:``known_index < boundary`` 为 dev,否则 test。

    只做一次乘法取整;``dev_frac`` 由调用方从配置读取(生产配置 0.7)。
    """
    return int(n_bars * dev_frac)
