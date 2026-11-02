# Session research data archive - 2026-11-02

Historical results only. Research implementations and raw generated artifacts are removed after this archive is verified. See the companion Chinese findings documents for interpretation.

## Scope and limitations

Four symbols and four cognition timeframes; canonical timing studies use 300 USDT and the existing risk/cost settings. Segments are independent accounts, not additive. Existing test data was reused and repeatedly inspected. No independent validation or global optimum is claimed. No failed candidate was deployed.

This archive preserves summary metrics, category results, failed gates, matching diagnostics, and source hashes. It does NOT preserve every raw event or trade, or make the removed experiments directly reproducible. Source paths below are historical provenance, not existing files.

The separate 400-USDT compounding study is archived in `compounding-400-results-20261102.md`, including original scenario summary fields, category/monthly results and provenance. Its raw output directory was also removed; the existing general-purpose compounding tool remains.

## Source hashes

| Historical source | SHA256 |
|---|---|
| entry_timing_replay_20261102/summary.json | 7f6f05aa61822c9eae1315a19391b73910d51dfca94a76bd788bad8f23a8daeb |
| attempt_confirmation_replay_20261102/summary.json | ba61dad2a1878c6d6d74a7c6917ed64b2b45faea17584bed3db2fd389fa2e3f7 |
| exit_timing_20261102/summary.json | 5e818c53d213f655b7e997b89b157840e05e3068401353460bdb9917f60ed958 |
| exit_contract_refactor_20261102/summary.json | da5c2ce52a2b4b0bdb6cbbc644053e2711b0bba33559f240ad9db04e12567c42 |
| exit_contract_audit_20261102/summary.json | 75f91ef4e61a603903d352602721a2fd1df252a81c672071c60dcaf55ca0a7f8 |
| entry_evidence_20261102/entry_evidence.json | f1a21bfb3855139559e1dbbd5df5a610ddf3dd4675d3121cdf2bda78259d6ac8 |
| early_entry_shadow_20261102/summary.json | 1008cda3e84eac0db0aae91b5c05589edf1ef235cc41546618d833fbac6f318d |
| structure_entry_audit_20261102.json | 082c8e312a066eb440e299bc813a0d374b8c03844b3aaedb79c8353323f81993 |
| structure_entry_decoupled_20261102.json | 105213cca8d3d6329bda91b874e48830ab44bd2d3d6fc51287be19ec8012908d |

## entry_timing_replay_20261102

| Arm | Scene | N | Net USDT | Gross USDT | Cost diagnostic | Max DD % | Win rate | PF |
|---|---|---|---|---|---|---|---|---|
| legacy_1h | dev/1.0 | 424 | 363.7918 | 425.7893 | 80.5552 | 8.9621 | 0.4741 | 1.7874 |
| legacy_1h | dev/1.5 | 419 | 216.2612 | 331.335 | 150.6011 | 10.6441 | 0.4749 | 1.7082 |
| legacy_1h | test/1.0 | 214 | 159.0406 | 189.6714 | 39.8937 | 12.9648 | 0.4533 | 1.9993 |
| legacy_1h | test/1.5 | 212 | 95.015 | 154.8022 | 78.3265 | 17.5411 | 0.4528 | 1.8656 |
| legacy_1h | full/1.0 | 640 | 715.7364 | 846.4263 | 170.0432 | 12.922 | 0.4656 | 1.8687 |
| legacy_1h | full/1.5 | 632 | 374.0988 | 593.467 | 287.214 | 21.6008 | 0.4668 | 1.7576 |
| baseline_15m | dev/1.0 | 428 | 320.3191 | 381.9072 | 80.1178 | 9.3886 | 0.465 | 1.7033 |
| baseline_15m | dev/1.5 | 424 | 205.9972 | 321.652 | 151.3531 | 10.198 | 0.467 | 1.6808 |
| baseline_15m | test/1.0 | 221 | 157.6895 | 189.1726 | 41.0149 | 13.1989 | 0.4661 | 1.9815 |
| baseline_15m | test/1.5 | 218 | 101.1968 | 162.949 | 80.9326 | 18.9218 | 0.4679 | 1.9113 |
| baseline_15m | full/1.0 | 650 | 647.0151 | 774.736 | 166.2402 | 13.8993 | 0.4646 | 1.8148 |
| baseline_15m | full/1.5 | 642 | 354.3923 | 572.9362 | 286.1956 | 24.7825 | 0.4642 | 1.7362 |
| reject_15m | dev/1.0 | 514 | 140.6841 | 214.4374 | 96.1757 | 11.9876 | 0.4086 | 1.358 |
| reject_15m | dev/1.5 | 508 | -5.7309 | 121.2564 | 166.4758 | 25.6629 | 0.4035 | 1.2484 |
| reject_15m | test/1.0 | 259 | 124.1698 | 169.8377 | 59.7568 | 15.573 | 0.4247 | 1.6441 |
| reject_15m | test/1.5 | 255 | 46.2533 | 134.5569 | 115.8981 | 20.6955 | 0.4196 | 1.5518 |
| reject_15m | full/1.0 | 772 | 324.9551 | 466.1485 | 184.4304 | 15.7692 | 0.4145 | 1.47 |
| reject_15m | full/1.5 | 762 | 43.2987 | 257.1866 | 280.5304 | 25.6629 | 0.4094 | 1.354 |
| accept_15m | dev/1.0 | 511 | 343.8308 | 419.5271 | 98.8415 | 12.2152 | 0.4442 | 1.6253 |
| accept_15m | dev/1.5 | 507 | 203.9748 | 343.9987 | 183.7332 | 14.1912 | 0.4438 | 1.5937 |
| accept_15m | test/1.0 | 247 | 138.9855 | 172.54 | 43.6452 | 17.1254 | 0.4534 | 1.8516 |
| accept_15m | test/1.5 | 244 | 76.8201 | 142.5788 | 86.1118 | 24.8649 | 0.4508 | 1.7458 |
| accept_15m | full/1.0 | 758 | 638.4656 | 787.2383 | 193.9002 | 20.4735 | 0.4459 | 1.7043 |
| accept_15m | full/1.5 | 752 | 317.635 | 567.8076 | 327.8411 | 29.9473 | 0.4428 | 1.6259 |

Slippage is embedded in fills; do not subtract the cost diagnostic again from net.

| Arm | Scene | Category | Name | N | Net USDT |
|---|---|---|---|---|---|
| legacy_1h | dev/1.0 | by_timeframe | 12h | 74 | 158.6066 |
| legacy_1h | dev/1.0 | by_timeframe | 1d | 31 | 73.7116 |
| legacy_1h | dev/1.0 | by_timeframe | 2h | 193 | 49.0537 |
| legacy_1h | dev/1.0 | by_timeframe | 4h | 126 | 82.4198 |
| legacy_1h | dev/1.0 | by_event | BREAKOUT_ACCEPTED | 123 | 116.457 |
| legacy_1h | dev/1.0 | by_event | LEVEL_REJECTED | 301 | 247.3348 |
| legacy_1h | dev/1.0 | by_symbol | BTCUSDT | 104 | -11.661 |
| legacy_1h | dev/1.0 | by_symbol | DOGEUSDT | 100 | 86.2725 |
| legacy_1h | dev/1.0 | by_symbol | ETHUSDT | 108 | 120.5664 |
| legacy_1h | dev/1.0 | by_symbol | SOLUSDT | 112 | 168.6138 |
| legacy_1h | dev/1.5 | by_timeframe | 12h | 74 | 130.1363 |
| legacy_1h | dev/1.5 | by_timeframe | 1d | 30 | 44.9757 |
| legacy_1h | dev/1.5 | by_timeframe | 2h | 191 | -11.8395 |
| legacy_1h | dev/1.5 | by_timeframe | 4h | 124 | 52.9886 |
| legacy_1h | dev/1.5 | by_event | BREAKOUT_ACCEPTED | 122 | 74.4488 |
| legacy_1h | dev/1.5 | by_event | LEVEL_REJECTED | 297 | 141.8123 |
| legacy_1h | dev/1.5 | by_symbol | BTCUSDT | 104 | -34.1204 |
| legacy_1h | dev/1.5 | by_symbol | DOGEUSDT | 98 | 58.4779 |
| legacy_1h | dev/1.5 | by_symbol | ETHUSDT | 107 | 83.6706 |
| legacy_1h | dev/1.5 | by_symbol | SOLUSDT | 110 | 108.2331 |
| legacy_1h | test/1.0 | by_timeframe | 12h | 44 | 46.4911 |
| legacy_1h | test/1.0 | by_timeframe | 1d | 20 | 27.4266 |
| legacy_1h | test/1.0 | by_timeframe | 2h | 85 | 65.7424 |
| legacy_1h | test/1.0 | by_timeframe | 4h | 65 | 19.3806 |
| legacy_1h | test/1.0 | by_event | BREAKOUT_ACCEPTED | 48 | 54.3133 |
| legacy_1h | test/1.0 | by_event | LEVEL_REJECTED | 166 | 104.7273 |
| legacy_1h | test/1.0 | by_symbol | BTCUSDT | 49 | 45.049 |
| legacy_1h | test/1.0 | by_symbol | DOGEUSDT | 62 | 9.6039 |
| legacy_1h | test/1.0 | by_symbol | ETHUSDT | 58 | 45.1969 |
| legacy_1h | test/1.0 | by_symbol | SOLUSDT | 45 | 59.1908 |
| legacy_1h | test/1.5 | by_timeframe | 12h | 44 | 34.8254 |
| legacy_1h | test/1.5 | by_timeframe | 1d | 20 | 20.1515 |
| legacy_1h | test/1.5 | by_timeframe | 2h | 84 | 38.0382 |
| legacy_1h | test/1.5 | by_timeframe | 4h | 64 | 1.9999 |
| legacy_1h | test/1.5 | by_event | BREAKOUT_ACCEPTED | 48 | 42.6789 |
| legacy_1h | test/1.5 | by_event | LEVEL_REJECTED | 164 | 52.3361 |
| legacy_1h | test/1.5 | by_symbol | BTCUSDT | 48 | 30.6223 |
| legacy_1h | test/1.5 | by_symbol | DOGEUSDT | 61 | -7.5138 |
| legacy_1h | test/1.5 | by_symbol | ETHUSDT | 58 | 28.2131 |
| legacy_1h | test/1.5 | by_symbol | SOLUSDT | 45 | 43.6934 |
| legacy_1h | full/1.0 | by_timeframe | 12h | 118 | 263.9118 |
| legacy_1h | full/1.0 | by_timeframe | 1d | 52 | 130.0673 |
| legacy_1h | full/1.0 | by_timeframe | 2h | 279 | 198.2396 |
| legacy_1h | full/1.0 | by_timeframe | 4h | 191 | 123.5176 |
| legacy_1h | full/1.0 | by_event | BREAKOUT_ACCEPTED | 173 | 230.6523 |
| legacy_1h | full/1.0 | by_event | LEVEL_REJECTED | 467 | 485.0841 |
| legacy_1h | full/1.0 | by_symbol | BTCUSDT | 153 | 88.3605 |
| legacy_1h | full/1.0 | by_symbol | DOGEUSDT | 162 | 107.7277 |
| legacy_1h | full/1.0 | by_symbol | ETHUSDT | 168 | 217.5393 |
| legacy_1h | full/1.0 | by_symbol | SOLUSDT | 157 | 302.1088 |
| legacy_1h | full/1.5 | by_timeframe | 12h | 118 | 188.552 |
| legacy_1h | full/1.5 | by_timeframe | 1d | 51 | 80.8851 |
| legacy_1h | full/1.5 | by_timeframe | 2h | 275 | 52.7583 |
| legacy_1h | full/1.5 | by_timeframe | 4h | 188 | 51.9034 |
| legacy_1h | full/1.5 | by_event | BREAKOUT_ACCEPTED | 171 | 140.8797 |
| legacy_1h | full/1.5 | by_event | LEVEL_REJECTED | 461 | 233.2191 |
| legacy_1h | full/1.5 | by_symbol | BTCUSDT | 152 | 17.1075 |
| legacy_1h | full/1.5 | by_symbol | DOGEUSDT | 159 | 45.8813 |
| legacy_1h | full/1.5 | by_symbol | ETHUSDT | 166 | 129.543 |
| legacy_1h | full/1.5 | by_symbol | SOLUSDT | 155 | 181.5669 |
| baseline_15m | dev/1.0 | by_timeframe | 12h | 74 | 143.2868 |
| baseline_15m | dev/1.0 | by_timeframe | 1d | 33 | 36.847 |
| baseline_15m | dev/1.0 | by_timeframe | 2h | 192 | 54.902 |
| baseline_15m | dev/1.0 | by_timeframe | 4h | 129 | 85.2832 |
| baseline_15m | dev/1.0 | by_event | BREAKOUT_ACCEPTED | 122 | 117.0136 |
| baseline_15m | dev/1.0 | by_event | LEVEL_REJECTED | 306 | 203.3055 |
| baseline_15m | dev/1.0 | by_symbol | BTCUSDT | 107 | -22.9282 |
| baseline_15m | dev/1.0 | by_symbol | DOGEUSDT | 101 | 93.0037 |
| baseline_15m | dev/1.0 | by_symbol | ETHUSDT | 109 | 109.7047 |
| baseline_15m | dev/1.0 | by_symbol | SOLUSDT | 111 | 140.539 |
| baseline_15m | dev/1.5 | by_timeframe | 12h | 74 | 116.1731 |
| baseline_15m | dev/1.5 | by_timeframe | 1d | 33 | 37.9724 |
| baseline_15m | dev/1.5 | by_timeframe | 2h | 189 | -0.9748 |
| baseline_15m | dev/1.5 | by_timeframe | 4h | 128 | 52.8265 |
| baseline_15m | dev/1.5 | by_event | BREAKOUT_ACCEPTED | 121 | 74.6089 |
| baseline_15m | dev/1.5 | by_event | LEVEL_REJECTED | 303 | 131.3883 |
| baseline_15m | dev/1.5 | by_symbol | BTCUSDT | 107 | -44.6485 |
| baseline_15m | dev/1.5 | by_symbol | DOGEUSDT | 99 | 69.4257 |
| baseline_15m | dev/1.5 | by_symbol | ETHUSDT | 109 | 71.9878 |
| baseline_15m | dev/1.5 | by_symbol | SOLUSDT | 109 | 109.2321 |
| baseline_15m | test/1.0 | by_timeframe | 12h | 45 | 43.4891 |
| baseline_15m | test/1.0 | by_timeframe | 1d | 22 | 31.0382 |
| baseline_15m | test/1.0 | by_timeframe | 2h | 86 | 63.109 |
| baseline_15m | test/1.0 | by_timeframe | 4h | 68 | 20.0532 |
| baseline_15m | test/1.0 | by_event | BREAKOUT_ACCEPTED | 52 | 54.6537 |
| baseline_15m | test/1.0 | by_event | LEVEL_REJECTED | 169 | 103.0357 |
| baseline_15m | test/1.0 | by_symbol | BTCUSDT | 51 | 48.3661 |
| baseline_15m | test/1.0 | by_symbol | DOGEUSDT | 64 | 11.8624 |
| baseline_15m | test/1.0 | by_symbol | ETHUSDT | 60 | 49.0846 |
| baseline_15m | test/1.0 | by_symbol | SOLUSDT | 46 | 48.3763 |
| baseline_15m | test/1.5 | by_timeframe | 12h | 44 | 37.4125 |
| baseline_15m | test/1.5 | by_timeframe | 1d | 22 | 24.2262 |
| baseline_15m | test/1.5 | by_timeframe | 2h | 85 | 37.1902 |
| baseline_15m | test/1.5 | by_timeframe | 4h | 67 | 2.3678 |
| baseline_15m | test/1.5 | by_event | BREAKOUT_ACCEPTED | 52 | 43.0076 |
| baseline_15m | test/1.5 | by_event | LEVEL_REJECTED | 166 | 58.1891 |
| baseline_15m | test/1.5 | by_symbol | BTCUSDT | 50 | 34.4371 |
| baseline_15m | test/1.5 | by_symbol | DOGEUSDT | 63 | -5.6686 |
| baseline_15m | test/1.5 | by_symbol | ETHUSDT | 59 | 37.571 |
| baseline_15m | test/1.5 | by_symbol | SOLUSDT | 46 | 34.8572 |
| baseline_15m | full/1.0 | by_timeframe | 12h | 119 | 237.3081 |
| baseline_15m | full/1.0 | by_timeframe | 1d | 56 | 97.1632 |
| baseline_15m | full/1.0 | by_timeframe | 2h | 278 | 188.7113 |
| baseline_15m | full/1.0 | by_timeframe | 4h | 197 | 123.8326 |
| baseline_15m | full/1.0 | by_event | BREAKOUT_ACCEPTED | 175 | 226.2456 |
| baseline_15m | full/1.0 | by_event | LEVEL_REJECTED | 475 | 420.7696 |
| baseline_15m | full/1.0 | by_symbol | BTCUSDT | 158 | 78.5785 |
| baseline_15m | full/1.0 | by_symbol | DOGEUSDT | 165 | 117.7583 |
| baseline_15m | full/1.0 | by_symbol | ETHUSDT | 170 | 206.6791 |
| baseline_15m | full/1.0 | by_symbol | SOLUSDT | 157 | 243.9992 |
| baseline_15m | full/1.5 | by_timeframe | 12h | 118 | 173.5718 |
| baseline_15m | full/1.5 | by_timeframe | 1d | 56 | 80.1614 |
| baseline_15m | full/1.5 | by_timeframe | 2h | 274 | 59.1457 |
| baseline_15m | full/1.5 | by_timeframe | 4h | 194 | 41.5135 |
| baseline_15m | full/1.5 | by_event | BREAKOUT_ACCEPTED | 174 | 135.4177 |
| baseline_15m | full/1.5 | by_event | LEVEL_REJECTED | 468 | 218.9747 |
| baseline_15m | full/1.5 | by_symbol | BTCUSDT | 156 | 8.808 |
| baseline_15m | full/1.5 | by_symbol | DOGEUSDT | 163 | 59.5868 |
| baseline_15m | full/1.5 | by_symbol | ETHUSDT | 169 | 126.1776 |
| baseline_15m | full/1.5 | by_symbol | SOLUSDT | 154 | 159.8199 |
| reject_15m | dev/1.0 | by_timeframe | 12h | 76 | 107.9484 |
| reject_15m | dev/1.0 | by_timeframe | 1d | 32 | 19.7274 |
| reject_15m | dev/1.0 | by_timeframe | 2h | 261 | -34.8082 |
| reject_15m | dev/1.0 | by_timeframe | 4h | 145 | 47.8165 |
| reject_15m | dev/1.0 | by_event | BREAKOUT_ACCEPTED | 118 | 82.1187 |
| reject_15m | dev/1.0 | by_event | LEVEL_REJECTED | 396 | 58.5654 |
| reject_15m | dev/1.0 | by_symbol | BTCUSDT | 130 | -83.197 |
| reject_15m | dev/1.0 | by_symbol | DOGEUSDT | 128 | -3.6286 |
| reject_15m | dev/1.0 | by_symbol | ETHUSDT | 128 | 122.5147 |
| reject_15m | dev/1.0 | by_symbol | SOLUSDT | 128 | 104.995 |
| reject_15m | dev/1.5 | by_timeframe | 12h | 76 | 61.5562 |
| reject_15m | dev/1.5 | by_timeframe | 1d | 30 | 17.182 |
| reject_15m | dev/1.5 | by_timeframe | 2h | 258 | -96.3463 |
| reject_15m | dev/1.5 | by_timeframe | 4h | 144 | 11.8771 |
| reject_15m | dev/1.5 | by_event | BREAKOUT_ACCEPTED | 119 | 42.8243 |
| reject_15m | dev/1.5 | by_event | LEVEL_REJECTED | 389 | -48.5552 |
| reject_15m | dev/1.5 | by_symbol | BTCUSDT | 129 | -115.4197 |
| reject_15m | dev/1.5 | by_symbol | DOGEUSDT | 127 | -20.5123 |
| reject_15m | dev/1.5 | by_symbol | ETHUSDT | 128 | 57.9113 |
| reject_15m | dev/1.5 | by_symbol | SOLUSDT | 124 | 72.2898 |
| reject_15m | test/1.0 | by_timeframe | 12h | 45 | 47.4856 |
| reject_15m | test/1.0 | by_timeframe | 1d | 22 | 32.1865 |
| reject_15m | test/1.0 | by_timeframe | 2h | 116 | -6.9282 |
| reject_15m | test/1.0 | by_timeframe | 4h | 76 | 51.4259 |
| reject_15m | test/1.0 | by_event | BREAKOUT_ACCEPTED | 50 | 21.5557 |
| reject_15m | test/1.0 | by_event | LEVEL_REJECTED | 209 | 102.6141 |
| reject_15m | test/1.0 | by_symbol | BTCUSDT | 57 | 32.6763 |
| reject_15m | test/1.0 | by_symbol | DOGEUSDT | 77 | 22.2133 |
| reject_15m | test/1.0 | by_symbol | ETHUSDT | 66 | 61.8867 |
| reject_15m | test/1.0 | by_symbol | SOLUSDT | 59 | 7.3935 |
| reject_15m | test/1.5 | by_timeframe | 12h | 45 | 33.1006 |
| reject_15m | test/1.5 | by_timeframe | 1d | 22 | 23.3939 |
| reject_15m | test/1.5 | by_timeframe | 2h | 114 | -42.4818 |
| reject_15m | test/1.5 | by_timeframe | 4h | 74 | 32.2406 |
| reject_15m | test/1.5 | by_event | BREAKOUT_ACCEPTED | 48 | 16.013 |
| reject_15m | test/1.5 | by_event | LEVEL_REJECTED | 207 | 30.2403 |
| reject_15m | test/1.5 | by_symbol | BTCUSDT | 55 | 12.8453 |
| reject_15m | test/1.5 | by_symbol | DOGEUSDT | 76 | -0.6871 |
| reject_15m | test/1.5 | by_symbol | ETHUSDT | 66 | 40.5014 |
| reject_15m | test/1.5 | by_symbol | SOLUSDT | 58 | -6.4063 |
| reject_15m | full/1.0 | by_timeframe | 12h | 121 | 176.4083 |
| reject_15m | full/1.0 | by_timeframe | 1d | 55 | 63.6627 |
| reject_15m | full/1.0 | by_timeframe | 2h | 376 | -42.4005 |
| reject_15m | full/1.0 | by_timeframe | 4h | 220 | 127.2846 |
| reject_15m | full/1.0 | by_event | BREAKOUT_ACCEPTED | 169 | 110.0897 |
| reject_15m | full/1.0 | by_event | LEVEL_REJECTED | 603 | 214.8654 |
| reject_15m | full/1.0 | by_symbol | BTCUSDT | 186 | -31.3749 |
| reject_15m | full/1.0 | by_symbol | DOGEUSDT | 205 | 28.8979 |
| reject_15m | full/1.0 | by_symbol | ETHUSDT | 195 | 207.4111 |
| reject_15m | full/1.0 | by_symbol | SOLUSDT | 186 | 120.0209 |
| reject_15m | full/1.5 | by_timeframe | 12h | 121 | 95.6258 |
| reject_15m | full/1.5 | by_timeframe | 1d | 52 | 40.6278 |
| reject_15m | full/1.5 | by_timeframe | 2h | 372 | -138.2558 |
| reject_15m | full/1.5 | by_timeframe | 4h | 217 | 45.301 |
| reject_15m | full/1.5 | by_event | BREAKOUT_ACCEPTED | 167 | 59.1162 |
| reject_15m | full/1.5 | by_event | LEVEL_REJECTED | 595 | -15.8174 |
| reject_15m | full/1.5 | by_symbol | BTCUSDT | 183 | -100.0063 |
| reject_15m | full/1.5 | by_symbol | DOGEUSDT | 203 | -21.1135 |
| reject_15m | full/1.5 | by_symbol | ETHUSDT | 194 | 98.1915 |
| reject_15m | full/1.5 | by_symbol | SOLUSDT | 182 | 66.227 |
| accept_15m | dev/1.0 | by_timeframe | 12h | 74 | 144.5348 |
| accept_15m | dev/1.0 | by_timeframe | 1d | 33 | 35.0736 |
| accept_15m | dev/1.0 | by_timeframe | 2h | 262 | 47.587 |
| accept_15m | dev/1.0 | by_timeframe | 4h | 142 | 116.6355 |
| accept_15m | dev/1.0 | by_event | BREAKOUT_ACCEPTED | 206 | 134.9072 |
| accept_15m | dev/1.0 | by_event | LEVEL_REJECTED | 305 | 208.9236 |
| accept_15m | dev/1.0 | by_symbol | BTCUSDT | 130 | -51.7816 |
| accept_15m | dev/1.0 | by_symbol | DOGEUSDT | 123 | 118.1969 |
| accept_15m | dev/1.0 | by_symbol | ETHUSDT | 126 | 137.2161 |
| accept_15m | dev/1.0 | by_symbol | SOLUSDT | 132 | 140.1994 |
| accept_15m | dev/1.5 | by_timeframe | 12h | 74 | 118.0419 |
| accept_15m | dev/1.5 | by_timeframe | 1d | 33 | 37.185 |
| accept_15m | dev/1.5 | by_timeframe | 2h | 259 | -27.4996 |
| accept_15m | dev/1.5 | by_timeframe | 4h | 141 | 76.2475 |
| accept_15m | dev/1.5 | by_event | BREAKOUT_ACCEPTED | 205 | 74.0491 |
| accept_15m | dev/1.5 | by_event | LEVEL_REJECTED | 302 | 129.9257 |
| accept_15m | dev/1.5 | by_symbol | BTCUSDT | 129 | -77.4092 |
| accept_15m | dev/1.5 | by_symbol | DOGEUSDT | 122 | 83.5906 |
| accept_15m | dev/1.5 | by_symbol | ETHUSDT | 126 | 86.7237 |
| accept_15m | dev/1.5 | by_symbol | SOLUSDT | 130 | 111.0697 |
| accept_15m | test/1.0 | by_timeframe | 12h | 46 | 47.8201 |
| accept_15m | test/1.0 | by_timeframe | 1d | 22 | 26.0711 |
| accept_15m | test/1.0 | by_timeframe | 2h | 106 | 61.0724 |
| accept_15m | test/1.0 | by_timeframe | 4h | 73 | 4.0218 |
| accept_15m | test/1.0 | by_event | BREAKOUT_ACCEPTED | 77 | 47.8982 |
| accept_15m | test/1.0 | by_event | LEVEL_REJECTED | 170 | 91.0873 |
| accept_15m | test/1.0 | by_symbol | BTCUSDT | 59 | 43.9479 |
| accept_15m | test/1.0 | by_symbol | DOGEUSDT | 72 | 4.7276 |
| accept_15m | test/1.0 | by_symbol | ETHUSDT | 66 | 32.8503 |
| accept_15m | test/1.0 | by_symbol | SOLUSDT | 50 | 57.4597 |
| accept_15m | test/1.5 | by_timeframe | 12h | 44 | 33.7208 |
| accept_15m | test/1.5 | by_timeframe | 1d | 22 | 22.6108 |
| accept_15m | test/1.5 | by_timeframe | 2h | 105 | 29.1756 |
| accept_15m | test/1.5 | by_timeframe | 4h | 73 | -8.687 |
| accept_15m | test/1.5 | by_event | BREAKOUT_ACCEPTED | 77 | 29.1634 |
| accept_15m | test/1.5 | by_event | LEVEL_REJECTED | 167 | 47.6567 |
| accept_15m | test/1.5 | by_symbol | BTCUSDT | 58 | 26.5639 |
| accept_15m | test/1.5 | by_symbol | DOGEUSDT | 72 | -8.6536 |
| accept_15m | test/1.5 | by_symbol | ETHUSDT | 65 | 24.3513 |
| accept_15m | test/1.5 | by_symbol | SOLUSDT | 49 | 34.5585 |
| accept_15m | full/1.0 | by_timeframe | 12h | 119 | 233.8035 |
| accept_15m | full/1.0 | by_timeframe | 1d | 56 | 94.8401 |
| accept_15m | full/1.0 | by_timeframe | 2h | 368 | 181.1306 |
| accept_15m | full/1.0 | by_timeframe | 4h | 215 | 128.6914 |
| accept_15m | full/1.0 | by_event | BREAKOUT_ACCEPTED | 283 | 229.7714 |
| accept_15m | full/1.0 | by_event | LEVEL_REJECTED | 475 | 408.6941 |
| accept_15m | full/1.0 | by_symbol | BTCUSDT | 189 | 42.0928 |
| accept_15m | full/1.0 | by_symbol | DOGEUSDT | 195 | 128.5429 |
| accept_15m | full/1.0 | by_symbol | ETHUSDT | 193 | 212.3761 |
| accept_15m | full/1.0 | by_symbol | SOLUSDT | 181 | 255.4538 |
| accept_15m | full/1.5 | by_timeframe | 12h | 119 | 168.3878 |
| accept_15m | full/1.5 | by_timeframe | 1d | 56 | 70.5627 |
| accept_15m | full/1.5 | by_timeframe | 2h | 364 | 20.3839 |
| accept_15m | full/1.5 | by_timeframe | 4h | 213 | 58.3006 |
| accept_15m | full/1.5 | by_event | BREAKOUT_ACCEPTED | 284 | 119.1871 |
| accept_15m | full/1.5 | by_event | LEVEL_REJECTED | 468 | 198.4479 |
| accept_15m | full/1.5 | by_symbol | BTCUSDT | 186 | -35.5183 |
| accept_15m | full/1.5 | by_symbol | DOGEUSDT | 194 | 69.0923 |
| accept_15m | full/1.5 | by_symbol | ETHUSDT | 193 | 115.9219 |
| accept_15m | full/1.5 | by_symbol | SOLUSDT | 179 | 168.1391 |

