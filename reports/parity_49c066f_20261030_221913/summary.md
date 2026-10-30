# Exact strategy parity against 49c066f

Result: PASS. Exit code: 0. Differences: {}.
Reference: 49c066fed7bb2946d92529a6465380db41113a2d.
Current: uncommitted workspace source.
Command: python D:\LiAn\Web3Code\market-operator\tools\verify_strategy_parity.py --ref 49c066f
Symbols: BTCUSDT,DOGEUSDT,ETHUSDT,SOLUSDT.

The implementations ran in separate interpreters using shared local market data,
funding rates and contract specifications. The tool verified JSON equality of the
current strategy configuration and reference configuration. SHA-256 comparisons
include every trade field, list order, the full equity series and all counters,
without rounding floating-point output.

| Scenario | Trade records | Trades | Equity | Counters |
| --- | ---: | --- | --- | --- |
| dev@1 | 443 | MATCH | MATCH | MATCH |
| dev@1.5 | 442 | MATCH | MATCH | MATCH |
| test@1 | 230 | MATCH | MATCH | MATCH |
| test@1.5 | 227 | MATCH | MATCH | MATCH |
| full@1 | 673 | MATCH | MATCH | MATCH |
| full@1.5 | 669 | MATCH | MATCH | MATCH |

Trade record counts include open trades and differ from closed-trade counts in
the earlier profitability replay report. This verifies only the covered local
data, configuration and six scenarios, not all possible inputs or live exchange
interactions. No strategy source, configuration or frozen baseline was changed;
no live trading process was started.

Additional checks: test_strategy_parity.py: 5 passed in 0.84s; git diff --check passed.
Artifacts: comparison.json, parity.stdout.log, parity.stderr.log, result.json,
and Git status snapshots. Logs contain fingerprints, not raw trade details.
The reference source temporary directory was cleaned up by the tool.
Original PowerShell redirected logs use UTF-16; comparison.json and this summary
use UTF-8.


Pre-commit validation: full pytest suite: 440 passed, 1 skipped in 20.40s. Working-tree patch snapshot is losslessly gzip-compressed.
