"""Render completed bounded risk search artifacts, without running new trials."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    out = parser.parse_args().output.resolve()
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    winners = json.loads((out / "winners.json").read_text(encoding="utf-8"))
    lines = ["# 300 USDT 回撤预算仓位研究", "", "日期：2026-10-30；币种BTC/DOGE/ETH/SOL；历史2024-10-15至2026-10-19。",
             "", "## 方法", "", "- 固定策略，仅搜索5种单笔风险×3种组合容量×2种周期容量，共30组，加旧动态档位基准。",
             "- 每档按dev收益冻结前三；失败后依冻结dev排名检查备选，首个通过即停止。没有穷举验证所有组合，因此候选不代表全网格或连续参数空间最优。",
             "- 通过要求：dev/test/full/full@1.5x均盈利，且每场景最大盯市回撤均不超过预算。dev/test各自300起步。",
             "- 原始首次运行只完成前三验证，均失败；第二次以--resume执行预先公开的备选规则，未改变网格或硬约束。详见stdout.log与resume.stdout.log。",
             "- 费用模型保留生产口径，历史资金费；压力场景手续费、滑点及有符号资金费各放大1.5倍一次。",
             "- 不模拟live_runner日亏损/订单频率闸门、动态选币、断线/API失败；没有修改生产配置。",
             "- 这些历史数据已经反复研究；验证只是稳定性诊断，不是从未接触过的样本外证据。",
             "", "## 候选", "", "|预算|候选|全样本终值|全样本回撤|测试段终值/回撤|压力终值/回撤|", "|---|---|---:|---:|---|---|"]
    for budget, key in winners.items():
        if key is None:
            lines.append(f"|{float(budget):.0%}|未找到|—|—|—|—|")
            continue
        r = results[key]
        f, t, s = (r[k] for k in ("full", "test", "stress"))
        lines.append(f"|{float(budget):.0%}|{key}|{f['equity_end']:.2f}|{f['max_drawdown']:.2%}|{t['equity_end']:.2f} / {t['max_drawdown']:.2%}|{s['equity_end']:.2f} / {s['max_drawdown']:.2%}|")
        config = r["config"]
        snippet = {"risk_frac": config["risk_frac"],
                   "dynamic_risk_tiers": config["dynamic_risk_tiers"] or [],
                   "portfolio_cap_frac": config["portfolio_cap_frac"],
                   "tf_cap_by_tf": config["tf_cap_by_tf"]}
        (out / f"candidate_{round(float(budget)*100)}pct.risk.json").write_text(
            json.dumps(snippet, indent=2), encoding="utf-8")
    for budget, key in winners.items():
        if key is None:
            continue
        r = results[key]
        f, c = r["full"], r["full"]["closed"]
        tail = f["concentration"]
        lines += ["", f"## {float(budget):.0%}预算候选明细", "", "```json", json.dumps(r["config"], indent=2), "```",
                  f"- 全样本{c['n']}笔已平仓，胜率{c['winrate']:.2%}，净PF={c['profit_factor']:.3f}，平均盈亏比={c['payoff_ratio']:.3f}。",
                  f"- 净利润{c['net']:+.2f}；期末未平仓{f['open_end']}笔。终值含浮盈及未结算资金费，不强平。",
                  f"- 去最大10笔：{tail['without_top_10_trades']['net']:+.2f}；去头部10%：{tail['remaining_90pct']['net']:+.2f}。这是原账本拆分，不是删除交易后重新复利。",
                  f"- dev终值{r['dev']['equity_end']:.2f}，回撤{r['dev']['max_drawdown']:.2%}。",
                  "", "|周期|笔数|净利润|胜率|PF|", "|---|---:|---:|---:|---:|"]
        for tf, b in f["by_tf"].items():
            pf = f"{b['profit_factor']:.3f}" if b['profit_factor'] is not None else "NA"
            wr = f"{b['winrate']:.2%}" if b['winrate'] is not None else "NA"
            lines.append(f"|{tf}|{b['n']}|{b['net']:+.2f}|{wr}|{pf}|")
    lines += ["", "## 已验证组合中的最高全样本收益（事后描述，非预注册选中者）", "",
              "以下只在已验证组合中比较，不代表所有31组都已完整验证；不替代上面的dev优先选择。", "",
              "|预算|组合|全样本终值|全样本回撤|压力回撤|", "|---|---|---:|---:|---:|"]
    for budget in winners:
        eligible = [(key, r) for key, r in results.items() if all(
            name in r and r[name]["max_drawdown"] <= float(budget) and r[name]["equity_end"] > 300
            for name in ("dev", "test", "full", "stress"))]
        if eligible:
            key, r = max(eligible, key=lambda item: item[1]["full"]["equity_end"])
            lines.append(f"|{float(budget):.0%}|{key}|{r['full']['equity_end']:.2f}|{r['full']['max_drawdown']:.2%}|{r['stress']['max_drawdown']:.2%}|")
    lines += ["", "## 使用注意", "", "- 候选片段不是完整配置，不能替换整个live.json。若未来采用固定风险率，必须清空dynamic_risk_tiers，否则动态档位优先生效。",
              "- 保持20倍杠杆上限和原capacity_mode；容量预算不是最大回撤保险。",
              "- 预算仅是历史筛选条件，不保证未来不超限；实盘闸门尚未完整纳入该回放。",
              "- 要寻找更高收益还需额外预注册实验/新数据；不能为100000终值不断放宽风险。",
              "- plan.json、shortlist.json、backup_order.json、results.json、winners.json保存完整研究轨迹；逐笔及权益CSV支持核查。", ""]
    (out / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(out / "REPORT.md")


if __name__ == "__main__":
    main()