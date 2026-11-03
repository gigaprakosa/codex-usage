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
`~/.codex` when unset. Both `.jsonl` and `.jsonl.zst` are supported. A plain file is preferred, with compressed siblings and archived copies also
checked for valid matching responses before inference. Session files are opened for reading only;
the tool does not read credentials or send session data to a service.
Unrelated message events and unused payload fields are discarded during loading.
An ancestry pass collects metadata first, then included sessions are streamed;
only usage in the report window is retained for deduplication and pricing.
Historical model, fork, and cumulative state is still used.

CLI sources `cli` and `exec` are included, together with descendants whose
parent chain reaches a CLI root. Editor sessions and their descendants are excluded without a coverage warning.
Optional `state*.sqlite` databases under the same home supply missing ancestry
from `threads` and `thread_spawn_edges`, read from temporary private copies with
schema inspection. Committed WAL data is copied too, so SQLite cannot create
sidecars in the source directory. The temporary copies are removed after reading.
Recorded ancestry, including nested parent links, takes precedence. Missing ancestry or unknown sources with
relevant usage produce a warning rather than guessing the origin; inactive
orphans produce no warning. Unavailable state databases produce a note. Parent
chains are walked iteratively, so deep ancestry does not depend on Python's
recursion limit; cycles remain excluded. Inclusion decisions for shared ancestors
are reused within each report after metadata collection.

Current `token_usage_record.usage` records take precedence over matching legacy
`token_count` snapshots. Response IDs prevent duplicate current records;
repeated cumulative legacy snapshots are ignored. Rejected timestamps and copied
pre-fork events do not consume a legacy snapshot; valid historical events do. Earlier legacy-only activity
is retained in mixed-format sessions. Copied fork history before fork creation
is excluded. Event timestamps determine the local calendar month, even when a
session spans months. Missing logs and responses without identifiable usage events cannot be
recovered. Legacy attribution is best effort because snapshots lack response IDs. Null legacy
usage accompanied by a cumulative total produces an invalid-counter warning;
empty informational events without usage or totals remain silent.

Tokens equal input plus output. Cached reads and cache writes are subdivisions
of input; reasoning is a subdivision of output. They are never added again.
Input and output counters must be present, cumulative counters must be valid,
and response identities must be nonempty strings. Rejected current records do
not suppress valid legacy snapshots. Malformed ancestry is excluded.
Relevant malformed counters, metadata, and unreadable records produce an incomplete-report warning,
without printing transcript contents. Truncated compressed sessions use the same
warning path so readable sessions still contribute. Timestamps that overflow
during local timezone conversion are skipped with an invalid-timestamp warning. An empty store prints no
usage; missing session directories exit with an error.

Recovery first checks valid alternate records with the same session, provider,
model, and response identity, then reconciles exact legacy twins with the same
session, provider, model, and cumulative snapshot before attempting inference.
Conflicting legacy twins remain unresolved. An isolated modern response can then be recovered
from neighboring cumulative snapshots, subtracting the known following response.
Neighbors must have the same model, known response counts, ordered timestamps,
and nonnegative valid category deltas. Resets, conflicting copies, adjacent gaps,
and malformed surrounding data block inference. Contradictory cumulative evidence
is distinguished from unavailable evidence and blocks median fallback, with an
explicit coverage diagnostic. Inferred counters must agree with any valid
per-response counters already present. Reconstructed cumulative snapshots
also suppress matching legacy copies.

Otherwise, an identifiable isolated modern response with a valid timestamp,
model, provider, and confirmed CLI ancestry can be estimated from at least five
unique exact valid responses in the same session, provider, model, and report
window. Each disjoint category (uncached input, cache reads, cache writes,
ordinary output, reasoning output) uses its rounded median, computed with integer arithmetic and ties rounded to even. Totals are rebuilt
from those categories. Recovered and estimated records never train estimates.
Monthly output explicitly labels estimated tokens and response counts. Internal
monthly counters distinguish exact, recovered, and estimated responses;
unresolved gaps are deduplicated and reported by count and month on stderr.
Candidates without sufficient evidence stay excluded, with diagnostics retained.
Events known to be outside the window do not produce token warnings. Records
whose timestamp or contents cannot establish relevance retain diagnostics.

The formats follow the upstream
[Codex protocol](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs)
and [usage persistence tests](https://github.com/openai/codex/blob/main/codex-rs/core/tests/suite/token_usage_rollout.rs).
Local verification covers logs from Codex 0.142 through 0.166; future format
changes may require reader updates.

## Estimated cost

Rates come from [models.dev](https://models.dev/api.json), fetched fresh with a
5-second timeout and one retry for transient network failures only when the report contains usage. Matching uses the exact
provider and model, with no fuzzy substitutions. Input context tiers apply
above their stated thresholds.

Cost uses uncached input, cached reads, cache writes, and output at their
respective per-million-token rates. Reasoning adds no separate output charge.
A missing required rate or model makes the month's entire cost `unknown`.
Oversized numeric rates, tier thresholds, and overflowing cost arithmetic also
produce unknown cost without discarding tokens, including overflow when finite
record costs are added to a monthly total. Explicit zero prices remain zero.
These are current API-equivalent estimates,
not historical invoices, subscription charges, or quota consumption.

A successful fetch updates `~/.cache/codex-usage/models-dev.json`. If the network
fails, including malformed HTTP responses or an interrupted response body, a usable cache supplies
stale prices with a note and the cache modification date on stderr. Without usable prices, cost is unknown. Cache-write failures produce notes without discarding
live pricing. Pricing notes never claim that token coverage is incomplete.
Invalid catalogs and permanent HTTP or certificate failures are not retried. Cache updates use a private temporary sibling and atomic replacement
so a failed write preserves the previous offline catalog. The pricing cache and
its temporary sibling are the only persistent data files this tool writes.

## Verification

```bash
python3 -m unittest discover -v
python3 -m py_compile codex-usage test_usage.py
./codex-usage --help
./codex-usage
```

Tests create synthetic sessions in temporary directories and freeze the reader clock
at November 3, 2026 so fixture records stay in the report window. They cover compressed
and plain logs, duplicate records, CLI ancestry, mixed usage formats, forks,
month filtering, alternate recovery, cumulative reconstruction, isolated median
estimation, read-only state ancestry, ambiguity rejection, cache arithmetic,
context tiers, unavailable prices, malformed logs, transient retries, offline
pricing, and CLI exit behavior. No typechecker is configured;
syntax compilation is a separate check.

## License

MIT, see [LICENSE](LICENSE).

Conflicting valid copies are excluded with a coverage warning rather than choosing
an arbitrary count. Valid response counters are never replaced with median estimates.
Duplicate neighboring responses prevent cumulative recovery or median fallback.

Available partial counters constrain every recovery source, including alternate files.
A positive, unambiguous final cumulative delta is recovered exactly. Resets,
conflicting alternate counters, and shared legacy ownership remain unresolved
rather than being replaced by a median.