### Acceptance gates
- reject_15m: FAIL
  - passed
  - failures
- accept_15m: FAIL
  - passed
  - failures
## attempt_confirmation_replay_20261102

| Arm | Scene | N | Net USDT | Gross USDT | Cost diagnostic | Max DD % | Win rate | PF |
|---|---|---|---|---|---|---|---|---|
| legacy_1h | dev/1.0 | 424 | 363.7918 | 425.7893 | 80.5552 | 8.9621 | 0.4741 | 1.7874 |
| legacy_1h | dev/1.5 | 419 | 216.2612 | 331.335 | 150.6011 | 10.6441 | 0.4749 | 1.7082 |
| legacy_1h | test/1.0 | 214 | 159.0406 | 189.6714 | 39.8937 | 12.9648 | 0.4533 | 1.9993 |
| legacy_1h | test/1.5 | 212 | 95.015 | 154.8022 | 78.3265 | 17.5411 | 0.4528 | 1.8656 |
| legacy_1h | full/1.0 | 640 | 715.7364 | 846.4263 | 170.0432 | 12.922 | 0.4656 | 1.8687 |
| legacy_1h | full/1.5 | 632 | 374.0988 | 593.467 | 287.214 | 21.6008 | 0.4668 | 1.7576 |
| baseline_15m | dev/1.0 | 428 | 320.3191 | 381.9072 | 80.1178 | 9.3886 | 0.465 | 1.7033 |
| baseline_15m | dev/1.5 | 424 | 205.9972 | 321.652 | 151.3531 | 10.198 | 0.467 | 1.6808 |
| baseline_15m | test/1.0 | 221 | 157.6895 | 189.1726 | 41.0149 | 13.1989 | 0.4661 | 1.9815 |
| baseline_15m | test/1.5 | 218 | 101.1968 | 162.949 | 80.9326 | 18.9218 | 0.4679 | 1.9113 |
| baseline_15m | full/1.0 | 650 | 647.0151 | 774.736 | 166.2402 | 13.8993 | 0.4646 | 1.8148 |
| baseline_15m | full/1.5 | 642 | 354.3923 | 572.9362 | 286.1956 | 24.7825 | 0.4642 | 1.7362 |
| reject_15m | dev/1.0 | 519 | 260.4898 | 339.9313 | 103.5004 | 11.2105 | 0.4374 | 1.4876 |
| reject_15m | dev/1.5 | 515 | 116.4113 | 261.5227 | 190.098 | 17.6447 | 0.4311 | 1.4401 |
| reject_15m | test/1.0 | 275 | 92.521 | 131.7095 | 51.1918 | 17.0303 | 0.4327 | 1.5235 |
| reject_15m | test/1.5 | 272 | 22.3137 | 98.8719 | 100.3547 | 25.9261 | 0.4338 | 1.4294 |
| reject_15m | full/1.0 | 795 | 434.3864 | 588.2561 | 200.7141 | 18.3277 | 0.4352 | 1.5002 |
| reject_15m | full/1.5 | 788 | 147.7152 | 400.1216 | 330.7369 | 29.8937 | 0.4315 | 1.4346 |
| accept_15m | dev/1.0 | 520 | 273.6463 | 348.272 | 97.1601 | 13.9282 | 0.4404 | 1.5225 |
| accept_15m | dev/1.5 | 516 | 153.6607 | 291.8706 | 180.9563 | 16.3098 | 0.4399 | 1.5082 |
| accept_15m | test/1.0 | 281 | 129.9382 | 167.1348 | 48.6056 | 21.7467 | 0.4093 | 1.6717 |
| accept_15m | test/1.5 | 277 | 48.3446 | 118.5989 | 92.3041 | 31.1658 | 0.4079 | 1.5262 |
| accept_15m | full/1.0 | 801 | 520.6998 | 667.1452 | 191.0107 | 27.8332 | 0.4282 | 1.5805 |
| accept_15m | full/1.5 | 793 | 224.563 | 470.1025 | 321.902 | 38.4493 | 0.4275 | 1.5109 |

Slippage is embedded in fills; do not subtract the cost diagnostic again from net.

| Arm | Scene | Category | Name | N | Net USDT |
|---|---|---|---|---|---|
| legacy_1h | dev/1.0 | by_timeframe | 12h | 74 | 158.6066 |
| legacy_1h | dev/1.0 | by_timeframe | 1d | 31 | 73.7116 |
| legacy_1h | dev/1.0 | by_timeframe | 2h | 193 | 49.0537 |
| legacy_1h | dev/1.0 | by_timeframe | 4h | 126 | 82.4198 |
| legacy_1h | dev/1.0 | by_event | BREAKOUT_ACCEPTED | 123 | 116.457 |
| legacy_1h | dev/1.0 | by_event | LEVEL_REJECTED | 301 | 247.3348 |
| legacy_1h | dev/1.0 | by_symbol | BTCUSDT | 104 | -11.661 |
| legacy_1h | dev/1.0 | by_symbol | DOGEUSDT | 100 | 86.2725 |
| legacy_1h | dev/1.0 | by_symbol | ETHUSDT | 108 | 120.5664 |
| legacy_1h | dev/1.0 | by_symbol | SOLUSDT | 112 | 168.6138 |
| legacy_1h | dev/1.5 | by_timeframe | 12h | 74 | 130.1363 |
| legacy_1h | dev/1.5 | by_timeframe | 1d | 30 | 44.9757 |
| legacy_1h | dev/1.5 | by_timeframe | 2h | 191 | -11.8395 |
| legacy_1h | dev/1.5 | by_timeframe | 4h | 124 | 52.9886 |
| legacy_1h | dev/1.5 | by_event | BREAKOUT_ACCEPTED | 122 | 74.4488 |
| legacy_1h | dev/1.5 | by_event | LEVEL_REJECTED | 297 | 141.8123 |
| legacy_1h | dev/1.5 | by_symbol | BTCUSDT | 104 | -34.1204 |
| legacy_1h | dev/1.5 | by_symbol | DOGEUSDT | 98 | 58.4779 |
| legacy_1h | dev/1.5 | by_symbol | ETHUSDT | 107 | 83.6706 |
| legacy_1h | dev/1.5 | by_symbol | SOLUSDT | 110 | 108.2331 |
| legacy_1h | test/1.0 | by_timeframe | 12h | 44 | 46.4911 |
| legacy_1h | test/1.0 | by_timeframe | 1d | 20 | 27.4266 |
| legacy_1h | test/1.0 | by_timeframe | 2h | 85 | 65.7424 |
| legacy_1h | test/1.0 | by_timeframe | 4h | 65 | 19.3806 |
| legacy_1h | test/1.0 | by_event | BREAKOUT_ACCEPTED | 48 | 54.3133 |
| legacy_1h | test/1.0 | by_event | LEVEL_REJECTED | 166 | 104.7273 |
| legacy_1h | test/1.0 | by_symbol | BTCUSDT | 49 | 45.049 |
| legacy_1h | test/1.0 | by_symbol | DOGEUSDT | 62 | 9.6039 |
| legacy_1h | test/1.0 | by_symbol | ETHUSDT | 58 | 45.1969 |
| legacy_1h | test/1.0 | by_symbol | SOLUSDT | 45 | 59.1908 |
| legacy_1h | test/1.5 | by_timeframe | 12h | 44 | 34.8254 |
| legacy_1h | test/1.5 | by_timeframe | 1d | 20 | 20.1515 |
| legacy_1h | test/1.5 | by_timeframe | 2h | 84 | 38.0382 |
| legacy_1h | test/1.5 | by_timeframe | 4h | 64 | 1.9999 |
| legacy_1h | test/1.5 | by_event | BREAKOUT_ACCEPTED | 48 | 42.6789 |
| legacy_1h | test/1.5 | by_event | LEVEL_REJECTED | 164 | 52.3361 |
| legacy_1h | test/1.5 | by_symbol | BTCUSDT | 48 | 30.6223 |
| legacy_1h | test/1.5 | by_symbol | DOGEUSDT | 61 | -7.5138 |
| legacy_1h | test/1.5 | by_symbol | ETHUSDT | 58 | 28.2131 |
| legacy_1h | test/1.5 | by_symbol | SOLUSDT | 45 | 43.6934 |
| legacy_1h | full/1.0 | by_timeframe | 12h | 118 | 263.9118 |
| legacy_1h | full/1.0 | by_timeframe | 1d | 52 | 130.0673 |
| legacy_1h | full/1.0 | by_timeframe | 2h | 279 | 198.2396 |
| legacy_1h | full/1.0 | by_timeframe | 4h | 191 | 123.5176 |
| legacy_1h | full/1.0 | by_event | BREAKOUT_ACCEPTED | 173 | 230.6523 |
| legacy_1h | full/1.0 | by_event | LEVEL_REJECTED | 467 | 485.0841 |
| legacy_1h | full/1.0 | by_symbol | BTCUSDT | 153 | 88.3605 |
| legacy_1h | full/1.0 | by_symbol | DOGEUSDT | 162 | 107.7277 |
| legacy_1h | full/1.0 | by_symbol | ETHUSDT | 168 | 217.5393 |
| legacy_1h | full/1.0 | by_symbol | SOLUSDT | 157 | 302.1088 |
| legacy_1h | full/1.5 | by_timeframe | 12h | 118 | 188.552 |
| legacy_1h | full/1.5 | by_timeframe | 1d | 51 | 80.8851 |
| legacy_1h | full/1.5 | by_timeframe | 2h | 275 | 52.7583 |
| legacy_1h | full/1.5 | by_timeframe | 4h | 188 | 51.9034 |
| legacy_1h | full/1.5 | by_event | BREAKOUT_ACCEPTED | 171 | 140.8797 |
| legacy_1h | full/1.5 | by_event | LEVEL_REJECTED | 461 | 233.2191 |
| legacy_1h | full/1.5 | by_symbol | BTCUSDT | 152 | 17.1075 |
| legacy_1h | full/1.5 | by_symbol | DOGEUSDT | 159 | 45.8813 |
| legacy_1h | full/1.5 | by_symbol | ETHUSDT | 166 | 129.543 |
| legacy_1h | full/1.5 | by_symbol | SOLUSDT | 155 | 181.5669 |
| baseline_15m | dev/1.0 | by_timeframe | 12h | 74 | 143.2868 |
| baseline_15m | dev/1.0 | by_timeframe | 1d | 33 | 36.847 |
| baseline_15m | dev/1.0 | by_timeframe | 2h | 192 | 54.902 |
| baseline_15m | dev/1.0 | by_timeframe | 4h | 129 | 85.2832 |
| baseline_15m | dev/1.0 | by_event | BREAKOUT_ACCEPTED | 122 | 117.0136 |
| baseline_15m | dev/1.0 | by_event | LEVEL_REJECTED | 306 | 203.3055 |
| baseline_15m | dev/1.0 | by_symbol | BTCUSDT | 107 | -22.9282 |
| baseline_15m | dev/1.0 | by_symbol | DOGEUSDT | 101 | 93.0037 |
| baseline_15m | dev/1.0 | by_symbol | ETHUSDT | 109 | 109.7047 |
| baseline_15m | dev/1.0 | by_symbol | SOLUSDT | 111 | 140.539 |
| baseline_15m | dev/1.5 | by_timeframe | 12h | 74 | 116.1731 |
| baseline_15m | dev/1.5 | by_timeframe | 1d | 33 | 37.9724 |
| baseline_15m | dev/1.5 | by_timeframe | 2h | 189 | -0.9748 |
| baseline_15m | dev/1.5 | by_timeframe | 4h | 128 | 52.8265 |
| baseline_15m | dev/1.5 | by_event | BREAKOUT_ACCEPTED | 121 | 74.6089 |
| baseline_15m | dev/1.5 | by_event | LEVEL_REJECTED | 303 | 131.3883 |
| baseline_15m | dev/1.5 | by_symbol | BTCUSDT | 107 | -44.6485 |
| baseline_15m | dev/1.5 | by_symbol | DOGEUSDT | 99 | 69.4257 |
| baseline_15m | dev/1.5 | by_symbol | ETHUSDT | 109 | 71.9878 |
| baseline_15m | dev/1.5 | by_symbol | SOLUSDT | 109 | 109.2321 |
| baseline_15m | test/1.0 | by_timeframe | 12h | 45 | 43.4891 |
| baseline_15m | test/1.0 | by_timeframe | 1d | 22 | 31.0382 |
| baseline_15m | test/1.0 | by_timeframe | 2h | 86 | 63.109 |
| baseline_15m | test/1.0 | by_timeframe | 4h | 68 | 20.0532 |
| baseline_15m | test/1.0 | by_event | BREAKOUT_ACCEPTED | 52 | 54.6537 |
| baseline_15m | test/1.0 | by_event | LEVEL_REJECTED | 169 | 103.0357 |
| baseline_15m | test/1.0 | by_symbol | BTCUSDT | 51 | 48.3661 |
| baseline_15m | test/1.0 | by_symbol | DOGEUSDT | 64 | 11.8624 |
| baseline_15m | test/1.0 | by_symbol | ETHUSDT | 60 | 49.0846 |
| baseline_15m | test/1.0 | by_symbol | SOLUSDT | 46 | 48.3763 |
| baseline_15m | test/1.5 | by_timeframe | 12h | 44 | 37.4125 |
| baseline_15m | test/1.5 | by_timeframe | 1d | 22 | 24.2262 |
| baseline_15m | test/1.5 | by_timeframe | 2h | 85 | 37.1902 |
| baseline_15m | test/1.5 | by_timeframe | 4h | 67 | 2.3678 |
| baseline_15m | test/1.5 | by_event | BREAKOUT_ACCEPTED | 52 | 43.0076 |
| baseline_15m | test/1.5 | by_event | LEVEL_REJECTED | 166 | 58.1891 |
| baseline_15m | test/1.5 | by_symbol | BTCUSDT | 50 | 34.4371 |
| baseline_15m | test/1.5 | by_symbol | DOGEUSDT | 63 | -5.6686 |
| baseline_15m | test/1.5 | by_symbol | ETHUSDT | 59 | 37.571 |
| baseline_15m | test/1.5 | by_symbol | SOLUSDT | 46 | 34.8572 |
| baseline_15m | full/1.0 | by_timeframe | 12h | 119 | 237.3081 |
| baseline_15m | full/1.0 | by_timeframe | 1d | 56 | 97.1632 |
| baseline_15m | full/1.0 | by_timeframe | 2h | 278 | 188.7113 |
| baseline_15m | full/1.0 | by_timeframe | 4h | 197 | 123.8326 |
| baseline_15m | full/1.0 | by_event | BREAKOUT_ACCEPTED | 175 | 226.2456 |
| baseline_15m | full/1.0 | by_event | LEVEL_REJECTED | 475 | 420.7696 |
| baseline_15m | full/1.0 | by_symbol | BTCUSDT | 158 | 78.5785 |
| baseline_15m | full/1.0 | by_symbol | DOGEUSDT | 165 | 117.7583 |
| baseline_15m | full/1.0 | by_symbol | ETHUSDT | 170 | 206.6791 |
| baseline_15m | full/1.0 | by_symbol | SOLUSDT | 157 | 243.9992 |
| baseline_15m | full/1.5 | by_timeframe | 12h | 118 | 173.5718 |
| baseline_15m | full/1.5 | by_timeframe | 1d | 56 | 80.1614 |
| baseline_15m | full/1.5 | by_timeframe | 2h | 274 | 59.1457 |
| baseline_15m | full/1.5 | by_timeframe | 4h | 194 | 41.5135 |
| baseline_15m | full/1.5 | by_event | BREAKOUT_ACCEPTED | 174 | 135.4177 |
| baseline_15m | full/1.5 | by_event | LEVEL_REJECTED | 468 | 218.9747 |
| baseline_15m | full/1.5 | by_symbol | BTCUSDT | 156 | 8.808 |
| baseline_15m | full/1.5 | by_symbol | DOGEUSDT | 163 | 59.5868 |
| baseline_15m | full/1.5 | by_symbol | ETHUSDT | 169 | 126.1776 |
| baseline_15m | full/1.5 | by_symbol | SOLUSDT | 154 | 159.8199 |
| reject_15m | dev/1.0 | by_timeframe | 12h | 73 | 133.5164 |
| reject_15m | dev/1.0 | by_timeframe | 1d | 33 | 38.4337 |
| reject_15m | dev/1.0 | by_timeframe | 2h | 267 | 21.9307 |
| reject_15m | dev/1.0 | by_timeframe | 4h | 146 | 66.609 |
| reject_15m | dev/1.0 | by_event | BREAKOUT_ACCEPTED | 116 | 120.1766 |
| reject_15m | dev/1.0 | by_event | LEVEL_REJECTED | 403 | 140.3131 |
| reject_15m | dev/1.0 | by_symbol | BTCUSDT | 131 | -18.4181 |
| reject_15m | dev/1.0 | by_symbol | DOGEUSDT | 130 | 43.5413 |
| reject_15m | dev/1.0 | by_symbol | ETHUSDT | 129 | 142.1709 |
| reject_15m | dev/1.0 | by_symbol | SOLUSDT | 129 | 93.1957 |
| reject_15m | dev/1.5 | by_timeframe | 12h | 74 | 105.5386 |
| reject_15m | dev/1.5 | by_timeframe | 1d | 33 | 30.9951 |
| reject_15m | dev/1.5 | by_timeframe | 2h | 264 | -35.6017 |
| reject_15m | dev/1.5 | by_timeframe | 4h | 144 | 15.4793 |
| reject_15m | dev/1.5 | by_event | BREAKOUT_ACCEPTED | 115 | 63.6311 |
| reject_15m | dev/1.5 | by_event | LEVEL_REJECTED | 400 | 52.7801 |
| reject_15m | dev/1.5 | by_symbol | BTCUSDT | 131 | -69.9496 |
| reject_15m | dev/1.5 | by_symbol | DOGEUSDT | 129 | 25.5364 |
| reject_15m | dev/1.5 | by_symbol | ETHUSDT | 128 | 90.8654 |
| reject_15m | dev/1.5 | by_symbol | SOLUSDT | 127 | 69.9591 |
| reject_15m | test/1.0 | by_timeframe | 12h | 45 | 42.4893 |
| reject_15m | test/1.0 | by_timeframe | 1d | 22 | 31.1588 |
| reject_15m | test/1.0 | by_timeframe | 2h | 120 | 11.9734 |
| reject_15m | test/1.0 | by_timeframe | 4h | 88 | 6.8995 |
| reject_15m | test/1.0 | by_event | BREAKOUT_ACCEPTED | 52 | 18.3261 |
| reject_15m | test/1.0 | by_event | LEVEL_REJECTED | 223 | 74.1949 |
| reject_15m | test/1.0 | by_symbol | BTCUSDT | 63 | 48.9913 |
| reject_15m | test/1.0 | by_symbol | DOGEUSDT | 83 | -17.227 |
| reject_15m | test/1.0 | by_symbol | ETHUSDT | 69 | 45.8131 |
| reject_15m | test/1.0 | by_symbol | SOLUSDT | 60 | 14.9437 |
| reject_15m | test/1.5 | by_timeframe | 12h | 45 | 30.0989 |
| reject_15m | test/1.5 | by_timeframe | 1d | 22 | 23.6004 |
| reject_15m | test/1.5 | by_timeframe | 2h | 119 | -16.3954 |
| reject_15m | test/1.5 | by_timeframe | 4h | 86 | -14.9902 |
| reject_15m | test/1.5 | by_event | BREAKOUT_ACCEPTED | 51 | 8.6512 |
| reject_15m | test/1.5 | by_event | LEVEL_REJECTED | 221 | 13.6624 |
| reject_15m | test/1.5 | by_symbol | BTCUSDT | 62 | 27.8394 |
| reject_15m | test/1.5 | by_symbol | DOGEUSDT | 82 | -36.7132 |
| reject_15m | test/1.5 | by_symbol | ETHUSDT | 69 | 28.6079 |
| reject_15m | test/1.5 | by_symbol | SOLUSDT | 59 | 2.5796 |
| reject_15m | full/1.0 | by_timeframe | 12h | 118 | 215.6849 |
| reject_15m | full/1.0 | by_timeframe | 1d | 56 | 94.608 |
| reject_15m | full/1.0 | by_timeframe | 2h | 387 | 46.0893 |
| reject_15m | full/1.0 | by_timeframe | 4h | 234 | 78.0041 |
| reject_15m | full/1.0 | by_event | BREAKOUT_ACCEPTED | 169 | 152.3313 |
| reject_15m | full/1.0 | by_event | LEVEL_REJECTED | 626 | 282.055 |
| reject_15m | full/1.0 | by_symbol | BTCUSDT | 194 | 73.6275 |
| reject_15m | full/1.0 | by_symbol | DOGEUSDT | 213 | 11.6639 |
| reject_15m | full/1.0 | by_symbol | ETHUSDT | 199 | 227.9185 |
| reject_15m | full/1.0 | by_symbol | SOLUSDT | 189 | 121.1765 |
| reject_15m | full/1.5 | by_timeframe | 12h | 119 | 146.5128 |
| reject_15m | full/1.5 | by_timeframe | 1d | 56 | 62.9021 |
| reject_15m | full/1.5 | by_timeframe | 2h | 383 | -59.5398 |
| reject_15m | full/1.5 | by_timeframe | 4h | 230 | -2.16 |
| reject_15m | full/1.5 | by_event | BREAKOUT_ACCEPTED | 167 | 76.3514 |
| reject_15m | full/1.5 | by_event | LEVEL_REJECTED | 621 | 71.3638 |
| reject_15m | full/1.5 | by_symbol | BTCUSDT | 193 | -31.7283 |
| reject_15m | full/1.5 | by_symbol | DOGEUSDT | 211 | -25.1676 |
| reject_15m | full/1.5 | by_symbol | ETHUSDT | 198 | 131.4587 |
| reject_15m | full/1.5 | by_symbol | SOLUSDT | 186 | 73.1524 |
| accept_15m | dev/1.0 | by_timeframe | 12h | 74 | 139.9365 |
| accept_15m | dev/1.0 | by_timeframe | 1d | 33 | 39.0106 |
| accept_15m | dev/1.0 | by_timeframe | 2h | 269 | 42.3812 |
| accept_15m | dev/1.0 | by_timeframe | 4h | 144 | 52.318 |
| accept_15m | dev/1.0 | by_event | BREAKOUT_ACCEPTED | 218 | 67.3802 |
| accept_15m | dev/1.0 | by_event | LEVEL_REJECTED | 302 | 206.2662 |
| accept_15m | dev/1.0 | by_symbol | BTCUSDT | 125 | -35.7572 |
| accept_15m | dev/1.0 | by_symbol | DOGEUSDT | 123 | 72.9421 |
| accept_15m | dev/1.0 | by_symbol | ETHUSDT | 133 | 102.8296 |
| accept_15m | dev/1.0 | by_symbol | SOLUSDT | 139 | 133.6319 |
| accept_15m | dev/1.5 | by_timeframe | 12h | 74 | 110.3511 |
| accept_15m | dev/1.5 | by_timeframe | 1d | 33 | 38.4645 |
| accept_15m | dev/1.5 | by_timeframe | 2h | 265 | -22.4173 |
| accept_15m | dev/1.5 | by_timeframe | 4h | 144 | 27.2624 |
| accept_15m | dev/1.5 | by_event | BREAKOUT_ACCEPTED | 218 | 18.1735 |
| accept_15m | dev/1.5 | by_event | LEVEL_REJECTED | 298 | 135.4872 |
| accept_15m | dev/1.5 | by_symbol | BTCUSDT | 125 | -63.1986 |
| accept_15m | dev/1.5 | by_symbol | DOGEUSDT | 121 | 48.1577 |
| accept_15m | dev/1.5 | by_symbol | ETHUSDT | 133 | 59.8901 |
| accept_15m | dev/1.5 | by_symbol | SOLUSDT | 137 | 108.8116 |
| accept_15m | test/1.0 | by_timeframe | 12h | 46 | 44.2435 |
| accept_15m | test/1.0 | by_timeframe | 1d | 22 | 26.3909 |
| accept_15m | test/1.0 | by_timeframe | 2h | 124 | 22.1094 |
| accept_15m | test/1.0 | by_timeframe | 4h | 89 | 37.1944 |
| accept_15m | test/1.0 | by_event | BREAKOUT_ACCEPTED | 112 | 37.5376 |
| accept_15m | test/1.0 | by_event | LEVEL_REJECTED | 169 | 92.4006 |
| accept_15m | test/1.0 | by_symbol | BTCUSDT | 66 | 20.5275 |
| accept_15m | test/1.0 | by_symbol | DOGEUSDT | 81 | -15.8516 |
| accept_15m | test/1.0 | by_symbol | ETHUSDT | 71 | 56.9358 |
| accept_15m | test/1.0 | by_symbol | SOLUSDT | 63 | 68.3265 |
| accept_15m | test/1.5 | by_timeframe | 12h | 45 | 25.7851 |
| accept_15m | test/1.5 | by_timeframe | 1d | 22 | 21.8966 |
| accept_15m | test/1.5 | by_timeframe | 2h | 122 | -6.7571 |
| accept_15m | test/1.5 | by_timeframe | 4h | 88 | 7.4201 |
| accept_15m | test/1.5 | by_event | BREAKOUT_ACCEPTED | 109 | 4.1687 |
| accept_15m | test/1.5 | by_event | LEVEL_REJECTED | 168 | 44.1759 |
| accept_15m | test/1.5 | by_symbol | BTCUSDT | 64 | 4.8463 |
| accept_15m | test/1.5 | by_symbol | DOGEUSDT | 81 | -33.1559 |
| accept_15m | test/1.5 | by_symbol | ETHUSDT | 71 | 39.3501 |
| accept_15m | test/1.5 | by_symbol | SOLUSDT | 61 | 37.3041 |
| accept_15m | full/1.0 | by_timeframe | 12h | 119 | 217.7609 |
| accept_15m | full/1.0 | by_timeframe | 1d | 56 | 92.3558 |
| accept_15m | full/1.0 | by_timeframe | 2h | 393 | 88.1423 |
| accept_15m | full/1.0 | by_timeframe | 4h | 233 | 122.4408 |
| accept_15m | full/1.0 | by_event | BREAKOUT_ACCEPTED | 330 | 127.0588 |
| accept_15m | full/1.0 | by_event | LEVEL_REJECTED | 471 | 393.641 |
| accept_15m | full/1.0 | by_symbol | BTCUSDT | 191 | 1.3398 |
| accept_15m | full/1.0 | by_symbol | DOGEUSDT | 204 | 42.9793 |
| accept_15m | full/1.0 | by_symbol | ETHUSDT | 205 | 221.6494 |
| accept_15m | full/1.0 | by_symbol | SOLUSDT | 201 | 254.7313 |
| accept_15m | full/1.5 | by_timeframe | 12h | 119 | 152.5343 |
| accept_15m | full/1.5 | by_timeframe | 1d | 56 | 67.0975 |
| accept_15m | full/1.5 | by_timeframe | 2h | 387 | -33.052 |
| accept_15m | full/1.5 | by_timeframe | 4h | 231 | 37.9832 |
| accept_15m | full/1.5 | by_event | BREAKOUT_ACCEPTED | 328 | 19.2715 |
| accept_15m | full/1.5 | by_event | LEVEL_REJECTED | 465 | 205.2915 |
| accept_15m | full/1.5 | by_symbol | BTCUSDT | 189 | -53.2255 |
| accept_15m | full/1.5 | by_symbol | DOGEUSDT | 201 | -1.4389 |
| accept_15m | full/1.5 | by_symbol | ETHUSDT | 205 | 113.8278 |
| accept_15m | full/1.5 | by_symbol | SOLUSDT | 198 | 165.3996 |

