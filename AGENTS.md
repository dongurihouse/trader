# Trader agent rules

## Database access

- Never invoke `sqlite3` or another SQL client against `data/trader.sqlite3` or `data/trader.db`.
- Never run ad hoc SQL from Python, a notebook, a shell command, or an MCP tool.
- Use the repository's named commands and loopback service APIs for reads, backfills, sweeps, polls, and algorithm recalculation.
- Change algorithm behavior only through `config/config.json` or the algorithm code. The algo service detects the effective definition change and recalculates its stored results.
- Use `make algo-recalculate` for an explicit full algorithm recalculation. Do not delete outputs or trades yourself.
- Use `make backfill`, `make sweep`, and `make once` for bar collection. These commands submit work to the bars service.
- Use `make status`, `make logs`, `make query`, `make algo-status`, and `make algo-logs` for operational reads.
- SQL belongs only inside predefined, service-owned storage functions. Schema changes belong in `config/schema.sql`.
