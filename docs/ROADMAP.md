# Release roadmap

- D0 P00–P03: repository, architecture, engineering governance (supplied Module 1 baseline).
- D1 P04–P08: public Binance market data, read-only gateway, database and event infrastructure (supplied Module 1 baseline).
- **D2 P09–P14: current release. Historical replay, risk rules, virtual portfolios, simulated exchange, baseline SMA worker and backtester.**
- D3 P15–P19: Go exchange order manager, Spot Testnet, futures Demo, order-state reconciliation — NOT IMPLEMENTED.
- D4 P20–P24: production-grade terminal, AI signals, research agents and controlled self-improvement — NOT IMPLEMENTED. Module 2 only adds a simple paper panel.
- D5 P25–P28: security, fault injection, staging and shadow operations — NOT IMPLEMENTED.
- D6 P29–P32: explicitly approved canary live spot, optional live futures and ongoing operations — NOT IMPLEMENTED.

Neither source implementation nor unit tests is equivalent to trading readiness. Local runtime and exchange-specific acceptance must pass before progressing.


## Module 3 extension (2026-10-28)
P15–P19 sandbox execution, reconciliation, worker and admin controls implemented. Offline verification completed; runtime/Binance credential acceptance pending. Details in `MODULE3_ACCEPTANCE.md`. P20–P24 pending.


### Module 6 development branch status
P29–P32 source implemented in Module 6 engineering release candidate. Offline checks pass; operational acceptance **not** achieved. See `MODULE6_ACCEPTANCE.md`. Live environment remains opt-in and disabled by default.