### Acceptance gates
- reject_15m: FAIL
  - passed
  - failures
- accept_15m: FAIL
  - passed
  - failures
## exit_timing_20261102

| Arm | Scene | N | Net USDT | Gross USDT | Cost diagnostic | Max DD % | Win rate | PF |
|---|---|---|---|---|---|---|---|---|
| cognition | dev/1 | 428 | 320.3191 | 381.9072 | 80.1178 | 9.3886 | 0.465 | 1.7033 |
| price_2h | dev/1 | 438 | 278.2201 | 336.8307 | 76.2406 | 10.2219 | 0.4658 | 1.6473 |
| price_15m | dev/1 | 448 | 194.6402 | 249.6366 | 71.6406 | 10.9172 | 0.471 | 1.5048 |
| armed_15m | dev/1 | 439 | 263.4485 | 320.806 | 74.569 | 10.1899 | 0.467 | 1.6292 |
| cognition | dev/1.5 | 424 | 205.9972 | 321.652 | 151.3531 | 10.198 | 0.467 | 1.6808 |
| price_2h | dev/1.5 | 433 | 188.0812 | 298.7534 | 144.8037 | 10.627 | 0.4665 | 1.6486 |
| price_15m | dev/1.5 | 441 | 91.3608 | 194.7743 | 135.4403 | 13.2884 | 0.4671 | 1.4465 |
| armed_15m | dev/1.5 | 432 | 177.0452 | 285.6788 | 142.1501 | 10.7568 | 0.4676 | 1.6306 |
| cognition | test/1 | 221 | 157.6895 | 189.1726 | 41.0149 | 13.1989 | 0.4661 | 1.9815 |
| price_2h | test/1 | 223 | 133.8253 | 164.2785 | 39.7201 | 14.2096 | 0.4709 | 1.8676 |
| price_15m | test/1 | 229 | 130.9996 | 162.6877 | 41.3929 | 12.9695 | 0.5109 | 1.8762 |
| armed_15m | test/1 | 225 | 129.3791 | 159.9349 | 39.8551 | 14.202 | 0.4667 | 1.8326 |
| cognition | test/1.5 | 218 | 101.1968 | 162.949 | 80.9326 | 18.9218 | 0.4679 | 1.9113 |
| price_2h | test/1.5 | 221 | 79.1831 | 140.0035 | 79.7776 | 18.8311 | 0.4706 | 1.7741 |
| price_15m | test/1.5 | 227 | 60.7472 | 123.562 | 82.4402 | 18.7696 | 0.5066 | 1.7024 |
| armed_15m | test/1.5 | 221 | 76.7389 | 137.298 | 79.4443 | 19.3777 | 0.4661 | 1.7636 |
| cognition | full/1 | 650 | 647.0151 | 774.736 | 166.2402 | 13.8993 | 0.4646 | 1.8148 |
| price_2h | full/1 | 661 | 532.024 | 649.5755 | 153.0775 | 14.4909 | 0.4644 | 1.7299 |
| price_15m | full/1 | 676 | 405.9502 | 513.6436 | 140.4464 | 12.7909 | 0.4837 | 1.6389 |
| armed_15m | full/1 | 664 | 521.4786 | 637.6682 | 151.274 | 14.5635 | 0.4669 | 1.7288 |
| cognition | full/1.5 | 642 | 354.3923 | 572.9362 | 286.1956 | 24.7825 | 0.4642 | 1.7362 |
| price_2h | full/1.5 | 654 | 310.4551 | 520.1182 | 274.4978 | 22.0895 | 0.4648 | 1.6871 |
| price_15m | full/1.5 | 669 | 170.939 | 355.4408 | 241.8064 | 19.2658 | 0.4798 | 1.5363 |
| armed_15m | full/1.5 | 654 | 287.5879 | 492.5063 | 268.2885 | 21.8754 | 0.4648 | 1.6649 |

Slippage is embedded in fills; do not subtract the cost diagnostic again from net.

| Arm | Scene | Category | Name | N | Net USDT |
|---|---|---|---|---|---|
| cognition | dev/1 | by_timeframe | 12h | 74 | 143.2868 |
| cognition | dev/1 | by_timeframe | 1d | 33 | 36.847 |
| cognition | dev/1 | by_timeframe | 2h | 192 | 54.902 |
| cognition | dev/1 | by_timeframe | 4h | 129 | 85.2832 |
| cognition | dev/1 | by_event | BREAKOUT_ACCEPTED | 122 | 117.0136 |
| cognition | dev/1 | by_event | LEVEL_REJECTED | 306 | 203.3055 |
| cognition | dev/1 | by_symbol | BTCUSDT | 107 | -22.9282 |
| cognition | dev/1 | by_symbol | DOGEUSDT | 101 | 93.0037 |
| cognition | dev/1 | by_symbol | ETHUSDT | 109 | 109.7047 |
| cognition | dev/1 | by_symbol | SOLUSDT | 111 | 140.539 |
| price_2h | dev/1 | by_timeframe | 12h | 82 | 132.3545 |
| price_2h | dev/1 | by_timeframe | 1d | 35 | 33.141 |
| price_2h | dev/1 | by_timeframe | 2h | 192 | 42.6566 |
| price_2h | dev/1 | by_timeframe | 4h | 129 | 70.068 |
| price_2h | dev/1 | by_event | BREAKOUT_ACCEPTED | 123 | 102.4425 |
| price_2h | dev/1 | by_event | LEVEL_REJECTED | 315 | 175.7776 |
| price_2h | dev/1 | by_symbol | BTCUSDT | 112 | -20.2946 |
| price_2h | dev/1 | by_symbol | DOGEUSDT | 104 | 92.9236 |
| price_2h | dev/1 | by_symbol | ETHUSDT | 111 | 89.7555 |
| price_2h | dev/1 | by_symbol | SOLUSDT | 111 | 115.8356 |
| price_15m | dev/1 | by_timeframe | 12h | 86 | 108.7288 |
| price_15m | dev/1 | by_timeframe | 1d | 36 | 3.7335 |
| price_15m | dev/1 | by_timeframe | 2h | 196 | 10.3182 |
| price_15m | dev/1 | by_timeframe | 4h | 130 | 71.8596 |
| price_15m | dev/1 | by_event | BREAKOUT_ACCEPTED | 123 | 90.06 |
| price_15m | dev/1 | by_event | LEVEL_REJECTED | 325 | 104.5801 |
| price_15m | dev/1 | by_symbol | BTCUSDT | 115 | -36.2316 |
| price_15m | dev/1 | by_symbol | DOGEUSDT | 104 | 79.2454 |
| price_15m | dev/1 | by_symbol | ETHUSDT | 115 | 67.4251 |
| price_15m | dev/1 | by_symbol | SOLUSDT | 114 | 84.2014 |
| armed_15m | dev/1 | by_timeframe | 12h | 83 | 130.5051 |
| armed_15m | dev/1 | by_timeframe | 1d | 35 | 33.4393 |
| armed_15m | dev/1 | by_timeframe | 2h | 192 | 34.5878 |
| armed_15m | dev/1 | by_timeframe | 4h | 129 | 64.9163 |
| armed_15m | dev/1 | by_event | BREAKOUT_ACCEPTED | 124 | 92.9299 |
| armed_15m | dev/1 | by_event | LEVEL_REJECTED | 315 | 170.5185 |
| armed_15m | dev/1 | by_symbol | BTCUSDT | 112 | -25.0665 |
| armed_15m | dev/1 | by_symbol | DOGEUSDT | 104 | 91.7689 |
| armed_15m | dev/1 | by_symbol | ETHUSDT | 112 | 84.8521 |
| armed_15m | dev/1 | by_symbol | SOLUSDT | 111 | 111.8939 |
| cognition | dev/1.5 | by_timeframe | 12h | 74 | 116.1731 |
| cognition | dev/1.5 | by_timeframe | 1d | 33 | 37.9724 |
| cognition | dev/1.5 | by_timeframe | 2h | 189 | -0.9748 |
| cognition | dev/1.5 | by_timeframe | 4h | 128 | 52.8265 |
| cognition | dev/1.5 | by_event | BREAKOUT_ACCEPTED | 121 | 74.6089 |
| cognition | dev/1.5 | by_event | LEVEL_REJECTED | 303 | 131.3883 |
| cognition | dev/1.5 | by_symbol | BTCUSDT | 107 | -44.6485 |
| cognition | dev/1.5 | by_symbol | DOGEUSDT | 99 | 69.4257 |
| cognition | dev/1.5 | by_symbol | ETHUSDT | 109 | 71.9878 |
| cognition | dev/1.5 | by_symbol | SOLUSDT | 109 | 109.2321 |
| price_2h | dev/1.5 | by_timeframe | 12h | 81 | 111.7395 |
| price_2h | dev/1.5 | by_timeframe | 1d | 35 | 36.2785 |
| price_2h | dev/1.5 | by_timeframe | 2h | 189 | 0.3888 |
| price_2h | dev/1.5 | by_timeframe | 4h | 128 | 39.6744 |
| price_2h | dev/1.5 | by_event | BREAKOUT_ACCEPTED | 123 | 70.8467 |
| price_2h | dev/1.5 | by_event | LEVEL_REJECTED | 310 | 117.2344 |
| price_2h | dev/1.5 | by_symbol | BTCUSDT | 111 | -49.3308 |
| price_2h | dev/1.5 | by_symbol | DOGEUSDT | 102 | 73.7631 |
| price_2h | dev/1.5 | by_symbol | ETHUSDT | 111 | 53.8873 |
| price_2h | dev/1.5 | by_symbol | SOLUSDT | 109 | 109.7616 |
| price_15m | dev/1.5 | by_timeframe | 12h | 84 | 68.9831 |
| price_15m | dev/1.5 | by_timeframe | 1d | 35 | 3.8923 |
| price_15m | dev/1.5 | by_timeframe | 2h | 193 | -25.0202 |
| price_15m | dev/1.5 | by_timeframe | 4h | 129 | 43.5056 |
| price_15m | dev/1.5 | by_event | BREAKOUT_ACCEPTED | 123 | 58.9016 |
| price_15m | dev/1.5 | by_event | LEVEL_REJECTED | 318 | 32.4592 |
| price_15m | dev/1.5 | by_symbol | BTCUSDT | 112 | -61.6732 |
| price_15m | dev/1.5 | by_symbol | DOGEUSDT | 103 | 57.8723 |
| price_15m | dev/1.5 | by_symbol | ETHUSDT | 114 | 35.4171 |
| price_15m | dev/1.5 | by_symbol | SOLUSDT | 112 | 59.7446 |
| armed_15m | dev/1.5 | by_timeframe | 12h | 81 | 105.6764 |
| armed_15m | dev/1.5 | by_timeframe | 1d | 34 | 35.3817 |
| armed_15m | dev/1.5 | by_timeframe | 2h | 189 | -4.3707 |
| armed_15m | dev/1.5 | by_timeframe | 4h | 128 | 40.3578 |
| armed_15m | dev/1.5 | by_event | BREAKOUT_ACCEPTED | 124 | 67.8773 |
| armed_15m | dev/1.5 | by_event | LEVEL_REJECTED | 308 | 109.1679 |
| armed_15m | dev/1.5 | by_symbol | BTCUSDT | 111 | -53.7651 |
| armed_15m | dev/1.5 | by_symbol | DOGEUSDT | 102 | 72.9382 |
| armed_15m | dev/1.5 | by_symbol | ETHUSDT | 110 | 54.203 |
| armed_15m | dev/1.5 | by_symbol | SOLUSDT | 109 | 103.6692 |
| cognition | test/1 | by_timeframe | 12h | 45 | 43.4891 |
| cognition | test/1 | by_timeframe | 1d | 22 | 31.0382 |
| cognition | test/1 | by_timeframe | 2h | 86 | 63.109 |
| cognition | test/1 | by_timeframe | 4h | 68 | 20.0532 |
| cognition | test/1 | by_event | BREAKOUT_ACCEPTED | 52 | 54.6537 |
| cognition | test/1 | by_event | LEVEL_REJECTED | 169 | 103.0357 |
| cognition | test/1 | by_symbol | BTCUSDT | 51 | 48.3661 |
| cognition | test/1 | by_symbol | DOGEUSDT | 64 | 11.8624 |
| cognition | test/1 | by_symbol | ETHUSDT | 60 | 49.0846 |
| cognition | test/1 | by_symbol | SOLUSDT | 46 | 48.3763 |
| price_2h | test/1 | by_timeframe | 12h | 46 | 42.0515 |
| price_2h | test/1 | by_timeframe | 1d | 23 | 14.775 |
| price_2h | test/1 | by_timeframe | 2h | 86 | 61.4474 |
| price_2h | test/1 | by_timeframe | 4h | 68 | 15.5513 |
| price_2h | test/1 | by_event | BREAKOUT_ACCEPTED | 54 | 67.2711 |
| price_2h | test/1 | by_event | LEVEL_REJECTED | 169 | 66.5542 |
| price_2h | test/1 | by_symbol | BTCUSDT | 51 | 13.3405 |
| price_2h | test/1 | by_symbol | DOGEUSDT | 65 | 22.6232 |
| price_2h | test/1 | by_symbol | ETHUSDT | 61 | 50.1077 |
| price_2h | test/1 | by_symbol | SOLUSDT | 46 | 47.7538 |
| price_15m | test/1 | by_timeframe | 12h | 49 | 32.3552 |
| price_15m | test/1 | by_timeframe | 1d | 23 | 5.6068 |
| price_15m | test/1 | by_timeframe | 2h | 88 | 65.1309 |
| price_15m | test/1 | by_timeframe | 4h | 69 | 27.9067 |
| price_15m | test/1 | by_event | BREAKOUT_ACCEPTED | 55 | 70.1764 |
| price_15m | test/1 | by_event | LEVEL_REJECTED | 174 | 60.8232 |
| price_15m | test/1 | by_symbol | BTCUSDT | 53 | 23.5754 |
| price_15m | test/1 | by_symbol | DOGEUSDT | 68 | 25.791 |
| price_15m | test/1 | by_symbol | ETHUSDT | 62 | 34.3242 |
| price_15m | test/1 | by_symbol | SOLUSDT | 46 | 47.309 |
| armed_15m | test/1 | by_timeframe | 12h | 48 | 38.0673 |
| armed_15m | test/1 | by_timeframe | 1d | 23 | 14.082 |
| armed_15m | test/1 | by_timeframe | 2h | 86 | 60.2053 |
| armed_15m | test/1 | by_timeframe | 4h | 68 | 17.0245 |
| armed_15m | test/1 | by_event | BREAKOUT_ACCEPTED | 56 | 64.665 |
| armed_15m | test/1 | by_event | LEVEL_REJECTED | 169 | 64.7141 |
| armed_15m | test/1 | by_symbol | BTCUSDT | 51 | 13.6636 |
| armed_15m | test/1 | by_symbol | DOGEUSDT | 66 | 22.9216 |
| armed_15m | test/1 | by_symbol | ETHUSDT | 61 | 49.1735 |
| armed_15m | test/1 | by_symbol | SOLUSDT | 47 | 43.6205 |
| cognition | test/1.5 | by_timeframe | 12h | 44 | 37.4125 |
| cognition | test/1.5 | by_timeframe | 1d | 22 | 24.2262 |
| cognition | test/1.5 | by_timeframe | 2h | 85 | 37.1902 |
| cognition | test/1.5 | by_timeframe | 4h | 67 | 2.3678 |
| cognition | test/1.5 | by_event | BREAKOUT_ACCEPTED | 52 | 43.0076 |
| cognition | test/1.5 | by_event | LEVEL_REJECTED | 166 | 58.1891 |
| cognition | test/1.5 | by_symbol | BTCUSDT | 50 | 34.4371 |
| cognition | test/1.5 | by_symbol | DOGEUSDT | 63 | -5.6686 |
| cognition | test/1.5 | by_symbol | ETHUSDT | 59 | 37.571 |
| cognition | test/1.5 | by_symbol | SOLUSDT | 46 | 34.8572 |
| price_2h | test/1.5 | by_timeframe | 12h | 46 | 32.4298 |
| price_2h | test/1.5 | by_timeframe | 1d | 23 | 8.6926 |
| price_2h | test/1.5 | by_timeframe | 2h | 85 | 35.3427 |
| price_2h | test/1.5 | by_timeframe | 4h | 67 | 2.718 |
| price_2h | test/1.5 | by_event | BREAKOUT_ACCEPTED | 55 | 52.3567 |
| price_2h | test/1.5 | by_event | LEVEL_REJECTED | 166 | 26.8265 |
| price_2h | test/1.5 | by_symbol | BTCUSDT | 50 | 3.0925 |
| price_2h | test/1.5 | by_symbol | DOGEUSDT | 64 | 7.507 |
| price_2h | test/1.5 | by_symbol | ETHUSDT | 60 | 37.0779 |
| price_2h | test/1.5 | by_symbol | SOLUSDT | 47 | 31.5057 |
| price_15m | test/1.5 | by_timeframe | 12h | 48 | 16.0679 |
| price_15m | test/1.5 | by_timeframe | 1d | 23 | 2.1688 |
| price_15m | test/1.5 | by_timeframe | 2h | 88 | 31.7726 |
| price_15m | test/1.5 | by_timeframe | 4h | 68 | 10.7379 |
| price_15m | test/1.5 | by_event | BREAKOUT_ACCEPTED | 55 | 50.215 |
| price_15m | test/1.5 | by_event | LEVEL_REJECTED | 172 | 10.5322 |
| price_15m | test/1.5 | by_symbol | BTCUSDT | 52 | 0.9533 |
| price_15m | test/1.5 | by_symbol | DOGEUSDT | 68 | 9.1063 |
| price_15m | test/1.5 | by_symbol | ETHUSDT | 62 | 22.0547 |
| price_15m | test/1.5 | by_symbol | SOLUSDT | 45 | 28.6329 |
| armed_15m | test/1.5 | by_timeframe | 12h | 47 | 32.0782 |
| armed_15m | test/1.5 | by_timeframe | 1d | 23 | 8.298 |
| armed_15m | test/1.5 | by_timeframe | 2h | 85 | 34.8776 |
| armed_15m | test/1.5 | by_timeframe | 4h | 66 | 1.4851 |
| armed_15m | test/1.5 | by_event | BREAKOUT_ACCEPTED | 55 | 49.9502 |
| armed_15m | test/1.5 | by_event | LEVEL_REJECTED | 166 | 26.7886 |
| armed_15m | test/1.5 | by_symbol | BTCUSDT | 50 | 2.7997 |
| armed_15m | test/1.5 | by_symbol | DOGEUSDT | 65 | 7.7591 |
| armed_15m | test/1.5 | by_symbol | ETHUSDT | 60 | 37.8583 |
| armed_15m | test/1.5 | by_symbol | SOLUSDT | 46 | 28.3217 |
| cognition | full/1 | by_timeframe | 12h | 119 | 237.3081 |
| cognition | full/1 | by_timeframe | 1d | 56 | 97.1632 |
| cognition | full/1 | by_timeframe | 2h | 278 | 188.7113 |
| cognition | full/1 | by_timeframe | 4h | 197 | 123.8326 |
| cognition | full/1 | by_event | BREAKOUT_ACCEPTED | 175 | 226.2456 |
| cognition | full/1 | by_event | LEVEL_REJECTED | 475 | 420.7696 |
| cognition | full/1 | by_symbol | BTCUSDT | 158 | 78.5785 |
| cognition | full/1 | by_symbol | DOGEUSDT | 165 | 117.7583 |
| cognition | full/1 | by_symbol | ETHUSDT | 170 | 206.6791 |
| cognition | full/1 | by_symbol | SOLUSDT | 157 | 243.9992 |
| price_2h | full/1 | by_timeframe | 12h | 128 | 220.3416 |
| price_2h | full/1 | by_timeframe | 1d | 59 | 58.3435 |
| price_2h | full/1 | by_timeframe | 2h | 278 | 162.3262 |
| price_2h | full/1 | by_timeframe | 4h | 196 | 91.0127 |
| price_2h | full/1 | by_event | BREAKOUT_ACCEPTED | 179 | 231.1092 |
| price_2h | full/1 | by_event | LEVEL_REJECTED | 482 | 300.9148 |
| price_2h | full/1 | by_symbol | BTCUSDT | 162 | 4.7242 |
| price_2h | full/1 | by_symbol | DOGEUSDT | 169 | 136.3621 |
| price_2h | full/1 | by_symbol | ETHUSDT | 173 | 181.8228 |
| price_2h | full/1 | by_symbol | SOLUSDT | 157 | 209.1149 |
| price_15m | full/1 | by_timeframe | 12h | 133 | 156.8761 |
| price_15m | full/1 | by_timeframe | 1d | 60 | 13.015 |
| price_15m | full/1 | by_timeframe | 2h | 284 | 116.9912 |
| price_15m | full/1 | by_timeframe | 4h | 199 | 119.068 |
| price_15m | full/1 | by_event | BREAKOUT_ACCEPTED | 179 | 205.3444 |
| price_15m | full/1 | by_event | LEVEL_REJECTED | 497 | 200.6058 |
| price_15m | full/1 | by_symbol | BTCUSDT | 167 | -9.6637 |
| price_15m | full/1 | by_symbol | DOGEUSDT | 172 | 122.2911 |
| price_15m | full/1 | by_symbol | ETHUSDT | 178 | 126.7449 |
| price_15m | full/1 | by_symbol | SOLUSDT | 159 | 166.578 |
| armed_15m | full/1 | by_timeframe | 12h | 130 | 214.7729 |
| armed_15m | full/1 | by_timeframe | 1d | 59 | 57.7455 |
| armed_15m | full/1 | by_timeframe | 2h | 278 | 151.5993 |
| armed_15m | full/1 | by_timeframe | 4h | 197 | 97.3609 |
| armed_15m | full/1 | by_event | BREAKOUT_ACCEPTED | 180 | 222.8476 |
| armed_15m | full/1 | by_event | LEVEL_REJECTED | 484 | 298.631 |
| armed_15m | full/1 | by_symbol | BTCUSDT | 163 | 1.336 |
| armed_15m | full/1 | by_symbol | DOGEUSDT | 170 | 135.7459 |
| armed_15m | full/1 | by_symbol | ETHUSDT | 174 | 183.0208 |
| armed_15m | full/1 | by_symbol | SOLUSDT | 157 | 201.3758 |
| cognition | full/1.5 | by_timeframe | 12h | 118 | 173.5718 |
| cognition | full/1.5 | by_timeframe | 1d | 56 | 80.1614 |
| cognition | full/1.5 | by_timeframe | 2h | 274 | 59.1457 |
| cognition | full/1.5 | by_timeframe | 4h | 194 | 41.5135 |
| cognition | full/1.5 | by_event | BREAKOUT_ACCEPTED | 174 | 135.4177 |
| cognition | full/1.5 | by_event | LEVEL_REJECTED | 468 | 218.9747 |
| cognition | full/1.5 | by_symbol | BTCUSDT | 156 | 8.808 |
| cognition | full/1.5 | by_symbol | DOGEUSDT | 163 | 59.5868 |
| cognition | full/1.5 | by_symbol | ETHUSDT | 169 | 126.1776 |
| cognition | full/1.5 | by_symbol | SOLUSDT | 154 | 159.8199 |
| price_2h | full/1.5 | by_timeframe | 12h | 127 | 164.7761 |
| price_2h | full/1.5 | by_timeframe | 1d | 59 | 55.0027 |
| price_2h | full/1.5 | by_timeframe | 2h | 274 | 56.4464 |
| price_2h | full/1.5 | by_timeframe | 4h | 194 | 34.23 |
| price_2h | full/1.5 | by_event | BREAKOUT_ACCEPTED | 179 | 154.9532 |
| price_2h | full/1.5 | by_event | LEVEL_REJECTED | 475 | 155.502 |
| price_2h | full/1.5 | by_symbol | BTCUSDT | 160 | -46.349 |
| price_2h | full/1.5 | by_symbol | DOGEUSDT | 166 | 86.1991 |
| price_2h | full/1.5 | by_symbol | ETHUSDT | 173 | 107.4711 |
| price_2h | full/1.5 | by_symbol | SOLUSDT | 155 | 163.1339 |
| price_15m | full/1.5 | by_timeframe | 12h | 131 | 93.4776 |
| price_15m | full/1.5 | by_timeframe | 1d | 59 | 5.7746 |
| price_15m | full/1.5 | by_timeframe | 2h | 281 | 17.3985 |
| price_15m | full/1.5 | by_timeframe | 4h | 198 | 54.2883 |
| price_15m | full/1.5 | by_event | BREAKOUT_ACCEPTED | 180 | 125.4214 |
| price_15m | full/1.5 | by_event | LEVEL_REJECTED | 489 | 45.5176 |
| price_15m | full/1.5 | by_symbol | BTCUSDT | 163 | -60.7263 |
| price_15m | full/1.5 | by_symbol | DOGEUSDT | 171 | 69.7894 |
| price_15m | full/1.5 | by_symbol | ETHUSDT | 177 | 58.2274 |
| price_15m | full/1.5 | by_symbol | SOLUSDT | 158 | 103.6484 |
| armed_15m | full/1.5 | by_timeframe | 12h | 128 | 155.9471 |
| armed_15m | full/1.5 | by_timeframe | 1d | 58 | 45.8855 |
| armed_15m | full/1.5 | by_timeframe | 2h | 274 | 47.9773 |
| armed_15m | full/1.5 | by_timeframe | 4h | 194 | 37.778 |
| armed_15m | full/1.5 | by_event | BREAKOUT_ACCEPTED | 181 | 145.5182 |
| armed_15m | full/1.5 | by_event | LEVEL_REJECTED | 473 | 142.0697 |
| armed_15m | full/1.5 | by_symbol | BTCUSDT | 160 | -51.6418 |
| armed_15m | full/1.5 | by_symbol | DOGEUSDT | 167 | 85.0822 |
| armed_15m | full/1.5 | by_symbol | ETHUSDT | 172 | 100.2883 |
| armed_15m | full/1.5 | by_symbol | SOLUSDT | 155 | 153.8592 |

