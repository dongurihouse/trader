# trader

Single-ticker (SNDK) intraday trading program. Signals are computed on SNDK; positions
are expressed on leveraged ETFs (long SNXX, short SNDQ). Backtest, paper, and live modes
run one identical decision path; sessions are operator-started, never scheduled.

Successor to and merger of three predecessor projects (`dt`, `daytrader`, `win`),
per Dev rulings 2026-08-01. Design: [docs/design/architecture.md](docs/design/architecture.md).
Contracts: [docs/design/contracts.md](docs/design/contracts.md).
Build plan: [docs/plans/build-plan.md](docs/plans/build-plan.md).

Until the `api` broker is wired and both interlocks are deliberately set, the program
places no orders anywhere: live mode renders order tickets and the Dev executes them
manually at the broker.
