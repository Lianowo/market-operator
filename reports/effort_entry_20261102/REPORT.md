# 边沿量价投入/结果扩展研究

固定候选；dev后冻结选择，历史test不是新独立样本外。

| 方案 | dev 1x | test 1x | full 1x | full DD 1x | full 1.5x |
|---|---:|---:|---:|---:|---:|
| baseline | +324.8481 | +156.9056 | +650.3108 | 14.2403% | +357.6290 |
| accept_only | +298.6830 | +120.0471 | +542.2089 | 18.5552% | +384.6078 |
| 15m_price | +388.7577 | +79.6839 | +571.7346 | 22.1542% | +345.5948 |
| 15m_effort | +289.2025 | +95.0559 | +470.4237 | 21.8007% | +258.5870 |
| 15m_effort_add | +330.0666 | +131.5510 | +613.0547 | 18.4648% | +318.0079 |
| 30m_price | +333.9888 | +119.1849 | +580.7238 | 17.9157% | +263.0195 |
| 30m_effort | +275.0885 | +99.6240 | +464.5329 | 19.7995% | +272.1540 |
| 30m_effort_add | +314.2484 | +133.0248 | +601.0611 | 15.2928% | +282.9415 |

## Dev选择
```json
{
  "status": "complete",
  "gates": {
    "15m_price": [
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
        "net": true,
        "drawdown": false,
        "noise": true,
        "both_dev_halves": true
      }
    ],
    "15m_effort": [
      {
        "cost": 1.0,
        "sample": true,
        "net": false,
        "drawdown": false,
        "noise": true,
        "both_dev_halves": false
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
    "15m_effort_add": [
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
        "noise": true,
        "both_dev_halves": false
      }
    ],
    "30m_price": [
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
        "noise": true,
        "both_dev_halves": false
      }
    ],
    "30m_effort": [
      {
        "cost": 1.0,
        "sample": true,
        "net": false,
        "drawdown": true,
        "noise": true,
        "both_dev_halves": false
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
    "30m_effort_add": [
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

仅OHLCV代理，不代表真实吸收。共享权益/容量反馈，净额差不是成交量因果效应。
触边延迟从触边K线开盘计算。保守MFE代理不等于真实假信号率。未部署。