### Acceptance gates
- price_15m/dev/1: FAIL
  - dev/1: 场景净收益 +194.6402 < 基线 +278.2201
  - dev/1/by_timeframe/12h: 净 +108.7288 < 基线 +132.3545
  - dev/1/by_timeframe/1d: 净 +3.7335 < 基线 +33.1410
  - dev/1/by_timeframe/2h: 净 +10.3182 < 基线 +42.6566
  - dev/1/by_event/BREAKOUT_ACCEPTED: 净 +90.0600 < 基线 +102.4425
  - dev/1/by_event/LEVEL_REJECTED: 净 +104.5801 < 基线 +175.7776
  - dev/1/by_symbol/BTCUSDT: 净 -36.2316 < 基线 -20.2946
  - dev/1/by_symbol/DOGEUSDT: 净 +79.2454 < 基线 +92.9236
  - dev/1/by_symbol/ETHUSDT: 净 +67.4251 < 基线 +89.7555
  - dev/1/by_symbol/SOLUSDT: 净 +84.2014 < 基线 +115.8356
- armed_15m/dev/1: FAIL
  - dev/1: 场景净收益 +263.4485 < 基线 +278.2201
  - dev/1/by_timeframe/12h: 净 +130.5051 < 基线 +132.3545
  - dev/1/by_timeframe/2h: 净 +34.5878 < 基线 +42.6566
  - dev/1/by_timeframe/4h: 净 +64.9163 < 基线 +70.0680
  - dev/1/by_event/BREAKOUT_ACCEPTED: 净 +92.9299 < 基线 +102.4425
  - dev/1/by_event/LEVEL_REJECTED: 净 +170.5185 < 基线 +175.7776
  - dev/1/by_symbol/BTCUSDT: 净 -25.0665 < 基线 -20.2946
  - dev/1/by_symbol/DOGEUSDT: 净 +91.7689 < 基线 +92.9236
  - dev/1/by_symbol/ETHUSDT: 净 +84.8521 < 基线 +89.7555
  - dev/1/by_symbol/SOLUSDT: 净 +111.8939 < 基线 +115.8356
- price_15m/dev/1.5: FAIL
  - dev/1.5: 场景净收益 +91.3608 < 基线 +188.0812
  - dev/1.5/by_timeframe/12h: 净 +68.9831 < 基线 +111.7395
  - dev/1.5/by_timeframe/1d: 净 +3.8923 < 基线 +36.2785
  - dev/1.5/by_timeframe/2h: 净 -25.0202 < 基线 +0.3888
  - dev/1.5/by_event/BREAKOUT_ACCEPTED: 净 +58.9016 < 基线 +70.8467
  - dev/1.5/by_event/LEVEL_REJECTED: 净 +32.4592 < 基线 +117.2344
  - dev/1.5/by_symbol/BTCUSDT: 净 -61.6732 < 基线 -49.3308
  - dev/1.5/by_symbol/DOGEUSDT: 净 +57.8723 < 基线 +73.7631
  - dev/1.5/by_symbol/ETHUSDT: 净 +35.4171 < 基线 +53.8873
  - dev/1.5/by_symbol/SOLUSDT: 净 +59.7446 < 基线 +109.7616
- armed_15m/dev/1.5: FAIL
  - dev/1.5: 场景净收益 +177.0452 < 基线 +188.0812
  - dev/1.5/by_timeframe/12h: 净 +105.6764 < 基线 +111.7395
  - dev/1.5/by_timeframe/1d: 净 +35.3817 < 基线 +36.2785
  - dev/1.5/by_timeframe/2h: 净 -4.3707 < 基线 +0.3888
  - dev/1.5/by_event/BREAKOUT_ACCEPTED: 净 +67.8773 < 基线 +70.8467
  - dev/1.5/by_event/LEVEL_REJECTED: 净 +109.1679 < 基线 +117.2344
  - dev/1.5/by_symbol/BTCUSDT: 净 -53.7651 < 基线 -49.3308
  - dev/1.5/by_symbol/DOGEUSDT: 净 +72.9382 < 基线 +73.7631
  - dev/1.5/by_symbol/SOLUSDT: 净 +103.6692 < 基线 +109.7616
- price_15m/test/1: FAIL
  - test/1: 场景净收益 +130.9996 < 基线 +133.8253
  - test/1/by_timeframe/12h: 净 +32.3552 < 基线 +42.0515
  - test/1/by_timeframe/1d: 净 +5.6068 < 基线 +14.7750
  - test/1/by_event/LEVEL_REJECTED: 净 +60.8232 < 基线 +66.5542
  - test/1/by_symbol/ETHUSDT: 净 +34.3242 < 基线 +50.1077
  - test/1/by_symbol/SOLUSDT: 净 +47.3090 < 基线 +47.7538
- armed_15m/test/1: FAIL
  - test/1: 场景净收益 +129.3791 < 基线 +133.8253
  - test/1/by_timeframe/12h: 净 +38.0673 < 基线 +42.0515
  - test/1/by_timeframe/1d: 净 +14.0820 < 基线 +14.7750
  - test/1/by_timeframe/2h: 净 +60.2053 < 基线 +61.4474
  - test/1/by_event/BREAKOUT_ACCEPTED: 净 +64.6650 < 基线 +67.2711
  - test/1/by_event/LEVEL_REJECTED: 净 +64.7141 < 基线 +66.5542
  - test/1/by_symbol/ETHUSDT: 净 +49.1735 < 基线 +50.1077
  - test/1/by_symbol/SOLUSDT: 净 +43.6205 < 基线 +47.7538
- price_15m/test/1.5: FAIL
  - test/1.5: 场景净收益 +60.7472 < 基线 +79.1831
  - test/1.5/by_timeframe/12h: 净 +16.0679 < 基线 +32.4298
  - test/1.5/by_timeframe/1d: 净 +2.1688 < 基线 +8.6926
  - test/1.5/by_timeframe/2h: 净 +31.7726 < 基线 +35.3427
  - test/1.5/by_event/BREAKOUT_ACCEPTED: 净 +50.2150 < 基线 +52.3567
  - test/1.5/by_event/LEVEL_REJECTED: 净 +10.5322 < 基线 +26.8265
  - test/1.5/by_symbol/BTCUSDT: 净 +0.9533 < 基线 +3.0925
  - test/1.5/by_symbol/ETHUSDT: 净 +22.0547 < 基线 +37.0779
  - test/1.5/by_symbol/SOLUSDT: 净 +28.6329 < 基线 +31.5057
- armed_15m/test/1.5: FAIL
  - test/1.5: 场景净收益 +76.7389 < 基线 +79.1831
  - test/1.5/by_timeframe/12h: 净 +32.0782 < 基线 +32.4298
  - test/1.5/by_timeframe/1d: 净 +8.2980 < 基线 +8.6926
  - test/1.5/by_timeframe/2h: 净 +34.8776 < 基线 +35.3427
  - test/1.5/by_timeframe/4h: 净 +1.4851 < 基线 +2.7180
  - test/1.5/by_event/BREAKOUT_ACCEPTED: 净 +49.9502 < 基线 +52.3567
  - test/1.5/by_event/LEVEL_REJECTED: 净 +26.7886 < 基线 +26.8265
  - test/1.5/by_symbol/BTCUSDT: 净 +2.7997 < 基线 +3.0925
  - test/1.5/by_symbol/SOLUSDT: 净 +28.3217 < 基线 +31.5057
- price_15m/full/1: FAIL
  - full/1: 场景净收益 +405.9502 < 基线 +532.0240
  - full/1/by_timeframe/12h: 净 +156.8761 < 基线 +220.3416
  - full/1/by_timeframe/1d: 净 +13.0150 < 基线 +58.3435
  - full/1/by_timeframe/2h: 净 +116.9912 < 基线 +162.3262
  - full/1/by_event/BREAKOUT_ACCEPTED: 净 +205.3444 < 基线 +231.1092
  - full/1/by_event/LEVEL_REJECTED: 净 +200.6058 < 基线 +300.9148
  - full/1/by_symbol/BTCUSDT: 净 -9.6637 < 基线 +4.7242
  - full/1/by_symbol/DOGEUSDT: 净 +122.2911 < 基线 +136.3621
  - full/1/by_symbol/ETHUSDT: 净 +126.7449 < 基线 +181.8228
  - full/1/by_symbol/SOLUSDT: 净 +166.5780 < 基线 +209.1149
- armed_15m/full/1: FAIL
  - full/1: 场景净收益 +521.4786 < 基线 +532.0240
  - full/1/by_timeframe/12h: 净 +214.7729 < 基线 +220.3416
  - full/1/by_timeframe/1d: 净 +57.7455 < 基线 +58.3435
  - full/1/by_timeframe/2h: 净 +151.5993 < 基线 +162.3262
  - full/1/by_event/BREAKOUT_ACCEPTED: 净 +222.8476 < 基线 +231.1092
  - full/1/by_event/LEVEL_REJECTED: 净 +298.6310 < 基线 +300.9148
  - full/1/by_symbol/BTCUSDT: 净 +1.3360 < 基线 +4.7242
  - full/1/by_symbol/DOGEUSDT: 净 +135.7459 < 基线 +136.3621
  - full/1/by_symbol/SOLUSDT: 净 +201.3758 < 基线 +209.1149
- price_15m/full/1.5: FAIL
  - full/1.5: 场景净收益 +170.9390 < 基线 +310.4551
  - full/1.5/by_timeframe/12h: 净 +93.4776 < 基线 +164.7761
  - full/1.5/by_timeframe/1d: 净 +5.7746 < 基线 +55.0027
  - full/1.5/by_timeframe/2h: 净 +17.3985 < 基线 +56.4464
  - full/1.5/by_event/BREAKOUT_ACCEPTED: 净 +125.4214 < 基线 +154.9532
  - full/1.5/by_event/LEVEL_REJECTED: 净 +45.5176 < 基线 +155.5020
  - full/1.5/by_symbol/BTCUSDT: 净 -60.7263 < 基线 -46.3490
  - full/1.5/by_symbol/DOGEUSDT: 净 +69.7894 < 基线 +86.1991
  - full/1.5/by_symbol/ETHUSDT: 净 +58.2274 < 基线 +107.4711
  - full/1.5/by_symbol/SOLUSDT: 净 +103.6484 < 基线 +163.1339
- armed_15m/full/1.5: FAIL
  - full/1.5: 场景净收益 +287.5879 < 基线 +310.4551
  - full/1.5/by_timeframe/12h: 净 +155.9471 < 基线 +164.7761
  - full/1.5/by_timeframe/1d: 净 +45.8855 < 基线 +55.0027
  - full/1.5/by_timeframe/2h: 净 +47.9773 < 基线 +56.4464
  - full/1.5/by_event/BREAKOUT_ACCEPTED: 净 +145.5182 < 基线 +154.9532
  - full/1.5/by_event/LEVEL_REJECTED: 净 +142.0697 < 基线 +155.5020
  - full/1.5/by_symbol/BTCUSDT: 净 -51.6418 < 基线 -46.3490
  - full/1.5/by_symbol/DOGEUSDT: 净 +85.0822 < 基线 +86.1991
  - full/1.5/by_symbol/ETHUSDT: 净 +100.2883 < 基线 +107.4711
  - full/1.5/by_symbol/SOLUSDT: 净 +153.8592 < 基线 +163.1339

| Comparison | Shared closed | Earlier exits | Baseline only | Candidate only | Shared R change | Median earlier hours |
|---|---|---|---|---|---|---|
| price_15m/dev/1 | 436 | 70 | 2 | 12 | -11.950933 | 1.875 |
| armed_15m/dev/1 | 437 | 19 | 1 | 2 | -2.45197 | 0.75 |
| price_15m/dev/1.5 | 428 | 69 | 5 | 13 | -9.761494 | 1.75 |
| armed_15m/dev/1.5 | 431 | 19 | 2 | 1 | -0.415412 | 0.5 |
| price_15m/test/1 | 220 | 51 | 3 | 9 | -3.900741 | 1.75 |
| armed_15m/test/1 | 223 | 13 | 0 | 2 | -0.480822 | 1.5 |
| price_15m/test/1.5 | 218 | 49 | 3 | 9 | -4.294736 | 1.75 |
| armed_15m/test/1.5 | 220 | 11 | 1 | 1 | -0.546474 | 1.5 |
| price_15m/full/1 | 656 | 120 | 5 | 20 | -15.876778 | 1.875 |
| armed_15m/full/1 | 659 | 32 | 2 | 5 | -2.932792 | 1 |
| price_15m/full/1.5 | 648 | 118 | 6 | 21 | -14.072624 | 1.75 |
| armed_15m/full/1.5 | 652 | 30 | 2 | 2 | -0.961886 | 0.75 |

## Entry evidence audit

Raw fine-stream events: 3178. Exact-zone matching is descriptive, not fixed-opportunity causal attribution. Rejection extrema are not immediately tradable evidence.

| Campaign | Event | Fine events | Matched | Unmatched | Median lead min | Median improvement bps | Median anchor-to-signal min |
|---|---|---|---|---|---|---|---|
| BTCUSDT@2h | BREAKOUT_ACCEPTED | 60 | 0 | 60 | - | - | 30 |
| BTCUSDT@2h | LEVEL_REJECTED | 350 | 83 | 267 | 105 | 17.524969 | 15 |
| BTCUSDT@4h | BREAKOUT_ACCEPTED | 41 | 7 | 34 | 225 | 21.511686 | 30 |
| BTCUSDT@4h | LEVEL_REJECTED | 164 | 55 | 109 | 105 | 33.37398 | 15 |
| ETHUSDT@2h | BREAKOUT_ACCEPTED | 70 | 0 | 70 | - | - | 30 |
| ETHUSDT@2h | LEVEL_REJECTED | 563 | 132 | 431 | 97.5 | 27.423147 | 15 |
| ETHUSDT@4h | BREAKOUT_ACCEPTED | 32 | 5 | 27 | 240 | 258.40228 | 30 |
| ETHUSDT@4h | LEVEL_REJECTED | 303 | 102 | 201 | 120 | 43.581187 | 15 |
| SOLUSDT@2h | BREAKOUT_ACCEPTED | 67 | 0 | 67 | - | - | 30 |
| SOLUSDT@2h | LEVEL_REJECTED | 484 | 124 | 360 | 90 | 35.68178 | 15 |
| SOLUSDT@4h | BREAKOUT_ACCEPTED | 25 | 2 | 23 | 210 | 269.48494 | 30 |
| SOLUSDT@4h | LEVEL_REJECTED | 250 | 72 | 178 | 120 | 46.192107 | 15 |
| DOGEUSDT@2h | BREAKOUT_ACCEPTED | 60 | 0 | 60 | - | - | 30 |
| DOGEUSDT@2h | LEVEL_REJECTED | 391 | 99 | 292 | 90 | 53.326562 | 15 |
| DOGEUSDT@4h | BREAKOUT_ACCEPTED | 31 | 4 | 27 | 217.5 | 222.078439 | 30 |
| DOGEUSDT@4h | LEVEL_REJECTED | 287 | 88 | 199 | 105 | 38.575155 | 15 |

## Qualification and protection audit

Six scenes matched prior trades exactly. Protective fills include initial, structure and lock stops. Immediate qualification-and-exit does not write back persistent armed state. No modeled missed stop does not establish live exchange reliability.

| Scene | Stop checks | Stop hits | qualification_transitions | protection_updates | fills_after_protection_update | qualified_peak_updates | giveback_exit_decisions | qualification_and_exit_same_observation |
|---|---|---|---|---|---|---|---|---|
| dev/1 | 98391 | 400 | 110 | 854 | 287 | 302 | 30 | 24 |
| dev/1.5 | 98731 | 395 | 109 | 866 | 284 | 319 | 30 | 23 |
| test/1 | 56911 | 199 | 56 | 382 | 158 | 70 | 22 | 17 |
| test/1.5 | 56692 | 198 | 56 | 380 | 157 | 69 | 21 | 16 |
| full/1 | 156445 | 600 | 166 | 1236 | 446 | 372 | 51 | 40 |
| full/1.5 | 156709 | 594 | 165 | 1247 | 443 | 388 | 50 | 38 |

### Recorded fill examples: dev/1

| Campaign | Trade | Direction | Protection observed ms | Execution bar start ms | Stop | Fill | Gap |
|---|---|---|---|---|---|---|---|
| DOGEUSDT@2h | DOGEUSDT@2h#6 | 1 | 1729562400000 | 1729565100000 | 0.144353 | 0.144324 | False |
| ETHUSDT@2h | ETHUSDT@2h#10 | -1 | 1729785600000 | 1729803600000 | 2547.80864 | 2548.318202 | False |
| BTCUSDT@4h | BTCUSDT@4h#22 | -1 | 1729915200000 | 1730032200000 | 67468.456834 | 67481.950525 | False |
| ETHUSDT@2h | ETHUSDT@2h#29 | 1 | 1730246400000 | 1730250000000 | 2604.013289 | 2603.492486 | False |
| BTCUSDT@2h | BTCUSDT@2h#34 | -1 | 1730390400000 | 1730391300000 | 70795.318085 | 70809.477149 | False |
| SOLUSDT@4h | SOLUSDT@4h#32 | -1 | 1730649600000 | 1730816100000 | 165.156965 | 165.189996 | False |
| DOGEUSDT@2h | DOGEUSDT@2h#40 | 1 | 1730880000000 | 1730880000000 | 0.195219 | 0.19518 | False |
| SOLUSDT@2h | SOLUSDT@2h#45 | 1 | 1731362400000 | 1731370500000 | 215.744693 | 215.701544 | False |
| SOLUSDT@2h | SOLUSDT@2h#49 | 1 | 1731736800000 | 1731759300000 | 216.362062 | 216.31879 | False |
| DOGEUSDT@2h | DOGEUSDT@2h#51 | 1 | 1731780000000 | 1731807000000 | 0.359926 | 0.359854 | False |
| ETHUSDT@2h | ETHUSDT@2h#64 | 1 | 1731974400000 | 1731974400000 | 3200.937585 | 3200.297398 | False |
| ETHUSDT@4h | ETHUSDT@4h#66 | -1 | 1732132800000 | 1732190400000 | 3168.818786 | 3169.45255 | False |

### Recorded fill examples: dev/1.5

| Campaign | Trade | Direction | Protection observed ms | Execution bar start ms | Stop | Fill | Gap |
|---|---|---|---|---|---|---|---|
| DOGEUSDT@2h | DOGEUSDT@2h#6 | 1 | 1729562400000 | 1729565100000 | 0.14438 | 0.144315 | False |
| ETHUSDT@2h | ETHUSDT@2h#10 | -1 | 1729785600000 | 1729803600000 | 2547.80864 | 2548.955154 | False |
| BTCUSDT@4h | BTCUSDT@4h#22 | -1 | 1729915200000 | 1730032200000 | 67468.456834 | 67498.817639 | False |
| ETHUSDT@2h | ETHUSDT@2h#29 | 1 | 1730246400000 | 1730250000000 | 2604.013289 | 2602.841483 | False |
| BTCUSDT@2h | BTCUSDT@2h#34 | -1 | 1730390400000 | 1730391300000 | 70781.776216 | 70813.628016 | False |
| SOLUSDT@4h | SOLUSDT@4h#32 | -1 | 1730649600000 | 1730816100000 | 165.156965 | 165.231286 | False |
| DOGEUSDT@2h | DOGEUSDT@2h#40 | 1 | 1730880000000 | 1730880000000 | 0.195219 | 0.195131 | False |
| SOLUSDT@2h | SOLUSDT@2h#45 | 1 | 1731362400000 | 1731370500000 | 215.744693 | 215.647608 | False |
| SOLUSDT@2h | SOLUSDT@2h#49 | 1 | 1731736800000 | 1731759300000 | 216.402027 | 216.304646 | False |
| DOGEUSDT@2h | DOGEUSDT@2h#51 | 1 | 1731780000000 | 1731807000000 | 0.359926 | 0.359764 | False |
| ETHUSDT@2h | ETHUSDT@2h#64 | 1 | 1731974400000 | 1731974400000 | 3201.527477 | 3200.08679 | False |
| ETHUSDT@4h | ETHUSDT@4h#66 | -1 | 1732132800000 | 1732190400000 | 3168.818786 | 3170.244755 | False |

### Recorded fill examples: test/1

| Campaign | Trade | Direction | Protection observed ms | Execution bar start ms | Stop | Fill | Gap |
|---|---|---|---|---|---|---|---|
| BTCUSDT@12h | BTCUSDT@12h#1 | -1 | 1773489600000 | 1773631800000 | 74064.831263 | 74079.644229 | False |
| SOLUSDT@12h | SOLUSDT@12h#3 | -1 | 1773489600000 | 1773631800000 | 93.291356 | 93.310014 | False |
| SOLUSDT@2h | SOLUSDT@2h#29 | 1 | 1774000800000 | 1774027800000 | 88.31282 | 88.295157 | False |
| ETHUSDT@4h | ETHUSDT@4h#35 | -1 | 1774252800000 | 1774263600000 | 2075.665815 | 2076.080948 | False |
| BTCUSDT@12h | BTCUSDT@12h#25 | -1 | 1774051200000 | 1774273500000 | 71525.075963 | 71539.380978 | False |
| SOLUSDT@12h | SOLUSDT@12h#37 | -1 | 1774310400000 | 1774414800000 | 92.278178 | 92.296633 | False |
| ETHUSDT@2h | ETHUSDT@2h#42 | -1 | 1774555200000 | 1774555200000 | 2075.832138 | 2076.247304 | False |
| SOLUSDT@4h | SOLUSDT@4h#48 | -1 | 1774800000000 | 1774838700000 | 83.331502 | 83.348168 | False |
| ETHUSDT@4h | ETHUSDT@4h#46 | -1 | 1774742400000 | 1774844100000 | 2050.118499 | 2050.528523 | False |
| DOGEUSDT@4h | DOGEUSDT@4h#68 | 1 | 1775073600000 | 1775091600000 | 0.091639 | 0.09162 | False |
| ETHUSDT@4h | ETHUSDT@4h#70 | 1 | 1775073600000 | 1775093400000 | 2102.940126 | 2102.519538 | False |
| BTCUSDT@12h | BTCUSDT@12h#56 | 1 | 1775044800000 | 1775104200000 | 66200.376027 | 66187.135952 | False |

### Recorded fill examples: test/1.5

| Campaign | Trade | Direction | Protection observed ms | Execution bar start ms | Stop | Fill | Gap |
|---|---|---|---|---|---|---|---|
| BTCUSDT@12h | BTCUSDT@12h#1 | -1 | 1773489600000 | 1773631800000 | 74064.831263 | 74098.160437 | False |
| SOLUSDT@12h | SOLUSDT@12h#3 | -1 | 1773489600000 | 1773631800000 | 93.291356 | 93.333337 | False |
| SOLUSDT@2h | SOLUSDT@2h#29 | 1 | 1774000800000 | 1774027800000 | 88.31282 | 88.273079 | False |
| ETHUSDT@4h | ETHUSDT@4h#35 | -1 | 1774252800000 | 1774263600000 | 2075.665815 | 2076.599865 | False |
| BTCUSDT@12h | BTCUSDT@12h#25 | -1 | 1774051200000 | 1774273500000 | 71525.075963 | 71557.262247 | False |
| SOLUSDT@12h | SOLUSDT@12h#37 | -1 | 1774310400000 | 1774414800000 | 92.278178 | 92.319703 | False |
| ETHUSDT@2h | ETHUSDT@2h#42 | -1 | 1774555200000 | 1774555200000 | 2075.832138 | 2076.766263 | False |
| SOLUSDT@4h | SOLUSDT@4h#48 | -1 | 1774800000000 | 1774838700000 | 83.331502 | 83.369001 | False |
| ETHUSDT@4h | ETHUSDT@4h#46 | -1 | 1774742400000 | 1774844100000 | 2050.118499 | 2051.041053 | False |
| DOGEUSDT@4h | DOGEUSDT@4h#68 | 1 | 1775073600000 | 1775091600000 | 0.091639 | 0.091597 | False |
| ETHUSDT@4h | ETHUSDT@4h#70 | 1 | 1775073600000 | 1775093400000 | 2102.940126 | 2101.993803 | False |
| BTCUSDT@12h | BTCUSDT@12h#56 | 1 | 1775044800000 | 1775104200000 | 66200.376027 | 66170.585858 | False |

