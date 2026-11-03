# 固定入场风险尺度消融

| 方案 | dev 1x | test 1x | full 1x | full DD 1x | full 1.5x |
|---|---:|---:|---:|---:|---:|
| baseline | +324.8481 | +156.9056 | +650.3108 | 14.2403% | +357.6290 |
| accept_only | +298.6830 | +120.0471 | +542.2089 | 18.5552% | +384.6078 |
| 15m_price_floor | +325.9084 | +81.4152 | +493.9805 | 20.5576% | +150.6116 |
| 15m_price_add_floor | +352.9873 | +123.4954 | +616.5813 | 22.3145% | +226.6128 |
| 15m_effort_floor | +303.7442 | +96.9438 | +507.2956 | 17.3276% | +240.7603 |
| 15m_effort_add_floor | +324.9419 | +130.7835 | +612.1842 | 17.8821% | +274.7602 |
| 30m_price_floor | +284.9739 | +96.1599 | +467.3509 | 18.4434% | +186.9807 |
| 30m_price_add_floor | +311.4097 | +149.0435 | +615.0346 | 16.8392% | +249.7564 |
| 30m_effort_floor | +240.6626 | +101.3633 | +426.8993 | 18.0486% | +249.8983 |
| 30m_effort_add_floor | +288.3437 | +138.7985 | +562.9826 | 14.9460% | +263.1280 |

```json
{
  "status": "complete",
  "gates": {
    "15m_price_floor": [
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
    "15m_price_add_floor": [
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
    "15m_effort_floor": [
      {
        "cost": 1.0,
        "sample": true,
        "net": false,
        "drawdown": false,
        "noise": false,
        "both_dev_halves": false
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
    "15m_effort_add_floor": [
      {
        "cost": 1.0,
        "sample": true,
        "net": true,
        "drawdown": false,
        "noise": true,
        "both_dev_halves": false
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
    "30m_price_floor": [
      {
        "cost": 1.0,
        "sample": true,
        "net": false,
        "drawdown": false,
        "noise": false,
        "both_dev_halves": false
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
    "30m_price_add_floor": [
      {
        "cost": 1.0,
        "sample": true,
        "net": false,
        "drawdown": false,
        "noise": false,
        "both_dev_halves": false
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
    "30m_effort_floor": [
      {
        "cost": 1.0,
        "sample": true,
        "net": false,
        "drawdown": true,
        "noise": false,
        "both_dev_halves": false
      },
      {
        "cost": 1.5,
        "sample": true,
        "net": false,
        "drawdown": true,
        "noise": false,
        "both_dev_halves": false
      }
    ],
    "30m_effort_add_floor": [
      {
        "cost": 1.0,
        "sample": true,
        "net": false,
        "drawdown": true,
        "noise": false,
        "both_dev_halves": false
      },
      {
        "cost": 1.5,
        "sample": true,
        "net": false,
        "drawdown": false,
        "noise": false,
        "both_dev_halves": false
      }
    ]
  },
  "eligible": [],
  "dev_selected": null
}
```

风险下限改变初始止损与仓位，不改变后续2h管理。历史test非新独立样本外。未部署。
