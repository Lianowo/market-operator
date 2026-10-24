import json
from pathlib import Path

p = json.loads(Path("reports/scale_20261024/scale.json").read_text(encoding="utf-8"))
arms = p["arms"]
rows = []
for k, v in arms.items():
    name, seg, fund, cm = k.split("__")
    rows.append((name, seg, fund, v))

print("| arm | seg | funding | N | net | E[R] | DD% | giveBack | ticks | per-TF |")
print("|---|---|---|---|---|---|---|---|---|---|")
for name, seg, fund, v in rows:
    tf = " ".join(f"{t}:{d['net']:+.1f}" for t, d in v["by_tf"].items())
    gb = v["tail"]["give_back_r_mean"]
    gbs = f"{gb:.3f}" if gb is not None else "n/a"
    print(f"| {name} | {seg} | {fund} | {v['n']} | {v['net']:+.1f} | "
          f"{v['e_r']:+.4f} | {v['dd_pct']:.2f} | {gbs} | "
          f"{v['exit_exec_ticks']} | {tf} |")

print()
print("## 每笔成本结构")
for name, seg, fund, v in rows:
    if seg == "dev" and fund == "history" and ("_x" not in name):
        n = max(v["n"], 1)
        print(f"{name:22s} n={n:5d} cost/trade={v['cost'] / n:6.3f} "
              f"fund/trade={v['funding_total'] / n:6.3f} "
              f"net/trade={v['net'] / n:7.3f}")
print()
print("## Δ vs 同 arm 无 xexec(同 seg/funding)")


base = {(n.replace("_xexec", "").replace("_xlock", ""), s, f): v
        for (n, s, f, v) in rows if "_xexec" not in n and "_xlock" not in n}
for name, seg, fund, v in rows:
    if "_xexec" not in name and "_xlock" not in name:
        continue
    key = (name.replace("_xexec", "").replace("_xlock", ""), seg, fund)
    b = base.get(key)
    if b:
        print(f"{name:24s} {seg:5s} {fund:8s} dNet={v['net'] - b['net']:+8.1f} "
              f"dER={v['e_r'] - b['e_r']:+.4f} dDD={v['dd_pct'] - b['dd_pct']:+.2f}")