### Recorded fill examples: full/1

| Campaign | Trade | Direction | Protection observed ms | Execution bar start ms | Stop | Fill | Gap |
|---|---|---|---|---|---|---|---|
| DOGEUSDT@2h | DOGEUSDT@2h#6 | 1 | 1729562400000 | 1729565100000 | 0.144353 | 0.144324 | False |
| ETHUSDT@2h | ETHUSDT@2h#10 | -1 | 1729785600000 | 1729803600000 | 2547.80864 | 2548.318202 | False |
| BTCUSDT@4h | BTCUSDT@4h#22 | -1 | 1729915200000 | 1730032200000 | 67468.456834 | 67481.950525 | False |
| ETHUSDT@2h | ETHUSDT@2h#29 | 1 | 1730246400000 | 1730250000000 | 2604.013289 | 2603.492486 | False |
| BTCUSDT@2h | BTCUSDT@2h#34 | -1 | 1730390400000 | 1730391300000 | 70795.318085 | 70809.477149 | False |
| SOLUSDT@4h | SOLUSDT@4h#32 | -1 | 1730649600000 | 1730816100000 | 165.156965 | 165.189996 | False |
| DOGEUSDT@2h | DOGEUSDT@2h#40 | 1 | 1730880000000 | 1730880000000 | 0.195219 | 0.19518 | False |
| SOLUSDT@2h | SOLUSDT@2h#45 | 1 | 1731362400000 | 1731370500000 | 215.744693 | 215.701544 | False |
| SOLUSDT@2h | SOLUSDT@2h#49 | 1 | 1731736800000 | 1731759300000 | 216.362062 | 216.31879 | False |
| DOGEUSDT@2h | DOGEUSDT@2h#51 | 1 | 1731780000000 | 1731807000000 | 0.359926 | 0.359854 | False |
| ETHUSDT@2h | ETHUSDT@2h#64 | 1 | 1731974400000 | 1731974400000 | 3200.937585 | 3200.297398 | False |
| ETHUSDT@4h | ETHUSDT@4h#66 | -1 | 1732132800000 | 1732190400000 | 3168.818786 | 3169.45255 | False |

### Recorded fill examples: full/1.5

| Campaign | Trade | Direction | Protection observed ms | Execution bar start ms | Stop | Fill | Gap |
|---|---|---|---|---|---|---|---|
| DOGEUSDT@2h | DOGEUSDT@2h#6 | 1 | 1729562400000 | 1729565100000 | 0.14438 | 0.144315 | False |
| ETHUSDT@2h | ETHUSDT@2h#10 | -1 | 1729785600000 | 1729803600000 | 2547.80864 | 2548.955154 | False |
| BTCUSDT@4h | BTCUSDT@4h#22 | -1 | 1729915200000 | 1730032200000 | 67468.456834 | 67498.817639 | False |
| ETHUSDT@2h | ETHUSDT@2h#29 | 1 | 1730246400000 | 1730250000000 | 2604.013289 | 2602.841483 | False |
| BTCUSDT@2h | BTCUSDT@2h#34 | -1 | 1730390400000 | 1730391300000 | 70781.776216 | 70813.628016 | False |
| SOLUSDT@4h | SOLUSDT@4h#32 | -1 | 1730649600000 | 1730816100000 | 165.156965 | 165.231286 | False |
| DOGEUSDT@2h | DOGEUSDT@2h#40 | 1 | 1730880000000 | 1730880000000 | 0.195219 | 0.195131 | False |
| SOLUSDT@2h | SOLUSDT@2h#45 | 1 | 1731362400000 | 1731370500000 | 215.744693 | 215.647608 | False |
| SOLUSDT@2h | SOLUSDT@2h#49 | 1 | 1731736800000 | 1731759300000 | 216.402027 | 216.304646 | False |
| DOGEUSDT@2h | DOGEUSDT@2h#51 | 1 | 1731780000000 | 1731807000000 | 0.359926 | 0.359764 | False |
| ETHUSDT@2h | ETHUSDT@2h#64 | 1 | 1731974400000 | 1731974400000 | 3201.527477 | 3200.08679 | False |
| ETHUSDT@4h | ETHUSDT@4h#66 | -1 | 1732132800000 | 1732190400000 | 3168.818786 | 3170.244755 | False |

## Structure entry decoupled results

| Variant | Segment | Cost | N | Net USDT | Max DD % |
|---|---|---|---|---|---|
| baseline | dev | 1 | 443 | 340.15404 | 7.819208 |
| decoupled | dev | 1 | 424 | 363.791753 | 8.962125 |
| reentry_observation | dev | 1 | 422 | 383.242308 | 8.673804 |
| reentry_management_opt_in | dev | 1 | 427 | 287.000148 | 8.158919 |
| baseline | test | 1 | 226 | 163.491803 | 16.570656 |
| decoupled | test | 1 | 214 | 159.04062 | 12.964779 |
| reentry_observation | test | 1 | 216 | 167.387737 | 14.619901 |
| reentry_management_opt_in | test | 1 | 217 | 137.196955 | 14.448008 |
| baseline | full | 1 | 669 | 680.783025 | 16.499408 |
| decoupled | full | 1 | 640 | 715.73639 | 12.922 |
| reentry_observation | full | 1 | 639 | 747.310341 | 14.734426 |
| reentry_management_opt_in | full | 1 | 644 | 548.510516 | 14.865307 |
| baseline | full | 1.5 | 665 | 354.901763 | 22.606815 |
| decoupled | full | 1.5 | 632 | 374.098833 | 21.600806 |
| reentry_observation | full | 1.5 | 629 | 384.608783 | 24.202343 |
| reentry_management_opt_in | full | 1.5 | 633 | 272.958868 | 23.781007 |

## Historical pre-decoupling attribution

Historical HEAD: 4a730fba61fcb46a0478f928b84427ccc4ed8375. Do not interpret as current cleaned-code performance.

| Metric path | Historical value |
|---|---|
| runs/0/variant | baseline |
| runs/0/segment | full |
| runs/0/n | 669 |
| runs/0/net | 680.783025 |
| runs/0/winrate | 0.455904 |
| runs/0/top5 | 271.953384 |
| runs/0/net_ex_top5 | 408.829641 |
| runs/0/realized_r_median | -0.284537 |
| runs/0/exits/PROTECTIVE_STOP | 590 |
| runs/0/exits/v21 | 66 |
| runs/0/exits/§8.5 | 13 |
| runs/0/max_dd_pct | 16.499408 |
| runs/1/variant | target_only |
| runs/1/segment | full |
| runs/1/n | 639 |
| runs/1/net | 744.087291 |
| runs/1/winrate | 0.464789 |
| runs/1/top5 | 282.003157 |
| runs/1/net_ex_top5 | 462.084134 |
| runs/1/realized_r_median | -0.216308 |
| runs/1/exits/PROTECTIVE_STOP | 563 |
| runs/1/exits/v21 | 67 |
| runs/1/exits/§8.5 | 9 |
| runs/1/max_dd_pct | 12.923126 |
| runs/2/variant | rr_only |
| runs/2/segment | full |
| runs/2/n | 426 |
| runs/2/net | 427.165578 |
| runs/2/winrate | 0.380282 |
| runs/2/top5 | 207.945423 |
| runs/2/net_ex_top5 | 219.220155 |
| runs/2/realized_r_median | -0.425392 |
| runs/2/exits/PROTECTIVE_STOP | 401 |
| runs/2/exits/v21 | 12 |
| runs/2/exits/§8.5 | 13 |
| runs/2/max_dd_pct | 15.280006 |
| runs/3/variant | fixed_no_fill |
| runs/3/segment | full |
| runs/3/n | 402 |
| runs/3/net | 488.75466 |
| runs/3/winrate | 0.383085 |
| runs/3/top5 | 234.013388 |
| runs/3/net_ex_top5 | 254.741272 |
| runs/3/realized_r_median | -0.425392 |
| runs/3/exits/PROTECTIVE_STOP | 380 |
| runs/3/exits/v21 | 13 |
| runs/3/exits/§8.5 | 9 |
| runs/3/max_dd_pct | 16.19539 |
| runs/4/variant | fixed |
| runs/4/segment | full |
| runs/4/n | 399 |
| runs/4/net | 488.56976 |
| runs/4/winrate | 0.380952 |
| runs/4/top5 | 234.013388 |
| runs/4/net_ex_top5 | 254.556372 |
| runs/4/realized_r_median | -0.449727 |
| runs/4/exits/PROTECTIVE_STOP | 378 |
| runs/4/exits/v21 | 12 |
| runs/4/exits/§8.5 | 9 |
| runs/4/max_dd_pct | 16.199561 |
| runs/5/variant | reentry_stop_only |
| runs/5/segment | full |
| runs/5/n | 669 |
| runs/5/net | 705.950604 |
| runs/5/winrate | 0.44843 |
| runs/5/top5 | 292.724876 |
| runs/5/net_ex_top5 | 413.225729 |
| runs/5/realized_r_median | -0.308717 |
| runs/5/exits/PROTECTIVE_STOP | 590 |
| runs/5/exits/v21 | 67 |
| runs/5/exits/§8.5 | 12 |
| runs/5/max_dd_pct | 17.862453 |
| runs/6/variant | reentry_target_only |
| runs/6/segment | full |
| runs/6/n | 674 |
| runs/6/net | 519.665147 |
| runs/6/winrate | 0.476261 |
| runs/6/top5 | 212.311476 |
| runs/6/net_ex_top5 | 307.353671 |
| runs/6/realized_r_median | -0.144878 |
| runs/6/exits/PROTECTIVE_STOP | 575 |
| runs/6/exits/v21 | 88 |
| runs/6/exits/§8.5 | 11 |
| runs/6/max_dd_pct | 16.982961 |
| runs/7/variant | fixed_reentry_stop |
| runs/7/segment | full |
| runs/7/n | 403 |
| runs/7/net | 499.67709 |
| runs/7/winrate | 0.364764 |
| runs/7/top5 | 248.556823 |
| runs/7/net_ex_top5 | 251.120268 |
| runs/7/realized_r_median | -0.498533 |
| runs/7/exits/PROTECTIVE_STOP | 383 |
| runs/7/exits/v21 | 12 |
| runs/7/exits/§8.5 | 8 |
| runs/7/max_dd_pct | 18.808028 |
| runs/8/variant | fixed_reentry_target |
| runs/8/segment | full |
| runs/8/n | 395 |
| runs/8/net | 349.289097 |
| runs/8/winrate | 0.407595 |
| runs/8/top5 | 171.016654 |
| runs/8/net_ex_top5 | 178.272443 |
| runs/8/realized_r_median | -0.417362 |
| runs/8/exits/PROTECTIVE_STOP | 357 |
| runs/8/exits/v21 | 31 |
| runs/8/exits/§8.5 | 7 |
| runs/8/max_dd_pct | 16.159989 |
| runs/9/variant | baseline |
| runs/9/segment | test |
| runs/9/n | 226 |
| runs/9/net | 163.491803 |
| runs/9/winrate | 0.442478 |
| runs/9/top5 | 114.584614 |
| runs/9/net_ex_top5 | 48.90719 |
| runs/9/realized_r_median | -0.229538 |
| runs/9/exits/PROTECTIVE_STOP | 192 |
| runs/9/exits/v21 | 31 |
| runs/9/exits/§8.5 | 3 |
| runs/9/max_dd_pct | 16.570656 |
| runs/10/variant | target_only |
| runs/10/segment | test |
| runs/10/n | 214 |
| runs/10/net | 166.080059 |
| runs/10/winrate | 0.448598 |
| runs/10/top5 | 114.584614 |
| runs/10/net_ex_top5 | 51.495446 |
| runs/10/realized_r_median | -0.216308 |
| runs/10/exits/PROTECTIVE_STOP | 180 |
| runs/10/exits/v21 | 32 |
| runs/10/exits/§8.5 | 2 |
| runs/10/max_dd_pct | 12.964779 |
| runs/11/variant | rr_only |
| runs/11/segment | test |
| runs/11/n | 142 |
| runs/11/net | 145.229794 |
| runs/11/winrate | 0.359155 |
| runs/11/top5 | 115.937083 |
| runs/11/net_ex_top5 | 29.292711 |
| runs/11/realized_r_median | -0.348459 |
| runs/11/exits/PROTECTIVE_STOP | 134 |
| runs/11/exits/v21 | 5 |
| runs/11/exits/§8.5 | 3 |
| runs/11/max_dd_pct | 14.977827 |
| runs/12/variant | fixed_no_fill |
| runs/12/segment | test |
| runs/12/n | 129 |
| runs/12/net | 120.278019 |
| runs/12/winrate | 0.341085 |
| runs/12/top5 | 107.397361 |
| runs/12/net_ex_top5 | 12.880658 |
| runs/12/realized_r_median | -0.473217 |
| runs/12/exits/PROTECTIVE_STOP | 122 |
| runs/12/exits/v21 | 5 |
| runs/12/exits/§8.5 | 2 |
| runs/12/max_dd_pct | 15.410737 |
| runs/13/variant | fixed |
| runs/13/segment | test |
| runs/13/n | 129 |
| runs/13/net | 120.278019 |
| runs/13/winrate | 0.341085 |
| runs/13/top5 | 107.397361 |
| runs/13/net_ex_top5 | 12.880658 |
| runs/13/realized_r_median | -0.473217 |
| runs/13/exits/PROTECTIVE_STOP | 122 |
| runs/13/exits/v21 | 5 |
| runs/13/exits/§8.5 | 2 |
| runs/13/max_dd_pct | 15.410737 |
| runs/14/variant | reentry_stop_only |
| runs/14/segment | test |
| runs/14/n | 226 |
| runs/14/net | 162.716443 |
| runs/14/winrate | 0.433628 |
| runs/14/top5 | 122.664299 |
| runs/14/net_ex_top5 | 40.052143 |
| runs/14/realized_r_median | -0.287504 |
| runs/14/exits/PROTECTIVE_STOP | 193 |
| runs/14/exits/v21 | 31 |
| runs/14/exits/§8.5 | 2 |
| runs/14/max_dd_pct | 17.537971 |
| runs/15/variant | reentry_target_only |
| runs/15/segment | test |
| runs/15/n | 226 |
| runs/15/net | 139.401875 |
| runs/15/winrate | 0.451327 |
| runs/15/top5 | 96.087761 |
| runs/15/net_ex_top5 | 43.314114 |
| runs/15/realized_r_median | -0.216308 |
| runs/15/exits/PROTECTIVE_STOP | 189 |
| runs/15/exits/v21 | 35 |
| runs/15/exits/§8.5 | 2 |
| runs/15/max_dd_pct | 16.897791 |
| runs/16/variant | fixed_reentry_stop |
| runs/16/segment | test |
| runs/16/n | 133 |
| runs/16/net | 127.419556 |
| runs/16/winrate | 0.330827 |
| runs/16/top5 | 115.477047 |
| runs/16/net_ex_top5 | 11.94251 |
| runs/16/realized_r_median | -0.494816 |
| runs/16/exits/PROTECTIVE_STOP | 127 |
| runs/16/exits/v21 | 5 |
| runs/16/exits/§8.5 | 1 |
| runs/16/max_dd_pct | 17.050756 |
| runs/17/variant | fixed_reentry_target |
| runs/17/segment | test |
| runs/17/n | 129 |
| runs/17/net | 107.602599 |
| runs/17/winrate | 0.364341 |
| runs/17/top5 | 90.841921 |
| runs/17/net_ex_top5 | 16.760678 |
| runs/17/realized_r_median | -0.421357 |
| runs/17/exits/PROTECTIVE_STOP | 120 |
| runs/17/exits/v21 | 8 |
| runs/17/exits/§8.5 | 1 |
| runs/17/max_dd_pct | 15.754553 |


| Metric path | Historical value |
|---|---|
| baseline_cohorts/full/reject_rr_ge_1.5/n | 180 |
| baseline_cohorts/full/reject_rr_ge_1.5/net | 356.563072 |
| baseline_cohorts/full/reject_rr_ge_1.5/winrate | 0.316667 |
| baseline_cohorts/full/reject_rr_ge_1.5/top5 | 234.545797 |
| baseline_cohorts/full/reject_rr_ge_1.5/net_ex_top5 | 122.017276 |
| baseline_cohorts/full/reject_rr_ge_1.5/realized_r_median | -0.792794 |
| baseline_cohorts/full/reject_rr_ge_1.5/exits/PROTECTIVE_STOP | 168 |
| baseline_cohorts/full/reject_rr_ge_1.5/exits/v21 | 10 |
| baseline_cohorts/full/reject_rr_ge_1.5/exits/§8.5 | 2 |
| baseline_cohorts/full/reject_rr_below_1/n | 269 |
| baseline_cohorts/full/reject_rr_below_1/net | 87.188819 |
| baseline_cohorts/full/reject_rr_below_1/winrate | 0.550186 |
| baseline_cohorts/full/reject_rr_below_1/top5 | 59.607095 |
| baseline_cohorts/full/reject_rr_below_1/net_ex_top5 | 27.581724 |
| baseline_cohorts/full/reject_rr_below_1/realized_r_median | 0.302358 |
| baseline_cohorts/full/reject_rr_below_1/exits/PROTECTIVE_STOP | 213 |
| baseline_cohorts/full/reject_rr_below_1/exits/v21 | 54 |
| baseline_cohorts/full/reject_rr_below_1/exits/§8.5 | 2 |
| baseline_cohorts/full/accept_FLIPPED_BOX_EDGE/n | 97 |
| baseline_cohorts/full/accept_FLIPPED_BOX_EDGE/net | 208.0512 |
| baseline_cohorts/full/accept_FLIPPED_BOX_EDGE/winrate | 0.525773 |
| baseline_cohorts/full/accept_FLIPPED_BOX_EDGE/top5 | 148.842322 |
| baseline_cohorts/full/accept_FLIPPED_BOX_EDGE/net_ex_top5 | 59.208879 |
| baseline_cohorts/full/accept_FLIPPED_BOX_EDGE/realized_r_median | 0.047867 |
| baseline_cohorts/full/accept_FLIPPED_BOX_EDGE/exits/PROTECTIVE_STOP | 94 |
| baseline_cohorts/full/accept_FLIPPED_BOX_EDGE/exits/§8.5 | 3 |
| baseline_cohorts/full/accept_BOX_EDGE/n | 68 |
| baseline_cohorts/full/accept_BOX_EDGE/net | 25.607103 |
| baseline_cohorts/full/accept_BOX_EDGE/winrate | 0.397059 |
| baseline_cohorts/full/accept_BOX_EDGE/top5 | 67.744855 |
| baseline_cohorts/full/accept_BOX_EDGE/net_ex_top5 | -42.137752 |
| baseline_cohorts/full/accept_BOX_EDGE/realized_r_median | -0.32769 |
| baseline_cohorts/full/accept_BOX_EDGE/exits/PROTECTIVE_STOP | 65 |
| baseline_cohorts/full/accept_BOX_EDGE/exits/§8.5 | 3 |
| baseline_cohorts/full/reject_target_passed/n | 39 |
| baseline_cohorts/full/reject_target_passed/net | -5.684977 |
| baseline_cohorts/full/reject_target_passed/winrate | 0.358974 |
| baseline_cohorts/full/reject_target_passed/top5 | 42.881237 |
| baseline_cohorts/full/reject_target_passed/net_ex_top5 | -48.566213 |
| baseline_cohorts/full/reject_target_passed/realized_r_median | -0.312939 |
| baseline_cohorts/full/reject_target_passed/exits/PROTECTIVE_STOP | 35 |
| baseline_cohorts/full/reject_target_passed/exits/§8.5 | 3 |
| baseline_cohorts/full/reject_target_passed/exits/v21 | 1 |
| baseline_cohorts/full/unmatched/n | 11 |
| baseline_cohorts/full/unmatched/net | -3.060061 |
| baseline_cohorts/full/unmatched/winrate | 0.545455 |
| baseline_cohorts/full/unmatched/top5 | 7.544094 |
| baseline_cohorts/full/unmatched/net_ex_top5 | -10.604155 |
| baseline_cohorts/full/unmatched/realized_r_median | 0.088299 |
| baseline_cohorts/full/unmatched/exits/PROTECTIVE_STOP | 11 |
| baseline_cohorts/full/reject_rr_1_to_1.5/n | 5 |
| baseline_cohorts/full/reject_rr_1_to_1.5/net | 12.117869 |
| baseline_cohorts/full/reject_rr_1_to_1.5/winrate | 0.4 |
| baseline_cohorts/full/reject_rr_1_to_1.5/top5 | 12.117869 |
| baseline_cohorts/full/reject_rr_1_to_1.5/net_ex_top5 | 0 |
| baseline_cohorts/full/reject_rr_1_to_1.5/realized_r_median | -0.281789 |
| baseline_cohorts/full/reject_rr_1_to_1.5/exits/PROTECTIVE_STOP | 4 |
| baseline_cohorts/full/reject_rr_1_to_1.5/exits/v21 | 1 |
| baseline_cohorts/test/reject_rr_ge_1.5/n | 61 |
| baseline_cohorts/test/reject_rr_ge_1.5/net | 79.046154 |
| baseline_cohorts/test/reject_rr_ge_1.5/winrate | 0.295082 |
| baseline_cohorts/test/reject_rr_ge_1.5/top5 | 95.422382 |
| baseline_cohorts/test/reject_rr_ge_1.5/net_ex_top5 | -16.376227 |
| baseline_cohorts/test/reject_rr_ge_1.5/realized_r_median | -0.702488 |
| baseline_cohorts/test/reject_rr_ge_1.5/exits/PROTECTIVE_STOP | 57 |
| baseline_cohorts/test/reject_rr_ge_1.5/exits/v21 | 4 |
| baseline_cohorts/test/accept_BOX_EDGE/n | 20 |
| baseline_cohorts/test/accept_BOX_EDGE/net | 24.490448 |
| baseline_cohorts/test/accept_BOX_EDGE/winrate | 0.45 |
| baseline_cohorts/test/accept_BOX_EDGE/top5 | 33.351838 |
| baseline_cohorts/test/accept_BOX_EDGE/net_ex_top5 | -8.86139 |
| baseline_cohorts/test/accept_BOX_EDGE/realized_r_median | -0.090666 |
| baseline_cohorts/test/accept_BOX_EDGE/exits/PROTECTIVE_STOP | 20 |
| baseline_cohorts/test/reject_rr_below_1/n | 98 |
| baseline_cohorts/test/reject_rr_below_1/net | 16.792438 |
| baseline_cohorts/test/reject_rr_below_1/winrate | 0.530612 |
| baseline_cohorts/test/reject_rr_below_1/top5 | 25.681148 |
| baseline_cohorts/test/reject_rr_below_1/net_ex_top5 | -8.88871 |
| baseline_cohorts/test/reject_rr_below_1/realized_r_median | 0.30211 |
| baseline_cohorts/test/reject_rr_below_1/exits/v21 | 25 |
| baseline_cohorts/test/reject_rr_below_1/exits/PROTECTIVE_STOP | 72 |
| baseline_cohorts/test/reject_rr_below_1/exits/§8.5 | 1 |
| baseline_cohorts/test/accept_FLIPPED_BOX_EDGE/n | 26 |
| baseline_cohorts/test/accept_FLIPPED_BOX_EDGE/net | 36.266056 |
| baseline_cohorts/test/accept_FLIPPED_BOX_EDGE/winrate | 0.461538 |
| baseline_cohorts/test/accept_FLIPPED_BOX_EDGE/top5 | 49.141088 |
| baseline_cohorts/test/accept_FLIPPED_BOX_EDGE/net_ex_top5 | -12.875032 |
| baseline_cohorts/test/accept_FLIPPED_BOX_EDGE/realized_r_median | -0.073747 |
| baseline_cohorts/test/accept_FLIPPED_BOX_EDGE/exits/PROTECTIVE_STOP | 25 |
| baseline_cohorts/test/accept_FLIPPED_BOX_EDGE/exits/§8.5 | 1 |
| baseline_cohorts/test/reject_target_passed/n | 16 |
| baseline_cohorts/test/reject_target_passed/net | 3.953 |
| baseline_cohorts/test/reject_target_passed/winrate | 0.4375 |
| baseline_cohorts/test/reject_target_passed/top5 | 13.470913 |
| baseline_cohorts/test/reject_target_passed/net_ex_top5 | -9.517913 |
| baseline_cohorts/test/reject_target_passed/realized_r_median | -0.043229 |
| baseline_cohorts/test/reject_target_passed/exits/PROTECTIVE_STOP | 14 |
| baseline_cohorts/test/reject_target_passed/exits/§8.5 | 1 |
| baseline_cohorts/test/reject_target_passed/exits/v21 | 1 |
| baseline_cohorts/test/unmatched/n | 2 |
| baseline_cohorts/test/unmatched/net | 1.076086 |
| baseline_cohorts/test/unmatched/winrate | 0.5 |
| baseline_cohorts/test/unmatched/top5 | 1.076086 |
| baseline_cohorts/test/unmatched/net_ex_top5 | 0 |
| baseline_cohorts/test/unmatched/realized_r_median | 0.681336 |
| baseline_cohorts/test/unmatched/exits/PROTECTIVE_STOP | 2 |
| baseline_cohorts/test/reject_rr_1_to_1.5/n | 3 |
| baseline_cohorts/test/reject_rr_1_to_1.5/net | 1.86762 |
| baseline_cohorts/test/reject_rr_1_to_1.5/winrate | 0.333333 |
| baseline_cohorts/test/reject_rr_1_to_1.5/top5 | 1.86762 |
| baseline_cohorts/test/reject_rr_1_to_1.5/net_ex_top5 | 0 |
| baseline_cohorts/test/reject_rr_1_to_1.5/realized_r_median | -0.281789 |
| baseline_cohorts/test/reject_rr_1_to_1.5/exits/PROTECTIVE_STOP | 2 |
| baseline_cohorts/test/reject_rr_1_to_1.5/exits/v21 | 1 |


| Metric path | Historical value |
|---|---|
| historical_broker_parity/n | 669 |
| historical_broker_parity/net | 680.783025 |
| historical_broker_parity/winrate | 0.455904 |
| historical_broker_parity/top5 | 271.953384 |
| historical_broker_parity/net_ex_top5 | 408.829641 |
| historical_broker_parity/realized_r_median | -0.284537 |
| historical_broker_parity/exits/PROTECTIVE_STOP | 590 |
| historical_broker_parity/exits/v21 | 66 |
| historical_broker_parity/exits/§8.5 | 13 |


