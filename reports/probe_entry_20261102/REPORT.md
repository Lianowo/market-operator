# 低风险试探入口研究

仅dev比较全部候选；只对dev选中者作历史test/full复核。

| dev方案 | 净额1x | DD1x | 净额1.5x | 新成交1x |
|---|---:|---:|---:|---:|
| baseline | +324.8481 | 9.3520% | +206.3510 | 0 |
| first30_all_q1_add | +311.2125 | 9.2225% | +202.8497 | 30 |
| first30_all_q2_add | +318.1372 | 9.3609% | +195.8488 | 30 |
| reclaim15_not_counter_q1_add | +294.8630 | 10.2206% | +196.6296 | 83 |
| reclaim15_not_counter_q2_add | +314.9277 | 11.7940% | +207.6917 | 83 |

```json
{
  "status": "complete",
  "gates": {
    "first30_all_q1_add": [
      {
        "cost": 1.0,
        "sample": true,
        "net": false,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": true
      },
      {
        "cost": 1.5,
        "sample": true,
        "net": false,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": false
      }
    ],
    "first30_all_q2_add": [
      {
        "cost": 1.0,
        "sample": true,
        "net": false,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": true
      },
      {
        "cost": 1.5,
        "sample": true,
        "net": false,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": false
      }
    ],
    "reclaim15_not_counter_q1_add": [
      {
        "cost": 1.0,
        "sample": true,
        "net": false,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": true
      },
      {
        "cost": 1.5,
        "sample": true,
        "net": false,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": false
      }
    ],
    "reclaim15_not_counter_q2_add": [
      {
        "cost": 1.0,
        "sample": true,
        "net": false,
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
