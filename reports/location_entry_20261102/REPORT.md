# 上层位置约束增量入口研究

仅dev比较全部候选；只对dev选中者作历史test/full复核。

| dev方案 | 净额1x | DD1x | 净额1.5x | 新成交1x |
|---|---:|---:|---:|---:|
| baseline | +324.8481 | 9.3520% | +206.3510 | 0 |
| first30_all_add | +341.8961 | 10.9429% | +198.5880 | 30 |
| first30_not_counter_add | +288.5304 | 9.9431% | +179.8973 | 13 |
| first30_range_add | +329.0234 | 9.5413% | +207.2854 | 2 |
| reclaim15_all_add | +381.5358 | 14.4485% | +195.5024 | 106 |
| reclaim15_not_counter_add | +386.3805 | 14.3741% | +222.3450 | 79 |
| reclaim15_range_add | +384.2621 | 10.3363% | +254.5024 | 12 |
| floor15_all_add | +352.9873 | 12.8631% | +171.3926 | 154 |
| floor15_not_counter_add | +389.1695 | 13.3687% | +205.7243 | 111 |
| floor15_range_add | +316.6235 | 10.2235% | +201.8468 | 17 |

```json
{
  "status": "complete",
  "gates": {
    "first30_all_add": [
      {
        "cost": 1.0,
        "sample": true,
        "net": true,
        "drawdown": false,
        "noise": true,
        "both_dev_halves": true
      },
      {
        "cost": 1.5,
        "sample": true,
        "net": false,
        "drawdown": false,
        "noise": true,
        "both_dev_halves": false
      }
    ],
    "first30_not_counter_add": [
      {
        "cost": 1.0,
        "sample": false,
        "net": false,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": false
      },
      {
        "cost": 1.5,
        "sample": false,
        "net": false,
        "drawdown": true,
        "noise": false,
        "both_dev_halves": false
      }
    ],
    "first30_range_add": [
      {
        "cost": 1.0,
        "sample": false,
        "net": true,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": false
      },
      {
        "cost": 1.5,
        "sample": false,
        "net": true,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": false
      }
    ],
    "reclaim15_all_add": [
      {
        "cost": 1.0,
        "sample": true,
        "net": true,
        "drawdown": false,
        "noise": true,
        "both_dev_halves": true
      },
      {
        "cost": 1.5,
        "sample": true,
        "net": false,
        "drawdown": false,
        "noise": true,
        "both_dev_halves": false
      }
    ],
    "reclaim15_not_counter_add": [
      {
        "cost": 1.0,
        "sample": true,
        "net": true,
        "drawdown": false,
        "noise": true,
        "both_dev_halves": true
      },
      {
        "cost": 1.5,
        "sample": true,
        "net": true,
        "drawdown": false,
        "noise": true,
        "both_dev_halves": true
      }
    ],
    "reclaim15_range_add": [
      {
        "cost": 1.0,
        "sample": false,
        "net": true,
        "drawdown": false,
        "noise": true,
        "both_dev_halves": false
      },
      {
        "cost": 1.5,
        "sample": false,
        "net": true,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": false
      }
    ],
    "floor15_all_add": [
      {
        "cost": 1.0,
        "sample": true,
        "net": true,
        "drawdown": false,
        "noise": true,
        "both_dev_halves": true
      },
      {
        "cost": 1.5,
        "sample": true,
        "net": false,
        "drawdown": false,
        "noise": true,
        "both_dev_halves": false
      }
    ],
    "floor15_not_counter_add": [
      {
        "cost": 1.0,
        "sample": true,
        "net": true,
        "drawdown": false,
        "noise": false,
        "both_dev_halves": true
      },
      {
        "cost": 1.5,
        "sample": true,
        "net": false,
        "drawdown": false,
        "noise": false,
        "both_dev_halves": false
      }
    ],
    "floor15_range_add": [
      {
        "cost": 1.0,
        "sample": false,
        "net": false,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": false
      },
      {
        "cost": 1.5,
        "sample": false,
        "net": false,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": false
      }
    ]
  },
  "eligible": [],
  "dev_selected": null
}
{
  "candidate": null,
  "gates": [],
  "passed": false,
  "deploy": false
}
```

反复研究存在选择偏差，历史test不是新独立样本外。未部署。
