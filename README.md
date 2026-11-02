# codex-usage

Recorded token usage and estimated API-equivalent cost for Codex CLI, grouped
by the current and previous local calendar month. One executable, no external
packages, and the same compact report as
[opencode-usage](https://github.com/gigaprakosa/opencode-usage).

## Requirements and installation

Python 3.14+ is required. Its standard-library Zstandard reader handles Codex's
compressed logs without installing a package or a separate decompressor.

```bash
git clone https://github.com/gigaprakosa/codex-usage.git
cd codex-usage
chmod +x codex-usage
mkdir -p ~/.local/bin
ln -sf "$(pwd)/codex-usage" ~/.local/bin/codex-usage
codex-usage
```

The command accepts `-h/--help`, with no reporting flags or subcommands.
Illustrative output:

```text
📊 Codex Usage (last 2 months):
• 2026-10: 1,234,567 tokens, Cost: $4.20
• 2026-11: 234,567 tokens, Cost: $0.80
```

## Data and counting rules

The reader scans `sessions` and `archived_sessions` under `$CODEX_HOME`, or
`~/.codex` when unset. Both `.jsonl` and `.jsonl.zst` are supported. A plain file
wins over its compressed sibling. Session files are opened for reading only;
the tool does not read credentials or send session data to a service.

CLI sources `cli` and `exec` are included, together with descendants whose
parent chain reaches a CLI root. Editor sessions are excluded. Missing ancestry
or unknown sources produce a warning rather than guessing the origin.

Current `token_usage_record.usage` records take precedence over matching legacy
`token_count` snapshots. Response IDs prevent duplicate current records;
repeated cumulative legacy snapshots are ignored. Earlier legacy-only activity
is retained in mixed-format sessions. Copied fork history before fork creation
is excluded. Event timestamps determine the local calendar month, even when a
session spans months. Missing logs and usage not recorded by Codex cannot be
recovered. Legacy attribution is best effort because snapshots lack response IDs.

Tokens equal input plus output. Cached reads and cache writes are subdivisions
of input; reasoning is a subdivision of output. They are never added again.
Malformed counters and unreadable records produce an incomplete-report warning,
without printing transcript contents. An empty store prints no usage; missing
session directories exit with an error.

The formats follow the upstream
[Codex protocol](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs)
and [usage persistence tests](https://github.com/openai/codex/blob/main/codex-rs/core/tests/suite/token_usage_rollout.rs).
Local verification covers logs from Codex 0.142 through 0.166; future format
changes may require reader updates.

## Estimated cost

Rates come from [models.dev](https://models.dev/api.json), fetched fresh with a
15-second timeout when the report contains usage. Matching uses the exact
provider and model, with no fuzzy substitutions. Input context tiers apply
above their stated thresholds.

Cost uses uncached input, cached reads, cache writes, and output at their
respective per-million-token rates. Reasoning adds no separate output charge.
A missing required rate or model makes the month's entire cost `unknown`.
Explicit zero prices remain zero. These are current API-equivalent estimates,
not historical invoices, subscription charges, or quota consumption.

A successful fetch updates `~/.cache/codex-usage/models-dev.json`. If the network
fails, a usable cache supplies stale prices with a warning on stderr. Without
usable prices, cost is unknown. Cache-write failures warn without discarding
live pricing. The pricing cache is the only data file this tool writes.

## Verification

```bash
python3 -m unittest discover -v
python3 -m py_compile codex-usage test_usage.py
./codex-usage --help
./codex-usage
```

Tests create synthetic sessions in temporary directories. They cover compressed
and plain logs, duplicate records, CLI ancestry, mixed usage formats, forks,
month filtering, cache arithmetic, context tiers, unavailable prices, malformed
logs, offline pricing, and CLI exit behavior. No typechecker is configured;
syntax compilation is a separate check.

## License

MIT, see [LICENSE](LICENSE).
