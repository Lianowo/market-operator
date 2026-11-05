"""诊断：降档触发层到底产出了什么（事件数 / 计数器 / 被过滤在哪）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from market_operator.scenario import ScenarioRunner  # noqa: E402

runner = ScenarioRunner(("BTCUSDT",), progress=False)
for key, frame in runner.frames.items():
    mic = getattr(frame, "mic", None)
    if mic is None:
        print(key, "mic=None")
        continue
    n_events = sum(len(v) for v in getattr(mic, "events_by", {}).values())
    kinds = {}
    for evs in getattr(mic, "events_by", {}).values():
        for e in evs:
            kinds[e.id] = kinds.get(e.id, 0) + 1
    print(key, "trigger", mic.trigger_tf_seconds, "bars", len(mic.bars),
          "cog_bars", len(mic.cog_index_of), "events", n_events, kinds,
          "counters", getattr(mic, "counters", None))
