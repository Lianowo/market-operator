"""分层栅栏:生产闭包不允许出现"退役模块",模块名不允许带版本号。

为什么需要这个测试
------------------
仓库里同时住着两类代码:在跑的策略,和为了某一轮 A/B 而写、跑完就该冻结的
实验件。它们混在同一个包里,靠人记;于是"这个模块还活着吗"要靠翻 import
才发现——v14-v20 的臂现在只由研究脚本与历史测试引用(`lifecycle`/`tiered`),
另有一些从未进过生产(`strategy_v8`/`zonebio`/`aggtape`…)。靠记忆区分必然出错,
所以把它变成断言:

1. 生产闭包(回放入口 ``scenario`` + 实盘入口 ``scripts/live_runner.py`` 的传递
   依赖)**不得** import 退役模块。谁要把退役臂重新接进生产,必须先改这份清单
   ——这正是一次应当被看见的决定。
2. 生产闭包里的模块名不得含版本号(``v6``/``v21``/``v13``…)。版本是协议文档
   的名字,不是代码的名字;要留住历史,写进 docstring 与 ``docs/``。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "market_operator"
PRODUCTION_ENTRY_POINTS = (
    SRC / "scenario.py",                     # 回放/验收的唯一门面
    REPO_ROOT / "scripts" / "live_runner.py",  # 实盘唯一入口
)

# 已退役:只被研究脚本与各自的历史测试引用,不参与任何生产路径。
# 判定依据是 import 反向依赖(见提交说明),不是"看起来像旧版本"。
#
# 两条边界值得说清楚:
# - `lifecycle`/`tiered` 原先由 scripts/live_runner.py(实盘入口)直接 import;
#   v21 单链化之后实盘只剩 MarketRouter + 对侧锁利 PositionManager,它们退回
#   scripts/run_v19.py / run_v20.py 与历史测试。
# - `harvest`/`quality`/`repairs` 仍由 campaign 传递 import(v18 决策语境钩子供
#   历史路由复用),属于当前代码的组成部分,所以**不在此清单**。
RETIRED = frozenset({
    "lifecycle",     # scripts/run_v20.py + tests/test_v20_lifecycle.py
    "tiered",        # scripts/run_v19.py / run_v20.py + tests/test_v19_tiered.py
    "strategy_v8",   # scripts/run_backtest_v8.py + tests/test_v8.py
    "zonebio",       # scripts/run_zonebio.py + tests/test_zonebio.py
    "quattro",       # tests/test_quattro.py(v7 逐字复刻对照,只留历史断言)
    "aggtape",       # v19-orderflow 研究件
    "bandside",      # scripts/run_v15.py + tests/test_v15.py
    "balance",       # scripts/_diag_balance.py(被 balance_detect 取代)
    "flow_features",  # v12 订单流研究件
    "flow_study",
    # tape_features:2026-10-24 随唯一 importer(scripts/diagnose_current_btc.py)
    # 一起归档到 scripts/archive/ ⇒ 已从本清单移除(零引用不留在退役名单里)。
})

_VERSION_IN_NAME = re.compile(r"(^|_)v\d+(_|$)")


def _internal_imports(path: Path) -> set[str]:
    """该模块 import 的包内模块(``from .x import`` 与 ``market_operator.x``)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module:
                out.add(node.module.split(".")[0])
            elif node.level == 0 and node.module == "market_operator":
                # ``from market_operator import quattro`` —— 名字本身就是模块
                out.update(alias.name.split(".")[0] for alias in node.names)
            elif node.level == 0 and node.module and \
                    node.module.startswith("market_operator."):
                out.add(node.module.split(".")[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("market_operator."):
                    out.add(alias.name.split(".")[1])
    return out


def _production_closure() -> set[str]:
    graph = {p.stem: _internal_imports(p) for p in SRC.glob("*.py")}
    seen: set[str] = set()
    stack: list[str] = []
    for entry in PRODUCTION_ENTRY_POINTS:
        # 入口本身可能不在 src 里(scripts/),此时直接把它的依赖当根
        if entry.stem in graph:
            stack.append(entry.stem)
        stack.extend(_internal_imports(entry))
    while stack:
        name = stack.pop()
        if name in seen or name not in graph:
            continue
        seen.add(name)
        stack.extend(dep for dep in graph[name] if dep not in seen)
    return seen


def test_production_never_imports_retired_modules():
    closure = _production_closure()
    offenders = sorted(
        f"{mod} -> {dep}"
        for mod in closure
        for dep in _internal_imports(SRC / f"{mod}.py")
        if dep in RETIRED)
    assert not offenders, (
        "退役模块被生产闭包引用(要么把依赖搬回生产模块,要么从生产路径摘掉):"
        f" {offenders}")
    assert not (closure & RETIRED), f"退役模块进了生产闭包: {sorted(closure & RETIRED)}"


def test_production_module_names_carry_no_version():
    """生产模块以规则命名;版本号留在协议文档与 docstring 里。"""
    bad = sorted(m for m in _production_closure() if _VERSION_IN_NAME.search(m))
    assert not bad, f"生产模块名含版本号: {bad}"


def test_research_methodology_is_not_in_production_closure():
    """research.py 是研究方法学(事件研究 / GO-NO-GO),不是生产依赖。

    `split_boundary` 是回放与战役共用的判分契约,已上收到 `split.py`;若有人图
    省事又写回 `from .research import ...`,这条断言会立刻失败。研究模块可以
    随轮次归档,但"哪段 bar 算 dev"不能跟着归档。
    """
    closure = _production_closure()
    assert "research" not in closure, (
        f"研究方法学进了生产闭包: {sorted(closure)}")
    assert "split" in closure, "生产闭包必须直接依赖 split.py(判分契约)"


def test_retired_list_has_no_orphans():
    """退役 ≠ 无主:清单里每一项都必须还有引用方(研究脚本或历史测试)。

    完全没有引用的模块不是"退役",是垃圾——下一步应当删除,而不是留在清单里
    自我安慰。这条断言就是那个提醒。
    """
    referenced: set[str] = set()
    for folder in ("tests", "scripts", "tools"):
        for path in (REPO_ROOT / folder).glob("*.py"):
            referenced |= _internal_imports(path)
    missing = sorted(m for m in RETIRED if not (SRC / f"{m}.py").exists())
    assert not missing, f"清单里的模块已不存在: {missing}"
    orphans = sorted(m for m in RETIRED if m not in referenced)
    assert not orphans, f"无人引用的退役模块(可直接删除): {orphans}"


def test_live_path_is_reachable_from_the_live_entry_point():
    """实盘入口的传递闭包必须包含撮合与记账(防止有人把 live 路径改断)。"""
    closure = _production_closure()
    for module in ("broker", "bitget_live", "position", "events", "hypothesis"):
        assert module in closure, f"实盘入口没连到 {module}"