| Metric path | Historical value |
|---|---|
| matched_decisions/full/target_only/lost_baseline/n | 39 |
| matched_decisions/full/target_only/lost_baseline/net | -7.929267 |
| matched_decisions/full/target_only/lost_baseline/winrate | 0.333333 |
| matched_decisions/full/target_only/lost_baseline/top5 | 42.881237 |
| matched_decisions/full/target_only/lost_baseline/net_ex_top5 | -50.810503 |
| matched_decisions/full/target_only/lost_baseline/realized_r_median | -0.32623 |
| matched_decisions/full/target_only/lost_baseline/exits/PROTECTIVE_STOP | 35 |
| matched_decisions/full/target_only/lost_baseline/exits/§8.5 | 4 |
| matched_decisions/full/target_only/new_current/n | 9 |
| matched_decisions/full/target_only/new_current/net | 25.299743 |
| matched_decisions/full/target_only/new_current/winrate | 0.555556 |
| matched_decisions/full/target_only/new_current/top5 | 38.307094 |
| matched_decisions/full/target_only/new_current/net_ex_top5 | -13.007351 |
| matched_decisions/full/target_only/new_current/realized_r_median | 0.360823 |
| matched_decisions/full/target_only/new_current/exits/PROTECTIVE_STOP | 8 |
| matched_decisions/full/target_only/new_current/exits/v21 | 1 |
| matched_decisions/full/target_only/shared_n | 630 |
| matched_decisions/full/target_only/shared_baseline_net | 688.712292 |
| matched_decisions/full/target_only/shared_current_net | 718.787548 |
| matched_decisions/full/target_only/shared_baseline_realized_r_sum | 189.669552 |
| matched_decisions/full/target_only/shared_current_realized_r_sum | 189.669552 |
| matched_decisions/full/target_only/earlier_exit_n | 0 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/identity/0 | BTCUSDT |
| matched_decisions/full/target_only/examples_largest_r_drop/0/identity/1 | 12h |
| matched_decisions/full/target_only/examples_largest_r_drop/0/identity/2 | LEVEL_REJECTED |
| matched_decisions/full/target_only/examples_largest_r_drop/0/identity/3 | 1 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/identity/4 | 1786708800000 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/identity/5 | level_rejected_market |
| matched_decisions/full/target_only/examples_largest_r_drop/0/before/entry | 63040.70562 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/before/target_1 | 65047.165537 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/before/risk_usdt | 4.394087 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/before/r_multiple | 15.08186 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/before/net_pnl | 66.271008 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/before/hold_hours | 156 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/before/reason_exit | v21 / lock_giveback_exhausted / peak=79576.2 ref=76742.4 budget=1504.8449 giveback=2833.8 exit all |
| matched_decisions/full/target_only/examples_largest_r_drop/0/after/entry | 63040.70562 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/after/target_1 | 65047.165537 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/after/risk_usdt | 4.663113 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/after/r_multiple | 15.08186 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/after/net_pnl | 70.328416 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/after/hold_hours | 156 |
| matched_decisions/full/target_only/examples_largest_r_drop/0/after/reason_exit | v21 / lock_giveback_exhausted / peak=79576.2 ref=76742.4 budget=1504.8449 giveback=2833.8 exit all |
| matched_decisions/full/target_only/examples_largest_r_drop/1/identity/0 | SOLUSDT |
| matched_decisions/full/target_only/examples_largest_r_drop/1/identity/1 | 2h |
| matched_decisions/full/target_only/examples_largest_r_drop/1/identity/2 | LEVEL_REJECTED |
| matched_decisions/full/target_only/examples_largest_r_drop/1/identity/3 | 1 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/identity/4 | 1751284800000 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/identity/5 | level_rejected_limit |
| matched_decisions/full/target_only/examples_largest_r_drop/1/before/entry | 150.477656 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/before/target_1 | 152.652845 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/before/risk_usdt | 3.13498 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/before/r_multiple | 5.250809 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/before/net_pnl | 16.461181 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/before/hold_hours | 2 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/before/reason_exit | v21 / lock_giveback_exhausted / peak=159.941 ref=157.791 budget=1.6313923 giveback=2.15 exit all |
| matched_decisions/full/target_only/examples_largest_r_drop/1/after/entry | 150.477656 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/after/target_1 | 152.652845 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/after/risk_usdt | 3.271284 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/after/r_multiple | 5.250809 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/after/net_pnl | 17.176884 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/after/hold_hours | 2 |
| matched_decisions/full/target_only/examples_largest_r_drop/1/after/reason_exit | v21 / lock_giveback_exhausted / peak=159.941 ref=157.791 budget=1.6313923 giveback=2.15 exit all |
| matched_decisions/full/target_only/examples_largest_r_drop/2/identity/0 | ETHUSDT |
| matched_decisions/full/target_only/examples_largest_r_drop/2/identity/1 | 1d |
| matched_decisions/full/target_only/examples_largest_r_drop/2/identity/2 | LEVEL_REJECTED |
| matched_decisions/full/target_only/examples_largest_r_drop/2/identity/3 | 1 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/identity/4 | 1785830400000 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/identity/5 | level_rejected_limit |
| matched_decisions/full/target_only/examples_largest_r_drop/2/before/entry | 1867.656981 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/before/target_1 | 1948.167322 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/before/risk_usdt | 4.247495 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/before/r_multiple | 7.133406 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/before/net_pnl | 30.299109 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/before/hold_hours | 372 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/before/reason_exit | v21 / lock_giveback_exhausted / peak=2337.54 ref=2252.05 budget=60.382756 giveback=85.49 exit all |
| matched_decisions/full/target_only/examples_largest_r_drop/2/after/entry | 1867.656981 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/after/target_1 | 1948.167322 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/after/risk_usdt | 4.778432 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/after/r_multiple | 7.133406 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/after/net_pnl | 34.086497 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/after/hold_hours | 372 |
| matched_decisions/full/target_only/examples_largest_r_drop/2/after/reason_exit | v21 / lock_giveback_exhausted / peak=2337.54 ref=2252.05 budget=60.382756 giveback=85.49 exit all |
| matched_decisions/full/rr_only/lost_baseline/n | 276 |
| matched_decisions/full/rr_only/lost_baseline/net | 120.018329 |
| matched_decisions/full/rr_only/lost_baseline/winrate | 0.547101 |
| matched_decisions/full/rr_only/lost_baseline/top5 | 85.472527 |
| matched_decisions/full/rr_only/lost_baseline/net_ex_top5 | 34.545802 |
| matched_decisions/full/rr_only/lost_baseline/realized_r_median | 0.278908 |
| matched_decisions/full/rr_only/lost_baseline/exits/PROTECTIVE_STOP | 220 |
| matched_decisions/full/rr_only/lost_baseline/exits/v21 | 55 |
| matched_decisions/full/rr_only/lost_baseline/exits/§8.5 | 1 |
| matched_decisions/full/rr_only/new_current/n | 33 |
| matched_decisions/full/rr_only/new_current/net | -17.112109 |
| matched_decisions/full/rr_only/new_current/winrate | 0.242424 |
| matched_decisions/full/rr_only/new_current/top5 | 45.670549 |
| matched_decisions/full/rr_only/new_current/net_ex_top5 | -62.782658 |
| matched_decisions/full/rr_only/new_current/realized_r_median | -0.655276 |
| matched_decisions/full/rr_only/new_current/exits/PROTECTIVE_STOP | 31 |
| matched_decisions/full/rr_only/new_current/exits/v21 | 1 |
| matched_decisions/full/rr_only/new_current/exits/§8.5 | 1 |
| matched_decisions/full/rr_only/shared_n | 393 |
| matched_decisions/full/rr_only/shared_baseline_net | 560.764696 |
| matched_decisions/full/rr_only/shared_current_net | 444.277687 |
| matched_decisions/full/rr_only/shared_baseline_realized_r_sum | 149.464592 |
| matched_decisions/full/rr_only/shared_current_realized_r_sum | 149.550446 |
| matched_decisions/full/rr_only/earlier_exit_n | 0 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/identity/0 | BTCUSDT |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/identity/1 | 12h |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/identity/2 | LEVEL_REJECTED |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/identity/3 | 1 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/identity/4 | 1786708800000 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/identity/5 | level_rejected_market |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/before/entry | 63040.70562 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/before/target_1 | 65047.165537 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/before/risk_usdt | 4.394087 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/before/r_multiple | 15.08186 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/before/net_pnl | 66.271008 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/before/hold_hours | 156 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/before/reason_exit | v21 / lock_giveback_exhausted / peak=79576.2 ref=76742.4 budget=1504.8449 giveback=2833.8 exit all |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/after/entry | 63040.70562 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/after/target_1 | 65047.165537 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/after/risk_usdt | 3.497335 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/after/r_multiple | 15.08186 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/after/net_pnl | 52.746312 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/after/hold_hours | 156 |
| matched_decisions/full/rr_only/examples_largest_r_drop/0/after/reason_exit | v21 / lock_giveback_exhausted / peak=79576.2 ref=76742.4 budget=1504.8449 giveback=2833.8 exit all |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/identity/0 | SOLUSDT |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/identity/1 | 2h |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/identity/2 | LEVEL_REJECTED |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/identity/3 | 1 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/identity/4 | 1751284800000 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/identity/5 | level_rejected_limit |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/before/entry | 150.477656 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/before/target_1 | 152.652845 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/before/risk_usdt | 3.13498 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/before/r_multiple | 5.250809 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/before/net_pnl | 16.461181 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/before/hold_hours | 2 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/before/reason_exit | v21 / lock_giveback_exhausted / peak=159.941 ref=157.791 budget=1.6313923 giveback=2.15 exit all |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/after/entry | 150.477656 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/after/target_1 | 152.652845 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/after/risk_usdt | 2.589766 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/after/r_multiple | 5.250809 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/after/net_pnl | 13.598367 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/after/hold_hours | 2 |
| matched_decisions/full/rr_only/examples_largest_r_drop/1/after/reason_exit | v21 / lock_giveback_exhausted / peak=159.941 ref=157.791 budget=1.6313923 giveback=2.15 exit all |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/identity/0 | DOGEUSDT |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/identity/1 | 4h |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/identity/2 | LEVEL_REJECTED |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/identity/3 | 1 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/identity/4 | 1735574400000 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/identity/5 | level_rejected_limit |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/before/entry | 0.310526 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/before/target_1 | 0.336795 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/before/risk_usdt | 2.202739 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/before/r_multiple | 10.983796 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/before/net_pnl | 24.194441 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/before/hold_hours | 116 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/before/reason_exit | §8.5 / STRUCTURE_BROKEN reverse CHoCH event_ts=1735992000000 exit all |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/after/entry | 0.310526 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/after/target_1 | 0.336795 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/after/risk_usdt | 2.190365 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/after/r_multiple | 10.983796 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/after/net_pnl | 24.058517 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/after/hold_hours | 116 |
| matched_decisions/full/rr_only/examples_largest_r_drop/2/after/reason_exit | §8.5 / STRUCTURE_BROKEN reverse CHoCH event_ts=1735992000000 exit all |
| matched_decisions/full/fixed/lost_baseline/n | 317 |
| matched_decisions/full/fixed/lost_baseline/net | 122.40088 |
| matched_decisions/full/fixed/lost_baseline/winrate | 0.526814 |
| matched_decisions/full/fixed/lost_baseline/top5 | 100.561937 |
| matched_decisions/full/fixed/lost_baseline/net_ex_top5 | 21.838943 |
| matched_decisions/full/fixed/lost_baseline/realized_r_median | 0.119657 |
| matched_decisions/full/fixed/lost_baseline/exits/PROTECTIVE_STOP | 257 |
| matched_decisions/full/fixed/lost_baseline/exits/v21 | 55 |
| matched_decisions/full/fixed/lost_baseline/exits/§8.5 | 5 |
| matched_decisions/full/fixed/new_current/n | 47 |
| matched_decisions/full/fixed/new_current/net | -0.684942 |
| matched_decisions/full/fixed/new_current/winrate | 0.297872 |
| matched_decisions/full/fixed/new_current/top5 | 59.747318 |
| matched_decisions/full/fixed/new_current/net_ex_top5 | -60.432261 |
| matched_decisions/full/fixed/new_current/realized_r_median | -0.655276 |
| matched_decisions/full/fixed/new_current/exits/PROTECTIVE_STOP | 45 |
| matched_decisions/full/fixed/new_current/exits/v21 | 1 |
| matched_decisions/full/fixed/new_current/exits/§8.5 | 1 |
| matched_decisions/full/fixed/shared_n | 352 |
| matched_decisions/full/fixed/shared_baseline_net | 558.382145 |
| matched_decisions/full/fixed/shared_current_net | 489.254703 |
| matched_decisions/full/fixed/shared_baseline_realized_r_sum | 151.288767 |
| matched_decisions/full/fixed/shared_current_realized_r_sum | 151.288767 |
| matched_decisions/full/fixed/earlier_exit_n | 0 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/identity/0 | BTCUSDT |
| matched_decisions/full/fixed/examples_largest_r_drop/0/identity/1 | 12h |
| matched_decisions/full/fixed/examples_largest_r_drop/0/identity/2 | LEVEL_REJECTED |
| matched_decisions/full/fixed/examples_largest_r_drop/0/identity/3 | 1 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/identity/4 | 1786708800000 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/identity/5 | level_rejected_market |
| matched_decisions/full/fixed/examples_largest_r_drop/0/before/entry | 63040.70562 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/before/target_1 | 65047.165537 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/before/risk_usdt | 4.394087 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/before/r_multiple | 15.08186 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/before/net_pnl | 66.271008 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/before/hold_hours | 156 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/before/reason_exit | v21 / lock_giveback_exhausted / peak=79576.2 ref=76742.4 budget=1504.8449 giveback=2833.8 exit all |
| matched_decisions/full/fixed/examples_largest_r_drop/0/after/entry | 63040.70562 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/after/target_1 | 65047.165537 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/after/risk_usdt | 3.676685 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/after/r_multiple | 15.08186 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/after/net_pnl | 55.451251 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/after/hold_hours | 156 |
| matched_decisions/full/fixed/examples_largest_r_drop/0/after/reason_exit | v21 / lock_giveback_exhausted / peak=79576.2 ref=76742.4 budget=1504.8449 giveback=2833.8 exit all |
| matched_decisions/full/fixed/examples_largest_r_drop/1/identity/0 | DOGEUSDT |
| matched_decisions/full/fixed/examples_largest_r_drop/1/identity/1 | 2h |
| matched_decisions/full/fixed/examples_largest_r_drop/1/identity/2 | BREAKOUT_ACCEPTED |
| matched_decisions/full/fixed/examples_largest_r_drop/1/identity/3 | 1 |
| matched_decisions/full/fixed/examples_largest_r_drop/1/identity/4 | 1771012800000 |
| matched_decisions/full/fixed/examples_largest_r_drop/1/identity/5 | breakout_accepted_market |
| matched_decisions/full/fixed/examples_largest_r_drop/1/before/entry | 0.097039 |
| matched_decisions/full/fixed/examples_largest_r_drop/1/before/target_1 | - |
| matched_decisions/full/fixed/examples_largest_r_drop/1/before/risk_usdt | 4.892243 |
| matched_decisions/full/fixed/examples_largest_r_drop/1/before/r_multiple | 4.217101 |
| matched_decisions/full/fixed/examples_largest_r_drop/1/before/net_pnl | 20.631081 |
| matched_decisions/full/fixed/examples_largest_r_drop/1/before/hold_hours | 38 |
| matched_decisions/full/fixed/examples_largest_r_drop/1/before/reason_exit | PROTECTIVE_STOP / 15m trigger stop=0.11018476 ref=0.11018476 gap=no |
| matched_decisions/full/fixed/examples_largest_r_drop/1/after/entry | 0.097039 |
| matched_decisions/full/fixed/examples_largest_r_drop/1/after/target_1 | - |
| matched_decisions/full/fixed/examples_largest_r_drop/1/after/risk_usdt | 4.224559 |
| matched_decisions/full/fixed/examples_largest_r_drop/1/after/r_multiple | 4.217101 |
| matched_decisions/full/fixed/examples_largest_r_drop/1/after/net_pnl | 17.815393 |
| matched_decisions/full/fixed/examples_largest_r_drop/1/after/hold_hours | 38 |
| matched_decisions/full/fixed/examples_largest_r_drop/1/after/reason_exit | PROTECTIVE_STOP / 15m trigger stop=0.11018476 ref=0.11018476 gap=no |
| matched_decisions/full/fixed/examples_largest_r_drop/2/identity/0 | SOLUSDT |
| matched_decisions/full/fixed/examples_largest_r_drop/2/identity/1 | 2h |
| matched_decisions/full/fixed/examples_largest_r_drop/2/identity/2 | LEVEL_REJECTED |
| matched_decisions/full/fixed/examples_largest_r_drop/2/identity/3 | 1 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/identity/4 | 1751284800000 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/identity/5 | level_rejected_limit |
| matched_decisions/full/fixed/examples_largest_r_drop/2/before/entry | 150.477656 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/before/target_1 | 152.652845 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/before/risk_usdt | 3.13498 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/before/r_multiple | 5.250809 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/before/net_pnl | 16.461181 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/before/hold_hours | 2 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/before/reason_exit | v21 / lock_giveback_exhausted / peak=159.941 ref=157.791 budget=1.6313923 giveback=2.15 exit all |
| matched_decisions/full/fixed/examples_largest_r_drop/2/after/entry | 150.477656 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/after/target_1 | 152.652845 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/after/risk_usdt | 2.589766 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/after/r_multiple | 5.250809 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/after/net_pnl | 13.598367 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/after/hold_hours | 2 |
| matched_decisions/full/fixed/examples_largest_r_drop/2/after/reason_exit | v21 / lock_giveback_exhausted / peak=159.941 ref=157.791 budget=1.6313923 giveback=2.15 exit all |
| matched_decisions/full/reentry_target_only/lost_baseline/n | 4 |
| matched_decisions/full/reentry_target_only/lost_baseline/net | -4.447344 |
| matched_decisions/full/reentry_target_only/lost_baseline/winrate | 0.25 |
| matched_decisions/full/reentry_target_only/lost_baseline/top5 | -4.447344 |
| matched_decisions/full/reentry_target_only/lost_baseline/net_ex_top5 | 0 |
| matched_decisions/full/reentry_target_only/lost_baseline/realized_r_median | -1.02894 |
| matched_decisions/full/reentry_target_only/lost_baseline/exits/PROTECTIVE_STOP | 4 |
| matched_decisions/full/reentry_target_only/new_current/n | 9 |
| matched_decisions/full/reentry_target_only/new_current/net | 7.166737 |
| matched_decisions/full/reentry_target_only/new_current/winrate | 0.777778 |
| matched_decisions/full/reentry_target_only/new_current/top5 | 12.874406 |
| matched_decisions/full/reentry_target_only/new_current/net_ex_top5 | -5.707669 |
| matched_decisions/full/reentry_target_only/new_current/realized_r_median | 0.472745 |
| matched_decisions/full/reentry_target_only/new_current/exits/PROTECTIVE_STOP | 8 |
| matched_decisions/full/reentry_target_only/new_current/exits/v21 | 1 |
| matched_decisions/full/reentry_target_only/shared_n | 665 |
| matched_decisions/full/reentry_target_only/shared_baseline_net | 685.230369 |
| matched_decisions/full/reentry_target_only/shared_current_net | 512.49841 |
| matched_decisions/full/reentry_target_only/shared_baseline_realized_r_sum | 188.308573 |
| matched_decisions/full/reentry_target_only/shared_current_realized_r_sum | 155.981087 |
| matched_decisions/full/reentry_target_only/earlier_exit_n | 54 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/identity/0 | SOLUSDT |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/identity/1 | 2h |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/identity/2 | BREAKOUT_ACCEPTED |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/identity/3 | 1 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/identity/4 | 1787047200000 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/identity/5 | breakout_accepted_market |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/before/entry | 76.303258 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/before/target_1 | - |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/before/risk_usdt | 4.532623 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/before/r_multiple | 16.043066 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/before/net_pnl | 72.717173 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/before/hold_hours | 89 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/before/reason_exit | PROTECTIVE_STOP / 15m trigger stop=92.671167 ref=92.671167 gap=no |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/after/entry | 76.303258 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/after/target_1 | 77.254107 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/after/risk_usdt | 4.028998 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/after/r_multiple | 5.328407 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/after/net_pnl | 21.468141 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/after/hold_hours | 28 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/0/after/reason_exit | v21 / lock_giveback_exhausted / peak=83.032 ref=81.804 budget=0.71313682 giveback=1.228 exit all |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/identity/0 | ETHUSDT |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/identity/1 | 4h |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/identity/2 | BREAKOUT_ACCEPTED |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/identity/3 | -1 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/identity/4 | 1769673600000 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/identity/5 | breakout_accepted_market |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/before/entry | 2927.934296 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/before/target_1 | - |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/before/risk_usdt | 3.864952 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/before/r_multiple | 6.112076 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/before/net_pnl | 23.622882 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/before/hold_hours | 97 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/before/reason_exit | PROTECTIVE_STOP / 15m trigger stop=2332.2812 ref=2332.2812 gap=no |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/after/entry | 2927.934296 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/after/target_1 | 2874.3543 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/after/risk_usdt | 3.864952 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/after/r_multiple | 0.915414 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/after/net_pnl | 3.53803 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/after/hold_hours | 4 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/1/after/reason_exit | PROTECTIVE_STOP / 15m trigger stop=2835.425 ref=2835.425 gap=no |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/identity/0 | ETHUSDT |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/identity/1 | 2h |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/identity/2 | BREAKOUT_ACCEPTED |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/identity/3 | -1 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/identity/4 | 1759968000000 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/identity/5 | breakout_accepted_market |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/before/entry | 4476.834454 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/before/target_1 | - |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/before/risk_usdt | 3.49996 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/before/r_multiple | 5.193515 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/before/net_pnl | 18.177094 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/before/hold_hours | 84 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/before/reason_exit | PROTECTIVE_STOP / 15m trigger stop=3865.048 ref=3865.048 gap=no |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/after/entry | 4476.834454 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/after/target_1 | 4448.38433 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/after/risk_usdt | 3.49996 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/after/r_multiple | 0.391701 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/after/net_pnl | 1.370937 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/after/hold_hours | 6 |
| matched_decisions/full/reentry_target_only/examples_largest_r_drop/2/after/reason_exit | v21 / lock_giveback_exhausted / peak=4401 ref=4424.9 budget=21.337593 giveback=23.9 exit all |
| matched_decisions/test/target_only/lost_baseline/n | 16 |
| matched_decisions/test/target_only/lost_baseline/net | 9.173083 |
| matched_decisions/test/target_only/lost_baseline/winrate | 0.4375 |
| matched_decisions/test/target_only/lost_baseline/top5 | 18.690996 |
| matched_decisions/test/target_only/lost_baseline/net_ex_top5 | -9.517913 |
| matched_decisions/test/target_only/lost_baseline/realized_r_median | -0.043229 |
| matched_decisions/test/target_only/lost_baseline/exits/PROTECTIVE_STOP | 15 |
| matched_decisions/test/target_only/lost_baseline/exits/§8.5 | 1 |
| matched_decisions/test/target_only/new_current/n | 4 |
| matched_decisions/test/target_only/new_current/net | 11.34548 |
| matched_decisions/test/target_only/new_current/winrate | 0.75 |
| matched_decisions/test/target_only/new_current/top5 | 11.34548 |
| matched_decisions/test/target_only/new_current/net_ex_top5 | 0 |
| matched_decisions/test/target_only/new_current/realized_r_median | 2.129184 |
| matched_decisions/test/target_only/new_current/exits/PROTECTIVE_STOP | 3 |
| matched_decisions/test/target_only/new_current/exits/v21 | 1 |
| matched_decisions/test/target_only/shared_n | 210 |
| matched_decisions/test/target_only/shared_baseline_net | 154.31872 |
| matched_decisions/test/target_only/shared_current_net | 154.73458 |
| matched_decisions/test/target_only/shared_baseline_realized_r_sum | 66.185188 |
| matched_decisions/test/target_only/shared_current_realized_r_sum | 66.185188 |
| matched_decisions/test/target_only/earlier_exit_n | 0 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/identity/0 | DOGEUSDT |
| matched_decisions/test/target_only/examples_largest_r_drop/0/identity/1 | 4h |
| matched_decisions/test/target_only/examples_largest_r_drop/0/identity/2 | LEVEL_REJECTED |
| matched_decisions/test/target_only/examples_largest_r_drop/0/identity/3 | -1 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/identity/4 | 1782136800000 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/identity/5 | level_rejected_market |
| matched_decisions/test/target_only/examples_largest_r_drop/0/before/entry | 0.083583 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/before/target_1 | 0.08219 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/before/risk_usdt | 2.380179 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/before/r_multiple | 2.539622 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/before/net_pnl | 6.044753 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/before/hold_hours | 20 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/before/reason_exit | v21 / lock_giveback_exhausted / peak=0.07832 ref=0.0795 budget=0.00104496 giveback=0.00118 exit all |
| matched_decisions/test/target_only/examples_largest_r_drop/0/after/entry | 0.083583 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/after/target_1 | 0.08219 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/after/risk_usdt | 2.270924 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/after/r_multiple | 2.539622 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/after/net_pnl | 5.767289 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/after/hold_hours | 20 |
| matched_decisions/test/target_only/examples_largest_r_drop/0/after/reason_exit | v21 / lock_giveback_exhausted / peak=0.07832 ref=0.0795 budget=0.00104496 giveback=0.00118 exit all |
| matched_decisions/test/target_only/examples_largest_r_drop/1/identity/0 | DOGEUSDT |
| matched_decisions/test/target_only/examples_largest_r_drop/1/identity/1 | 2h |
| matched_decisions/test/target_only/examples_largest_r_drop/1/identity/2 | LEVEL_REJECTED |
| matched_decisions/test/target_only/examples_largest_r_drop/1/identity/3 | 1 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/identity/4 | 1783440000000 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/identity/5 | level_rejected_market |
| matched_decisions/test/target_only/examples_largest_r_drop/1/before/entry | 0.075435 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/before/target_1 | 0.078165 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/before/risk_usdt | 2.400559 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/before/r_multiple | -1.100069 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/before/net_pnl | -2.640779 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/before/hold_hours | 1 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/before/reason_exit | PROTECTIVE_STOP / 15m trigger stop=0.074388177 ref=0.074388177 gap=no |
| matched_decisions/test/target_only/examples_largest_r_drop/1/after/entry | 0.075435 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/after/target_1 | 0.078165 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/after/risk_usdt | 2.323087 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/after/r_multiple | -1.100069 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/after/net_pnl | -2.555556 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/after/hold_hours | 1 |
| matched_decisions/test/target_only/examples_largest_r_drop/1/after/reason_exit | PROTECTIVE_STOP / 15m trigger stop=0.074388177 ref=0.074388177 gap=no |
| matched_decisions/test/target_only/examples_largest_r_drop/2/identity/0 | DOGEUSDT |
| matched_decisions/test/target_only/examples_largest_r_drop/2/identity/1 | 2h |
| matched_decisions/test/target_only/examples_largest_r_drop/2/identity/2 | LEVEL_REJECTED |
| matched_decisions/test/target_only/examples_largest_r_drop/2/identity/3 | -1 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/identity/4 | 1782136800000 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/identity/5 | level_rejected_market |
| matched_decisions/test/target_only/examples_largest_r_drop/2/before/entry | 0.083583 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/before/target_1 | 0.082174 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/before/risk_usdt | 2.378039 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/before/r_multiple | 0.816169 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/before/net_pnl | 1.940883 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/before/hold_hours | 10 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/before/reason_exit | PROTECTIVE_STOP / 15m trigger stop=0.082217211 ref=0.082217211 gap=no |
| matched_decisions/test/target_only/examples_largest_r_drop/2/after/entry | 0.083583 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/after/target_1 | 0.082174 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/after/risk_usdt | 2.26925 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/after/r_multiple | 0.816169 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/after/net_pnl | 1.852093 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/after/hold_hours | 10 |
| matched_decisions/test/target_only/examples_largest_r_drop/2/after/reason_exit | PROTECTIVE_STOP / 15m trigger stop=0.082217211 ref=0.082217211 gap=no |
| matched_decisions/test/rr_only/lost_baseline/n | 99 |
| matched_decisions/test/rr_only/lost_baseline/net | 20.262173 |
| matched_decisions/test/rr_only/lost_baseline/winrate | 0.535354 |
| matched_decisions/test/rr_only/lost_baseline/top5 | 25.681148 |
| matched_decisions/test/rr_only/lost_baseline/net_ex_top5 | -5.418975 |
| matched_decisions/test/rr_only/lost_baseline/realized_r_median | 0.261819 |
| matched_decisions/test/rr_only/lost_baseline/exits/v21 | 26 |
| matched_decisions/test/rr_only/lost_baseline/exits/PROTECTIVE_STOP | 72 |
| matched_decisions/test/rr_only/lost_baseline/exits/§8.5 | 1 |
| matched_decisions/test/rr_only/new_current/n | 15 |
| matched_decisions/test/rr_only/new_current/net | 0.201614 |
| matched_decisions/test/rr_only/new_current/winrate | 0.266667 |
| matched_decisions/test/rr_only/new_current/top5 | 18.421808 |
| matched_decisions/test/rr_only/new_current/net_ex_top5 | -18.220194 |
| matched_decisions/test/rr_only/new_current/realized_r_median | -0.479511 |
| matched_decisions/test/rr_only/new_current/exits/PROTECTIVE_STOP | 14 |
| matched_decisions/test/rr_only/new_current/exits/§8.5 | 1 |
| matched_decisions/test/rr_only/shared_n | 127 |
| matched_decisions/test/rr_only/shared_baseline_net | 143.22963 |
| matched_decisions/test/rr_only/shared_current_net | 145.028181 |
| matched_decisions/test/rr_only/shared_baseline_realized_r_sum | 65.00499 |
| matched_decisions/test/rr_only/shared_current_realized_r_sum | 65.090845 |
| matched_decisions/test/rr_only/earlier_exit_n | 0 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/identity/0 | DOGEUSDT |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/identity/1 | 2h |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/identity/2 | LEVEL_REJECTED |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/identity/3 | 1 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/identity/4 | 1791036000000 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/identity/5 | level_rejected_market |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/before/entry | 0.093129 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/before/target_1 | 0.096214 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/before/risk_usdt | 3.343657 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/before/r_multiple | 2.101174 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/before/net_pnl | 7.025605 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/before/hold_hours | 36 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/before/reason_exit | PROTECTIVE_STOP / 15m trigger stop=0.095616384 ref=0.095616384 gap=no |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/after/entry | 0.093129 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/after/target_1 | 0.096214 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/after/risk_usdt | 3.236226 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/after/r_multiple | 2.101174 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/after/net_pnl | 6.799873 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/after/hold_hours | 36 |
| matched_decisions/test/rr_only/examples_largest_r_drop/0/after/reason_exit | PROTECTIVE_STOP / 15m trigger stop=0.095616384 ref=0.095616384 gap=no |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/identity/0 | DOGEUSDT |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/identity/1 | 12h |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/identity/2 | BREAKOUT_ACCEPTED |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/identity/3 | -1 |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/identity/4 | 1779148800000 |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/identity/5 | breakout_accepted_market |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/before/entry | 0.103639 |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/before/target_1 | - |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/before/risk_usdt | 2.145543 |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/before/r_multiple | 1.986327 |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/before/net_pnl | 4.26175 |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/before/hold_hours | 578 |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/before/reason_exit | PROTECTIVE_STOP / 15m trigger stop=0.087883388 ref=0.087883388 gap=no |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/after/entry | 0.103639 |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/after/target_1 | - |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/after/risk_usdt | 2.161495 |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/after/r_multiple | 1.986327 |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/after/net_pnl | 4.293436 |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/after/hold_hours | 578 |
| matched_decisions/test/rr_only/examples_largest_r_drop/1/after/reason_exit | PROTECTIVE_STOP / 15m trigger stop=0.087883388 ref=0.087883388 gap=no |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/identity/0 | DOGEUSDT |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/identity/1 | 12h |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/identity/2 | LEVEL_REJECTED |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/identity/3 | 1 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/identity/4 | 1782180000000 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/identity/5 | level_rejected_market |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/before/entry | 0.082186 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/before/target_1 | 0.090628 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/before/risk_usdt | 2.399481 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/before/r_multiple | -1.054772 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/before/net_pnl | -2.530906 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/before/hold_hours | 2 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/before/reason_exit | PROTECTIVE_STOP / 15m trigger stop=0.080116131 ref=0.080116131 gap=no |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/after/entry | 0.082186 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/after/target_1 | 0.090628 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/after/risk_usdt | 2.360145 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/after/r_multiple | -1.054772 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/after/net_pnl | -2.489416 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/after/hold_hours | 2 |
| matched_decisions/test/rr_only/examples_largest_r_drop/2/after/reason_exit | PROTECTIVE_STOP / 15m trigger stop=0.080116131 ref=0.080116131 gap=no |
| matched_decisions/test/fixed/lost_baseline/n | 118 |
| matched_decisions/test/fixed/lost_baseline/net | 33.231145 |
| matched_decisions/test/fixed/lost_baseline/winrate | 0.525424 |
| matched_decisions/test/fixed/lost_baseline/top5 | 33.492974 |
| matched_decisions/test/fixed/lost_baseline/net_ex_top5 | -0.261829 |
| matched_decisions/test/fixed/lost_baseline/realized_r_median | 0.119657 |
| matched_decisions/test/fixed/lost_baseline/exits/v21 | 26 |
| matched_decisions/test/fixed/lost_baseline/exits/PROTECTIVE_STOP | 90 |
| matched_decisions/test/fixed/lost_baseline/exits/§8.5 | 2 |
| matched_decisions/test/fixed/new_current/n | 21 |
| matched_decisions/test/fixed/new_current/net | 3.199502 |
| matched_decisions/test/fixed/new_current/winrate | 0.285714 |
| matched_decisions/test/fixed/new_current/top5 | 28.601034 |
| matched_decisions/test/fixed/new_current/net_ex_top5 | -25.401532 |
| matched_decisions/test/fixed/new_current/realized_r_median | -0.557222 |
| matched_decisions/test/fixed/new_current/exits/PROTECTIVE_STOP | 20 |
| matched_decisions/test/fixed/new_current/exits/§8.5 | 1 |
| matched_decisions/test/fixed/shared_n | 108 |
| matched_decisions/test/fixed/shared_baseline_net | 130.260658 |
| matched_decisions/test/fixed/shared_current_net | 117.078516 |
| matched_decisions/test/fixed/shared_baseline_realized_r_sum | 58.551955 |
| matched_decisions/test/fixed/shared_current_realized_r_sum | 58.551955 |
| matched_decisions/test/fixed/earlier_exit_n | 0 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/identity/0 | BTCUSDT |
| matched_decisions/test/fixed/examples_largest_r_drop/0/identity/1 | 12h |
| matched_decisions/test/fixed/examples_largest_r_drop/0/identity/2 | LEVEL_REJECTED |
| matched_decisions/test/fixed/examples_largest_r_drop/0/identity/3 | 1 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/identity/4 | 1786708800000 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/identity/5 | level_rejected_market |
| matched_decisions/test/fixed/examples_largest_r_drop/0/before/entry | 63040.70562 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/before/target_1 | 65047.165537 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/before/risk_usdt | 2.062531 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/before/r_multiple | 15.08186 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/before/net_pnl | 31.1068 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/before/hold_hours | 156 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/before/reason_exit | v21 / lock_giveback_exhausted / peak=79576.2 ref=76742.4 budget=1504.8449 giveback=2833.8 exit all |
| matched_decisions/test/fixed/examples_largest_r_drop/0/after/entry | 63040.70562 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/after/target_1 | 65047.165537 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/after/risk_usdt | 1.972856 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/after/r_multiple | 15.08186 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/after/net_pnl | 29.75433 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/after/hold_hours | 156 |
| matched_decisions/test/fixed/examples_largest_r_drop/0/after/reason_exit | v21 / lock_giveback_exhausted / peak=79576.2 ref=76742.4 budget=1504.8449 giveback=2833.8 exit all |
| matched_decisions/test/fixed/examples_largest_r_drop/1/identity/0 | ETHUSDT |
| matched_decisions/test/fixed/examples_largest_r_drop/1/identity/1 | 1d |
| matched_decisions/test/fixed/examples_largest_r_drop/1/identity/2 | LEVEL_REJECTED |
| matched_decisions/test/fixed/examples_largest_r_drop/1/identity/3 | 1 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/identity/4 | 1785830400000 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/identity/5 | level_rejected_limit |
| matched_decisions/test/fixed/examples_largest_r_drop/1/before/entry | 1867.656981 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/before/target_1 | 1948.167322 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/before/risk_usdt | 2.123748 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/before/r_multiple | 7.133406 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/before/net_pnl | 15.149554 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/before/hold_hours | 372 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/before/reason_exit | v21 / lock_giveback_exhausted / peak=2337.54 ref=2252.05 budget=60.382756 giveback=85.49 exit all |
| matched_decisions/test/fixed/examples_largest_r_drop/1/after/entry | 1867.656981 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/after/target_1 | 1948.167322 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/after/risk_usdt | 1.592811 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/after/r_multiple | 7.133406 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/after/net_pnl | 11.362166 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/after/hold_hours | 372 |
| matched_decisions/test/fixed/examples_largest_r_drop/1/after/reason_exit | v21 / lock_giveback_exhausted / peak=2337.54 ref=2252.05 budget=60.382756 giveback=85.49 exit all |
| matched_decisions/test/fixed/examples_largest_r_drop/2/identity/0 | ETHUSDT |
| matched_decisions/test/fixed/examples_largest_r_drop/2/identity/1 | 12h |
| matched_decisions/test/fixed/examples_largest_r_drop/2/identity/2 | LEVEL_REJECTED |
| matched_decisions/test/fixed/examples_largest_r_drop/2/identity/3 | 1 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/identity/4 | 1785672000000 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/identity/5 | level_rejected_limit |
| matched_decisions/test/fixed/examples_largest_r_drop/2/before/entry | 1869.3894 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/before/target_1 | 1952.579134 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/before/risk_usdt | 2.104074 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/before/r_multiple | 7.162365 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/before/net_pnl | 15.07015 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/before/hold_hours | 407 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/before/reason_exit | v21 / lock_giveback_exhausted / peak=2337.54 ref=2252.05 budget=62.3923 giveback=85.49 exit all |
| matched_decisions/test/fixed/examples_largest_r_drop/2/after/entry | 1869.3894 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/after/target_1 | 1952.579134 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/after/risk_usdt | 1.578056 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/after/r_multiple | 7.162365 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/after/net_pnl | 11.302612 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/after/hold_hours | 407 |
| matched_decisions/test/fixed/examples_largest_r_drop/2/after/reason_exit | v21 / lock_giveback_exhausted / peak=2337.54 ref=2252.05 budget=62.3923 giveback=85.49 exit all |
| matched_decisions/test/reentry_target_only/lost_baseline/n | 1 |
| matched_decisions/test/reentry_target_only/lost_baseline/net | -1.8311 |
| matched_decisions/test/reentry_target_only/lost_baseline/winrate | 0 |
| matched_decisions/test/reentry_target_only/lost_baseline/top5 | -1.8311 |
| matched_decisions/test/reentry_target_only/lost_baseline/net_ex_top5 | 0 |
| matched_decisions/test/reentry_target_only/lost_baseline/realized_r_median | -0.604341 |
| matched_decisions/test/reentry_target_only/lost_baseline/exits/PROTECTIVE_STOP | 1 |
| matched_decisions/test/reentry_target_only/new_current/n | 1 |
| matched_decisions/test/reentry_target_only/new_current/net | -2.976123 |
| matched_decisions/test/reentry_target_only/new_current/winrate | 0 |
| matched_decisions/test/reentry_target_only/new_current/top5 | -2.976123 |
| matched_decisions/test/reentry_target_only/new_current/net_ex_top5 | 0 |
| matched_decisions/test/reentry_target_only/new_current/realized_r_median | -1.041818 |
| matched_decisions/test/reentry_target_only/new_current/exits/PROTECTIVE_STOP | 1 |
| matched_decisions/test/reentry_target_only/shared_n | 225 |
| matched_decisions/test/reentry_target_only/shared_baseline_net | 165.322903 |
| matched_decisions/test/reentry_target_only/shared_current_net | 142.377997 |
| matched_decisions/test/reentry_target_only/shared_baseline_realized_r_sum | 71.757949 |
| matched_decisions/test/reentry_target_only/shared_current_realized_r_sum | 61.249401 |
| matched_decisions/test/reentry_target_only/earlier_exit_n | 12 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/identity/0 | SOLUSDT |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/identity/1 | 2h |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/identity/2 | BREAKOUT_ACCEPTED |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/identity/3 | 1 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/identity/4 | 1787047200000 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/identity/5 | breakout_accepted_market |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/before/entry | 76.303258 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/before/target_1 | - |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/before/risk_usdt | 2.115224 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/before/r_multiple | 16.043066 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/before/net_pnl | 33.934681 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/before/hold_hours | 89 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/before/reason_exit | PROTECTIVE_STOP / 15m trigger stop=92.671167 ref=92.671167 gap=no |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/after/entry | 76.303258 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/after/target_1 | 77.254107 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/after/risk_usdt | 2.115224 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/after/r_multiple | 5.328407 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/after/net_pnl | 11.270774 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/after/hold_hours | 28 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/0/after/reason_exit | v21 / lock_giveback_exhausted / peak=83.032 ref=81.804 budget=0.71313682 giveback=1.228 exit all |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/identity/0 | SOLUSDT |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/identity/1 | 2h |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/identity/2 | BREAKOUT_ACCEPTED |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/identity/3 | 1 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/identity/4 | 1786104000000 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/identity/5 | breakout_accepted_market |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/before/entry | 74.106818 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/before/target_1 | - |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/before/risk_usdt | 2.101307 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/before/r_multiple | 1.77957 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/before/net_pnl | 3.739423 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/before/hold_hours | 58 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/before/reason_exit | §8.5 / STRUCTURE_BROKEN reverse CHoCH event_ts=1786312800000 exit all |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/after/entry | 74.106818 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/after/target_1 | 74.452475 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/after/risk_usdt | 2.211902 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/after/r_multiple | 0.452247 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/after/net_pnl | 1.000326 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/after/hold_hours | 14 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/1/after/reason_exit | PROTECTIVE_STOP / 15m trigger stop=74.724758 ref=74.724758 gap=no |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/identity/0 | SOLUSDT |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/identity/1 | 4h |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/identity/2 | BREAKOUT_ACCEPTED |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/identity/3 | 1 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/identity/4 | 1786118400000 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/identity/5 | breakout_accepted_market |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/before/entry | 74.000797 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/before/target_1 | - |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/before/risk_usdt | 2.033215 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/before/r_multiple | 1.13208 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/before/net_pnl | 2.301762 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/before/hold_hours | 67 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/before/reason_exit | PROTECTIVE_STOP / 15m trigger stop=76.083445 ref=76.083445 gap=no |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/after/entry | 74.000797 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/after/target_1 | 74.345197 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/after/risk_usdt | 2.202649 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/after/r_multiple | 0.362702 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/after/net_pnl | 0.798906 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/after/hold_hours | 8 |
| matched_decisions/test/reentry_target_only/examples_largest_r_drop/2/after/reason_exit | PROTECTIVE_STOP / 15m trigger stop=74.7257 ref=74.7257 gap=no |

