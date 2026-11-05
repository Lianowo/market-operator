"""诊断：实验配置是否真的把 2h 挂上了 30m 触发层。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from market_operator.scenario import ScenarioRunner  # noqa: E402

cfg = ROOT / "reports/exp_2h_trigger_30m/strategy.2h30m.json"
runner = ScenarioRunner(("BTCUSDT",), config_path=cfg, progress=False)
print("trigger_seconds_by_timeframe =", runner.settings.trigger_seconds_by_timeframe)
for key, frame in runner.frames.items():
    mic = getattr(frame, "mic", None)
    print(key, "mic=", None if mic is None else {
        "trigger_tf_seconds": mic.trigger_tf_seconds,
        "n_events": len(getattr(mic, "events", []) or []),
        "attrs": [a for a in dir(mic) if not a.startswith("_")][:12],
    })
