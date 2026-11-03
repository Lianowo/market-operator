# 固定量价早入场对照

规则已在运行前冻结；没有参数遍历。

| 方案 | dev 1x净额 | test 1x净额 | full 1x净额 | full 1.5x净额 | full DD 1x | 新入口数1x |
|---|---:|---:|---:|---:|---:|---:|
| baseline | +324.8481 | +156.9056 | +650.3108 | +357.6290 | 14.2403% | 0 |
| accept_only | +298.6830 | +120.0471 | +542.2089 | +384.6078 | 18.5552% | 0 |
| first_price | +349.5419 | +113.2906 | +600.5352 | +380.3620 | 17.8208% | 41 |
| first_volume | +335.5541 | +102.7688 | +547.4449 | +362.6807 | 20.6340% | 21 |
| second_price | +285.3141 | +148.0184 | +567.1025 | +351.8367 | 13.5157% | 30 |
| second_volume | +303.9945 | +118.4840 | +544.4674 | +370.4444 | 20.1410% | 11 |

## Dev选择

```json
{
  "status": "complete",
  "gates": {
    "first_price": [
      {
        "cost": 1.0,
        "sample": true,
        "net": true,
        "drawdown": true,
        "noise": true
      },
      {
        "cost": 1.5,
        "sample": true,
        "net": true,
        "drawdown": false,
        "noise": true
      }
    ],
    "first_volume": [
      {
        "cost": 1.0,
        "sample": false,
        "net": true,
        "drawdown": true,
        "noise": true
      },
      {
        "cost": 1.5,
        "sample": false,
        "net": true,
        "drawdown": true,
        "noise": true
      }
    ],
    "second_price": [
      {
        "cost": 1.0,
        "sample": true,
        "net": false,
        "drawdown": true,
        "noise": true
      },
      {
        "cost": 1.5,
        "sample": true,
        "net": false,
        "drawdown": true,
        "noise": true
      }
    ],
    "second_volume": [
      {
        "cost": 1.0,
        "sample": false,
        "net": false,
        "drawdown": true,
        "noise": true
      },
      {
        "cost": 1.5,
        "sample": false,
        "net": false,
        "drawdown": true,
        "noise": true
      }
    ]
  },
  "eligible": [],
  "dev_selected": null,
  "fallback": "baseline"
}
```

历史test被重复使用，不是新独立样本外。full是连续账户，不是dev与test收益之和。
成本/容量与权益反馈会改变交易集合；不能把量能组与价格组金额差当成纯成交量因果效应。
MFE是保守OHLC近似，退出15m仅用退出价；失败代理不等于真实假信号率。
未建模真实盘口吸收、Delta或CVD；没有修改生产策略。