## Discarded standalone shadow diagnostics

Not canonical trade PnL; never a deployed signal path. Diagnostic horizon returns are not portfolio net returns.

| Metric | Value |
|---|---|
| rules/local_window | 16 |
| rules/noise_multiple | 1 |
| rules/max_wait_bars | 8 |
| rules/accept_hold_bars | 2 |
| rules/max_extension_atr | 1.5 |


| Metric path | Value |
|---|---|
| BTCUSDT/2h/symbol | BTCUSDT |
| BTCUSDT/2h/tf | 2h |
| BTCUSDT/2h/start_ts | 1729022400000 |
| BTCUSDT/2h/end_ts | 1773374400000 |
| BTCUSDT/2h/baseline_event_count | 216 |
| BTCUSDT/2h/fine_original_event_count | 278 |
| BTCUSDT/2h/candidate_statuses/triggered | 128 |
| BTCUSDT/2h/candidate_statuses/expired | 80 |
| BTCUSDT/2h/candidate_statuses/context_changed | 50 |
| BTCUSDT/2h/candidate_statuses/invalidated_runaway | 8 |
| BTCUSDT/2h/trigger_events/LEVEL_REJECTED | 112 |
| BTCUSDT/2h/trigger_events/BREAKOUT_ACCEPTED | 16 |
| BTCUSDT/2h/matched | 47 |
| BTCUSDT/2h/unmatched_triggers | 81 |
| BTCUSDT/2h/median_lead_minutes | 45 |
| BTCUSDT/2h/horizon_diagnostics/baseline/ALL/1h/n | 216 |
| BTCUSDT/2h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps | -15.579816 |
| BTCUSDT/2h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps_1.5x | -23.579816 |
| BTCUSDT/2h/horizon_diagnostics/baseline/ALL/1h/positive_fraction | 0.277778 |
| BTCUSDT/2h/horizon_diagnostics/baseline/ALL/2h/n | 216 |
| BTCUSDT/2h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps | -15.53692 |
| BTCUSDT/2h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps_1.5x | -23.53692 |
| BTCUSDT/2h/horizon_diagnostics/baseline/ALL/2h/positive_fraction | 0.356481 |
| BTCUSDT/2h/horizon_diagnostics/baseline/ALL/4h/n | 216 |
| BTCUSDT/2h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps | -16.408245 |
| BTCUSDT/2h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps_1.5x | -24.408245 |
| BTCUSDT/2h/horizon_diagnostics/baseline/ALL/4h/positive_fraction | 0.37963 |
| BTCUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/n | 54 |
| BTCUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -1.442937 |
| BTCUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -9.442937 |
| BTCUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.425926 |
| BTCUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/n | 54 |
| BTCUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -3.245688 |
| BTCUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -11.245688 |
| BTCUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.37037 |
| BTCUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/n | 54 |
| BTCUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | 1.510573 |
| BTCUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -6.489427 |
| BTCUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.407407 |
| BTCUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/n | 162 |
| BTCUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -20.292109 |
| BTCUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -28.292109 |
| BTCUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/positive_fraction | 0.228395 |
| BTCUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/n | 162 |
| BTCUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -19.633997 |
| BTCUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -27.633997 |
| BTCUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/positive_fraction | 0.351852 |
| BTCUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/n | 162 |
| BTCUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -22.381185 |
| BTCUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -30.381185 |
| BTCUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/positive_fraction | 0.37037 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/n | 277 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps | -15.877872 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps_1.5x | -23.877872 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/positive_fraction | 0.303249 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/n | 276 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps | -12.204693 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps_1.5x | -20.204693 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/positive_fraction | 0.384058 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/n | 276 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps | -22.141129 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps_1.5x | -30.141129 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/positive_fraction | 0.376812 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/n | 42 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -1.131204 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -9.131204 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.404762 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/n | 42 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | 5.366115 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -2.633885 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.452381 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/n | 42 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | 7.578267 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -0.421733 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.47619 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/n | 235 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -18.513447 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -26.513447 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/positive_fraction | 0.285106 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/n | 234 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -15.358427 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -23.358427 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/positive_fraction | 0.371795 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/n | 234 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -27.475379 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -35.475379 |
| BTCUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/positive_fraction | 0.358974 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/n | 128 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps | -14.05143 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps_1.5x | -22.05143 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/positive_fraction | 0.3125 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/n | 128 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps | -8.516748 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps_1.5x | -16.516748 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/positive_fraction | 0.414062 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/n | 128 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps | -12.663617 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps_1.5x | -20.663617 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/positive_fraction | 0.40625 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/n | 16 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | 12.991164 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | 4.991164 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.375 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/n | 16 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | 15.273564 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | 7.273564 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.5 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/n | 16 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | 31.443034 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | 23.443034 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.5625 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/n | 112 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -17.914657 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -25.914657 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/positive_fraction | 0.303571 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/n | 112 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -11.915364 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -19.915364 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/positive_fraction | 0.401786 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/n | 112 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -18.964567 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -26.964567 |
| BTCUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/positive_fraction | 0.383929 |


| Metric path | Value |
|---|---|
| BTCUSDT/4h/symbol | BTCUSDT |
| BTCUSDT/4h/tf | 4h |
| BTCUSDT/4h/start_ts | 1729022400000 |
| BTCUSDT/4h/end_ts | 1773374400000 |
| BTCUSDT/4h/baseline_event_count | 76 |
| BTCUSDT/4h/fine_original_event_count | 126 |
| BTCUSDT/4h/candidate_statuses/triggered | 68 |
| BTCUSDT/4h/candidate_statuses/expired | 35 |
| BTCUSDT/4h/candidate_statuses/context_changed | 9 |
| BTCUSDT/4h/candidate_statuses/invalidated_runaway | 1 |
| BTCUSDT/4h/trigger_events/LEVEL_REJECTED | 52 |
| BTCUSDT/4h/trigger_events/BREAKOUT_ACCEPTED | 16 |
| BTCUSDT/4h/matched | 33 |
| BTCUSDT/4h/unmatched_triggers | 35 |
| BTCUSDT/4h/median_lead_minutes | 105 |
| BTCUSDT/4h/horizon_diagnostics/baseline/ALL/1h/n | 76 |
| BTCUSDT/4h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps | -24.758196 |
| BTCUSDT/4h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps_1.5x | -32.758196 |
| BTCUSDT/4h/horizon_diagnostics/baseline/ALL/1h/positive_fraction | 0.302632 |
| BTCUSDT/4h/horizon_diagnostics/baseline/ALL/2h/n | 76 |
| BTCUSDT/4h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps | -29.113218 |
| BTCUSDT/4h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps_1.5x | -37.113218 |
| BTCUSDT/4h/horizon_diagnostics/baseline/ALL/2h/positive_fraction | 0.342105 |
| BTCUSDT/4h/horizon_diagnostics/baseline/ALL/4h/n | 76 |
| BTCUSDT/4h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps | -39.710131 |
| BTCUSDT/4h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps_1.5x | -47.710131 |
| BTCUSDT/4h/horizon_diagnostics/baseline/ALL/4h/positive_fraction | 0.315789 |
| BTCUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/n | 26 |
| BTCUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -35.917976 |
| BTCUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -43.917976 |
| BTCUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.307692 |
| BTCUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/n | 26 |
| BTCUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -48.925747 |
| BTCUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -56.925747 |
| BTCUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.230769 |
| BTCUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/n | 26 |
| BTCUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -68.031345 |
| BTCUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -76.031345 |
| BTCUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.230769 |
| BTCUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/n | 50 |
| BTCUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -18.95511 |
| BTCUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -26.95511 |
| BTCUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/positive_fraction | 0.3 |
| BTCUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/n | 50 |
| BTCUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -18.810702 |
| BTCUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -26.810702 |
| BTCUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/positive_fraction | 0.4 |
| BTCUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/n | 50 |
| BTCUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -24.9831 |
| BTCUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -32.9831 |
| BTCUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/positive_fraction | 0.36 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/n | 126 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps | -17.227569 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps_1.5x | -25.227569 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/positive_fraction | 0.31746 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/n | 126 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps | -14.291569 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps_1.5x | -22.291569 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/positive_fraction | 0.365079 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/n | 126 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps | -24.807272 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps_1.5x | -32.807272 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/positive_fraction | 0.388889 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/n | 33 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -17.086394 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -25.086394 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.333333 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/n | 33 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -16.571914 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -24.571914 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.242424 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/n | 33 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -15.864542 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -23.864542 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.393939 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/n | 93 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -17.277664 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -25.277664 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/positive_fraction | 0.311828 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/n | 93 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -13.482415 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -21.482415 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/positive_fraction | 0.408602 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/n | 93 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -27.980499 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -35.980499 |
| BTCUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/positive_fraction | 0.387097 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/n | 68 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps | -13.17089 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps_1.5x | -21.17089 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/positive_fraction | 0.308824 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/n | 68 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps | -8.96036 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps_1.5x | -16.96036 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/positive_fraction | 0.352941 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/n | 68 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps | -9.553778 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps_1.5x | -17.553778 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/positive_fraction | 0.470588 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/n | 16 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | 1.644614 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -6.355386 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.375 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/n | 16 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | 14.675547 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | 6.675547 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.375 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/n | 16 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | 14.407521 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | 6.407521 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.5 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/n | 52 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -17.729506 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -25.729506 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/positive_fraction | 0.288462 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/n | 52 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -16.232947 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -24.232947 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/positive_fraction | 0.346154 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/n | 52 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -16.926485 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -24.926485 |
| BTCUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/positive_fraction | 0.461538 |


| Metric path | Value |
|---|---|
| ETHUSDT/2h/symbol | ETHUSDT |
| ETHUSDT/2h/tf | 2h |
| ETHUSDT/2h/start_ts | 1729022400000 |
| ETHUSDT/2h/end_ts | 1773374400000 |
| ETHUSDT/2h/baseline_event_count | 284 |
| ETHUSDT/2h/fine_original_event_count | 463 |
| ETHUSDT/2h/candidate_statuses/context_changed | 55 |
| ETHUSDT/2h/candidate_statuses/triggered | 157 |
| ETHUSDT/2h/candidate_statuses/expired | 94 |
| ETHUSDT/2h/candidate_statuses/invalidated_runaway | 9 |
| ETHUSDT/2h/candidate_statuses/invalidated_target_passed | 2 |
| ETHUSDT/2h/trigger_events/LEVEL_REJECTED | 138 |
| ETHUSDT/2h/trigger_events/BREAKOUT_ACCEPTED | 19 |
| ETHUSDT/2h/matched | 60 |
| ETHUSDT/2h/unmatched_triggers | 97 |
| ETHUSDT/2h/median_lead_minutes | 60 |
| ETHUSDT/2h/horizon_diagnostics/baseline/ALL/1h/n | 284 |
| ETHUSDT/2h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps | -6.05924 |
| ETHUSDT/2h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps_1.5x | -14.05924 |
| ETHUSDT/2h/horizon_diagnostics/baseline/ALL/1h/positive_fraction | 0.376761 |
| ETHUSDT/2h/horizon_diagnostics/baseline/ALL/2h/n | 284 |
| ETHUSDT/2h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps | -8.428978 |
| ETHUSDT/2h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps_1.5x | -16.428978 |
| ETHUSDT/2h/horizon_diagnostics/baseline/ALL/2h/positive_fraction | 0.419014 |
| ETHUSDT/2h/horizon_diagnostics/baseline/ALL/4h/n | 284 |
| ETHUSDT/2h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps | -7.825079 |
| ETHUSDT/2h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps_1.5x | -15.825079 |
| ETHUSDT/2h/horizon_diagnostics/baseline/ALL/4h/positive_fraction | 0.464789 |
| ETHUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/n | 63 |
| ETHUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | 3.302446 |
| ETHUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -4.697554 |
| ETHUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.412698 |
| ETHUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/n | 63 |
| ETHUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -19.250208 |
| ETHUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -27.250208 |
| ETHUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.396825 |
| ETHUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/n | 63 |
| ETHUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -14.629312 |
| ETHUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -22.629312 |
| ETHUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.428571 |
| ETHUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/n | 221 |
| ETHUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -8.727956 |
| ETHUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -16.727956 |
| ETHUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/positive_fraction | 0.366516 |
| ETHUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/n | 221 |
| ETHUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -5.344193 |
| ETHUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -13.344193 |
| ETHUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/positive_fraction | 0.425339 |
| ETHUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/n | 221 |
| ETHUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -5.88541 |
| ETHUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -13.88541 |
| ETHUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/positive_fraction | 0.475113 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/n | 463 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps | -9.900752 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps_1.5x | -17.900752 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/positive_fraction | 0.37365 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/n | 463 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps | -6.777789 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps_1.5x | -14.777789 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/positive_fraction | 0.419006 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/n | 463 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps | -1.05034 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps_1.5x | -9.05034 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/positive_fraction | 0.483801 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/n | 50 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -25.016579 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -33.016579 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.26 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/n | 50 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -11.851476 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -19.851476 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.38 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/n | 50 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | 13.918876 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | 5.918876 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.46 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/n | 413 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -8.070749 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -16.070749 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/positive_fraction | 0.387409 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/n | 413 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -6.163542 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -14.163542 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/positive_fraction | 0.423729 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/n | 413 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -2.862593 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -10.862593 |
| ETHUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/positive_fraction | 0.486683 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/n | 157 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps | -3.51955 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps_1.5x | -11.51955 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/positive_fraction | 0.458599 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/n | 157 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps | -5.970732 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps_1.5x | -13.970732 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/positive_fraction | 0.458599 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/n | 157 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps | -9.736195 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps_1.5x | -17.736195 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/positive_fraction | 0.464968 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/n | 19 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | 0.530881 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -7.469119 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.578947 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/n | 19 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -23.425808 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -31.425808 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.526316 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/n | 19 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -13.982949 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -21.982949 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.473684 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/n | 138 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -4.077218 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -12.077218 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/positive_fraction | 0.442029 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/n | 138 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -3.567497 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -11.567497 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/positive_fraction | 0.449275 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/n | 138 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -9.151498 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -17.151498 |
| ETHUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/positive_fraction | 0.463768 |


| Metric path | Value |
|---|---|
| ETHUSDT/4h/symbol | ETHUSDT |
| ETHUSDT/4h/tf | 4h |
| ETHUSDT/4h/start_ts | 1729022400000 |
| ETHUSDT/4h/end_ts | 1773374400000 |
| ETHUSDT/4h/baseline_event_count | 120 |
| ETHUSDT/4h/fine_original_event_count | 205 |
| ETHUSDT/4h/candidate_statuses/expired | 49 |
| ETHUSDT/4h/candidate_statuses/triggered | 75 |
| ETHUSDT/4h/candidate_statuses/context_changed | 5 |
| ETHUSDT/4h/candidate_statuses/invalidated_runaway | 2 |
| ETHUSDT/4h/trigger_events/LEVEL_REJECTED | 65 |
| ETHUSDT/4h/trigger_events/BREAKOUT_ACCEPTED | 10 |
| ETHUSDT/4h/matched | 43 |
| ETHUSDT/4h/unmatched_triggers | 32 |
| ETHUSDT/4h/median_lead_minutes | 90 |
| ETHUSDT/4h/horizon_diagnostics/baseline/ALL/1h/n | 120 |
| ETHUSDT/4h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps | -11.412307 |
| ETHUSDT/4h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps_1.5x | -19.412307 |
| ETHUSDT/4h/horizon_diagnostics/baseline/ALL/1h/positive_fraction | 0.325 |
| ETHUSDT/4h/horizon_diagnostics/baseline/ALL/2h/n | 120 |
| ETHUSDT/4h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps | -16.254364 |
| ETHUSDT/4h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps_1.5x | -24.254364 |
| ETHUSDT/4h/horizon_diagnostics/baseline/ALL/2h/positive_fraction | 0.391667 |
| ETHUSDT/4h/horizon_diagnostics/baseline/ALL/4h/n | 120 |
| ETHUSDT/4h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps | -13.952158 |
| ETHUSDT/4h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps_1.5x | -21.952158 |
| ETHUSDT/4h/horizon_diagnostics/baseline/ALL/4h/positive_fraction | 0.458333 |
| ETHUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/n | 32 |
| ETHUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -6.94543 |
| ETHUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -14.94543 |
| ETHUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.375 |
| ETHUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/n | 32 |
| ETHUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -9.647234 |
| ETHUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -17.647234 |
| ETHUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.3125 |
| ETHUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/n | 32 |
| ETHUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -12.257139 |
| ETHUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -20.257139 |
| ETHUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.40625 |
| ETHUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/n | 88 |
| ETHUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -13.036625 |
| ETHUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -21.036625 |
| ETHUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/positive_fraction | 0.306818 |
| ETHUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/n | 88 |
| ETHUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -18.656957 |
| ETHUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -26.656957 |
| ETHUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/positive_fraction | 0.420455 |
| ETHUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/n | 88 |
| ETHUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -14.568528 |
| ETHUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -22.568528 |
| ETHUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/positive_fraction | 0.477273 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/n | 205 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps | -8.628043 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps_1.5x | -16.628043 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/positive_fraction | 0.419512 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/n | 205 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps | -6.4435 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps_1.5x | -14.4435 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/positive_fraction | 0.434146 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/n | 205 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps | 3.155483 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps_1.5x | -4.844517 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/positive_fraction | 0.546341 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/n | 23 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | 37.91429 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | 29.91429 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.521739 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/n | 23 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | 47.786017 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | 39.786017 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.608696 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/n | 23 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | 103.58155 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | 95.58155 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.652174 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/n | 182 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -14.509766 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -22.509766 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/positive_fraction | 0.406593 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/n | 182 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -13.296681 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -21.296681 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/positive_fraction | 0.412088 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/n | 182 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -9.535724 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -17.535724 |
| ETHUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/positive_fraction | 0.532967 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/n | 75 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps | -21.208858 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps_1.5x | -29.208858 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/positive_fraction | 0.36 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/n | 75 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps | -16.535524 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps_1.5x | -24.535524 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/positive_fraction | 0.44 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/n | 75 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps | -10.561494 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps_1.5x | -18.561494 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/positive_fraction | 0.453333 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/n | 10 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | 14.103064 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | 6.103064 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.4 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/n | 10 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | 41.99465 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | 33.99465 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.5 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/n | 10 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | 73.170753 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | 65.170753 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.5 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/n | 65 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -26.641462 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -34.641462 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/positive_fraction | 0.353846 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/n | 65 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -25.540166 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -33.540166 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/positive_fraction | 0.430769 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/n | 65 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -23.443378 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -31.443378 |
| ETHUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/positive_fraction | 0.446154 |


| Metric path | Value |
|---|---|
| SOLUSDT/2h/symbol | SOLUSDT |
| SOLUSDT/2h/tf | 2h |
| SOLUSDT/2h/start_ts | 1729022400000 |
| SOLUSDT/2h/end_ts | 1773374400000 |
| SOLUSDT/2h/baseline_event_count | 249 |
| SOLUSDT/2h/fine_original_event_count | 401 |
| SOLUSDT/2h/candidate_statuses/triggered | 147 |
| SOLUSDT/2h/candidate_statuses/context_changed | 43 |
| SOLUSDT/2h/candidate_statuses/expired | 81 |
| SOLUSDT/2h/candidate_statuses/invalidated_runaway | 8 |
| SOLUSDT/2h/trigger_events/LEVEL_REJECTED | 125 |
| SOLUSDT/2h/trigger_events/BREAKOUT_ACCEPTED | 22 |
| SOLUSDT/2h/matched | 53 |
| SOLUSDT/2h/unmatched_triggers | 94 |
| SOLUSDT/2h/median_lead_minutes | 60 |
| SOLUSDT/2h/horizon_diagnostics/baseline/ALL/1h/n | 249 |
| SOLUSDT/2h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps | -14.09017 |
| SOLUSDT/2h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps_1.5x | -22.09017 |
| SOLUSDT/2h/horizon_diagnostics/baseline/ALL/1h/positive_fraction | 0.413655 |
| SOLUSDT/2h/horizon_diagnostics/baseline/ALL/2h/n | 249 |
| SOLUSDT/2h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps | -14.390058 |
| SOLUSDT/2h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps_1.5x | -22.390058 |
| SOLUSDT/2h/horizon_diagnostics/baseline/ALL/2h/positive_fraction | 0.481928 |
| SOLUSDT/2h/horizon_diagnostics/baseline/ALL/4h/n | 249 |
| SOLUSDT/2h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps | -21.486682 |
| SOLUSDT/2h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps_1.5x | -29.486682 |
| SOLUSDT/2h/horizon_diagnostics/baseline/ALL/4h/positive_fraction | 0.433735 |
| SOLUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/n | 67 |
| SOLUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -27.383117 |
| SOLUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -35.383117 |
| SOLUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.298507 |
| SOLUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/n | 67 |
| SOLUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -27.300776 |
| SOLUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -35.300776 |
| SOLUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.432836 |
| SOLUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/n | 67 |
| SOLUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -44.96834 |
| SOLUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -52.96834 |
| SOLUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.358209 |
| SOLUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/n | 182 |
| SOLUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -9.196613 |
| SOLUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -17.196613 |
| SOLUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/positive_fraction | 0.456044 |
| SOLUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/n | 182 |
| SOLUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -9.637211 |
| SOLUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -17.637211 |
| SOLUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/positive_fraction | 0.5 |
| SOLUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/n | 182 |
| SOLUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -12.842336 |
| SOLUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -20.842336 |
| SOLUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/positive_fraction | 0.461538 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/n | 401 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps | -16.840359 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps_1.5x | -24.840359 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/positive_fraction | 0.406484 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/n | 401 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps | -15.680167 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps_1.5x | -23.680167 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/positive_fraction | 0.458853 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/n | 401 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps | -4.365709 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps_1.5x | -12.365709 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/positive_fraction | 0.498753 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/n | 52 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -43.432721 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -51.432721 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.288462 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/n | 52 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -30.411399 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -38.411399 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.5 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/n | 52 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -7.973145 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -15.973145 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.5 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/n | 349 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -12.878173 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -20.878173 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/positive_fraction | 0.424069 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/n | 349 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -13.485256 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -21.485256 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/positive_fraction | 0.452722 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/n | 349 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -3.828212 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -11.828212 |
| SOLUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/positive_fraction | 0.498567 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/n | 147 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps | -11.42875 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps_1.5x | -19.42875 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/positive_fraction | 0.47619 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/n | 147 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps | -12.02577 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps_1.5x | -20.02577 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/positive_fraction | 0.47619 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/n | 146 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps | 2.467509 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps_1.5x | -5.532491 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/positive_fraction | 0.493151 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/n | 22 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -46.301114 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -54.301114 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.363636 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/n | 22 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -53.106363 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -61.106363 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.409091 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/n | 21 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -12.856365 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -20.856365 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.52381 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/n | 125 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -5.291214 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -13.291214 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/positive_fraction | 0.496 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/n | 125 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -4.795586 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -12.795586 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/positive_fraction | 0.488 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/n | 125 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | 5.04192 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -2.95808 |
| SOLUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/positive_fraction | 0.488 |


| Metric path | Value |
|---|---|
| SOLUSDT/4h/symbol | SOLUSDT |
| SOLUSDT/4h/tf | 4h |
| SOLUSDT/4h/start_ts | 1729022400000 |
| SOLUSDT/4h/end_ts | 1773374400000 |
| SOLUSDT/4h/baseline_event_count | 83 |
| SOLUSDT/4h/fine_original_event_count | 190 |
| SOLUSDT/4h/candidate_statuses/expired | 47 |
| SOLUSDT/4h/candidate_statuses/triggered | 93 |
| SOLUSDT/4h/candidate_statuses/context_changed | 7 |
| SOLUSDT/4h/trigger_events/LEVEL_REJECTED | 81 |
| SOLUSDT/4h/trigger_events/BREAKOUT_ACCEPTED | 12 |
| SOLUSDT/4h/matched | 38 |
| SOLUSDT/4h/unmatched_triggers | 55 |
| SOLUSDT/4h/median_lead_minutes | 105 |
| SOLUSDT/4h/horizon_diagnostics/baseline/ALL/1h/n | 83 |
| SOLUSDT/4h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps | -14.791374 |
| SOLUSDT/4h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps_1.5x | -22.791374 |
| SOLUSDT/4h/horizon_diagnostics/baseline/ALL/1h/positive_fraction | 0.361446 |
| SOLUSDT/4h/horizon_diagnostics/baseline/ALL/2h/n | 83 |
| SOLUSDT/4h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps | -14.475098 |
| SOLUSDT/4h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps_1.5x | -22.475098 |
| SOLUSDT/4h/horizon_diagnostics/baseline/ALL/2h/positive_fraction | 0.433735 |
| SOLUSDT/4h/horizon_diagnostics/baseline/ALL/4h/n | 83 |
| SOLUSDT/4h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps | -21.078673 |
| SOLUSDT/4h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps_1.5x | -29.078673 |
| SOLUSDT/4h/horizon_diagnostics/baseline/ALL/4h/positive_fraction | 0.361446 |
| SOLUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/n | 24 |
| SOLUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -10.737631 |
| SOLUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -18.737631 |
| SOLUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.291667 |
| SOLUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/n | 24 |
| SOLUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -5.198143 |
| SOLUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -13.198143 |
| SOLUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.416667 |
| SOLUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/n | 24 |
| SOLUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -42.173565 |
| SOLUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -50.173565 |
| SOLUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.25 |
| SOLUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/n | 59 |
| SOLUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -16.440355 |
| SOLUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -24.440355 |
| SOLUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/positive_fraction | 0.389831 |
| SOLUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/n | 59 |
| SOLUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -18.248775 |
| SOLUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -26.248775 |
| SOLUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/positive_fraction | 0.440678 |
| SOLUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/n | 59 |
| SOLUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -12.4977 |
| SOLUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -20.4977 |
| SOLUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/positive_fraction | 0.40678 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/n | 190 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps | -22.871235 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps_1.5x | -30.871235 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/positive_fraction | 0.4 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/n | 190 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps | -19.795286 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps_1.5x | -27.795286 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/positive_fraction | 0.421053 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/n | 190 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps | -16.982365 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps_1.5x | -24.982365 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/positive_fraction | 0.478947 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/n | 18 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -36.335002 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -44.335002 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.277778 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/n | 18 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -25.685252 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -33.685252 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.444444 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/n | 18 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -1.546873 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -9.546873 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.5 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/n | 172 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -21.462236 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -29.462236 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/positive_fraction | 0.412791 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/n | 172 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -19.178894 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -27.178894 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/positive_fraction | 0.418605 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/n | 172 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -18.597707 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -26.597707 |
| SOLUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/positive_fraction | 0.476744 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/n | 93 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps | -21.872153 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps_1.5x | -29.872153 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/positive_fraction | 0.354839 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/n | 93 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps | -10.681381 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps_1.5x | -18.681381 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/positive_fraction | 0.451613 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/n | 92 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps | -13.217644 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps_1.5x | -21.217644 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/positive_fraction | 0.434783 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/n | 12 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | 6.786945 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -1.213055 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.416667 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/n | 12 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | 5.418092 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -2.581908 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.583333 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/n | 11 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -20.181307 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -28.181307 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.454545 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/n | 81 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -26.117946 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -34.117946 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/positive_fraction | 0.345679 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/n | 81 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -13.066488 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -21.066488 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/positive_fraction | 0.432099 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/n | 81 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -12.271961 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -20.271961 |
| SOLUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/positive_fraction | 0.432099 |


| Metric path | Value |
|---|---|
| DOGEUSDT/2h/symbol | DOGEUSDT |
| DOGEUSDT/2h/tf | 2h |
| DOGEUSDT/2h/start_ts | 1729022400000 |
| DOGEUSDT/2h/end_ts | 1773374400000 |
| DOGEUSDT/2h/baseline_event_count | 227 |
| DOGEUSDT/2h/fine_original_event_count | 294 |
| DOGEUSDT/2h/candidate_statuses/triggered | 144 |
| DOGEUSDT/2h/candidate_statuses/expired | 84 |
| DOGEUSDT/2h/candidate_statuses/context_changed | 48 |
| DOGEUSDT/2h/candidate_statuses/invalidated_target_passed | 1 |
| DOGEUSDT/2h/candidate_statuses/invalidated_runaway | 4 |
| DOGEUSDT/2h/trigger_events/BREAKOUT_ACCEPTED | 19 |
| DOGEUSDT/2h/trigger_events/LEVEL_REJECTED | 125 |
| DOGEUSDT/2h/matched | 60 |
| DOGEUSDT/2h/unmatched_triggers | 84 |
| DOGEUSDT/2h/median_lead_minutes | 75 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/ALL/1h/n | 227 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps | -5.241737 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps_1.5x | -13.241737 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/ALL/1h/positive_fraction | 0.427313 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/ALL/2h/n | 227 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps | -3.582919 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps_1.5x | -11.582919 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/ALL/2h/positive_fraction | 0.409692 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/ALL/4h/n | 227 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps | 1.264151 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps_1.5x | -6.735849 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/ALL/4h/positive_fraction | 0.431718 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/n | 45 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -9.821537 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -17.821537 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.4 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/n | 45 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | 22.025969 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | 14.025969 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.288889 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/n | 45 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | 21.608581 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | 13.608581 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.4 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/n | 182 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -4.109368 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -12.109368 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/positive_fraction | 0.434066 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/n | 182 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -9.914786 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -17.914786 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/positive_fraction | 0.43956 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/n | 182 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -3.766066 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -11.766066 |
| DOGEUSDT/2h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/positive_fraction | 0.43956 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/n | 294 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps | -11.677867 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps_1.5x | -19.677867 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/ALL/1h/positive_fraction | 0.455782 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/n | 294 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps | -5.058691 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps_1.5x | -13.058691 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/ALL/2h/positive_fraction | 0.455782 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/n | 294 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps | 2.568845 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps_1.5x | -5.431155 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/ALL/4h/positive_fraction | 0.506803 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/n | 39 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -9.692309 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -17.692309 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.435897 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/n | 39 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -17.441531 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -25.441531 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.358974 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/n | 39 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -26.52839 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -34.52839 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.512821 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/n | 255 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -11.98154 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -19.98154 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/positive_fraction | 0.458824 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/n | 255 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -3.164845 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -11.164845 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/positive_fraction | 0.470588 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/n | 255 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | 7.01901 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -0.98099 |
| DOGEUSDT/2h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/positive_fraction | 0.505882 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/n | 144 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps | -11.392271 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps_1.5x | -19.392271 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/ALL/1h/positive_fraction | 0.465278 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/n | 144 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps | -3.951544 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps_1.5x | -11.951544 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/ALL/2h/positive_fraction | 0.416667 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/n | 144 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps | -2.748704 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps_1.5x | -10.748704 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/ALL/4h/positive_fraction | 0.409722 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/n | 19 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -22.395663 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -30.395663 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.473684 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/n | 19 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | 7.547921 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -0.452079 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.421053 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/n | 19 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | 3.146172 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -4.853828 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.263158 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/n | 125 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -9.719755 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -17.719755 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/positive_fraction | 0.464 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/n | 125 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -5.699463 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -13.699463 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/positive_fraction | 0.416 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/n | 125 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -3.644725 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -11.644725 |
| DOGEUSDT/2h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/positive_fraction | 0.432 |


| Metric path | Value |
|---|---|
| DOGEUSDT/4h/symbol | DOGEUSDT |
| DOGEUSDT/4h/tf | 4h |
| DOGEUSDT/4h/start_ts | 1729022400000 |
| DOGEUSDT/4h/end_ts | 1773374400000 |
| DOGEUSDT/4h/baseline_event_count | 91 |
| DOGEUSDT/4h/fine_original_event_count | 201 |
| DOGEUSDT/4h/candidate_statuses/expired | 82 |
| DOGEUSDT/4h/candidate_statuses/triggered | 83 |
| DOGEUSDT/4h/candidate_statuses/context_changed | 6 |
| DOGEUSDT/4h/candidate_statuses/invalidated_runaway | 2 |
| DOGEUSDT/4h/trigger_events/LEVEL_REJECTED | 77 |
| DOGEUSDT/4h/trigger_events/BREAKOUT_ACCEPTED | 6 |
| DOGEUSDT/4h/matched | 30 |
| DOGEUSDT/4h/unmatched_triggers | 53 |
| DOGEUSDT/4h/median_lead_minutes | 97.5 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/ALL/1h/n | 91 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps | -15.908318 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/ALL/1h/mean_cost_adjusted_bps_1.5x | -23.908318 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/ALL/1h/positive_fraction | 0.351648 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/ALL/2h/n | 91 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps | -0.270756 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/ALL/2h/mean_cost_adjusted_bps_1.5x | -8.270756 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/ALL/2h/positive_fraction | 0.373626 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/ALL/4h/n | 91 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps | -10.924876 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/ALL/4h/mean_cost_adjusted_bps_1.5x | -18.924876 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/ALL/4h/positive_fraction | 0.428571 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/n | 21 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | 37.852919 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | 29.852919 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.52381 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/n | 21 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | 141.960643 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | 133.960643 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.619048 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/n | 21 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | 113.58927 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | 105.58927 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.571429 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/n | 70 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -32.036689 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -40.036689 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/1h/positive_fraction | 0.3 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/n | 70 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -42.940176 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -50.940176 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/2h/positive_fraction | 0.3 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/n | 70 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -48.27912 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -56.27912 |
| DOGEUSDT/4h/horizon_diagnostics/baseline/LEVEL_REJECTED/4h/positive_fraction | 0.385714 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/n | 201 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps | -30.80005 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/mean_cost_adjusted_bps_1.5x | -38.80005 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/ALL/1h/positive_fraction | 0.333333 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/n | 201 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps | -43.378829 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/mean_cost_adjusted_bps_1.5x | -51.378829 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/ALL/2h/positive_fraction | 0.363184 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/n | 201 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps | -49.671979 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/mean_cost_adjusted_bps_1.5x | -57.671979 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/ALL/4h/positive_fraction | 0.383085 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/n | 22 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | -1.065341 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | -9.065341 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.318182 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/n | 22 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | -15.54998 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -23.54998 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.409091 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/n | 22 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | 8.151222 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | 0.151222 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.454545 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/n | 179 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -34.454595 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -42.454595 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/1h/positive_fraction | 0.335196 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/n | 179 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -46.799134 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -54.799134 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/2h/positive_fraction | 0.357542 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/n | 179 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -56.778741 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -64.778741 |
| DOGEUSDT/4h/horizon_diagnostics/fine_original/LEVEL_REJECTED/4h/positive_fraction | 0.374302 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/n | 83 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps | -27.623108 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/mean_cost_adjusted_bps_1.5x | -35.623108 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/ALL/1h/positive_fraction | 0.361446 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/n | 83 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps | -21.997131 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/mean_cost_adjusted_bps_1.5x | -29.997131 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/ALL/2h/positive_fraction | 0.39759 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/n | 83 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps | -15.572909 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/mean_cost_adjusted_bps_1.5x | -23.572909 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/ALL/4h/positive_fraction | 0.421687 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/n | 6 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps | 14.77867 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/mean_cost_adjusted_bps_1.5x | 6.77867 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/1h/positive_fraction | 0.333333 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/n | 6 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps | 1.961238 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/mean_cost_adjusted_bps_1.5x | -6.038762 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/2h/positive_fraction | 0.5 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/n | 6 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps | -39.195686 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/mean_cost_adjusted_bps_1.5x | -47.195686 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/BREAKOUT_ACCEPTED/4h/positive_fraction | 0.166667 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/n | 77 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps | -30.927143 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/mean_cost_adjusted_bps_1.5x | -38.927143 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/1h/positive_fraction | 0.363636 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/n | 77 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps | -23.864017 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/mean_cost_adjusted_bps_1.5x | -31.864017 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/2h/positive_fraction | 0.38961 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/n | 77 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps | -13.732173 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/mean_cost_adjusted_bps_1.5x | -21.732173 |
| DOGEUSDT/4h/horizon_diagnostics/event_shadow/LEVEL_REJECTED/4h/positive_fraction | 0.441558 |

## Cleanup state

Retained: production contracts, pure lock protection split, canonical fine-execution correctness guards and regression tests. Removed: failed attempt confirmation, research exit cadences, temporary audit hooks, study scripts, candidate configuration, dedicated study tests and raw artifacts. Unrelated compounding research and existing generic tools were deliberately left untouched.
