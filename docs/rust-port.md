# Rust implementation

Build the native application from the repository root:

```sh
cargo build --release --offline
```

Launch the terminal monitor with `target/release/envitop`. Produce one compact Snapshot JSON line with `target/release/envitop --once --json`.

The implementation reads Linux `/proc`, `/sys`, and mount information directly. It queries NVIDIA data with `nvidia-smi`, and reads OpenCode session totals through the system SQLite library in read-only mode. Claude Code and Codex sessions are read from their JSONL files. The optional `ENVITOP_TRACE_FD` hooks write benchmark records only to the inherited descriptor. Network snapshots include per-interface byte counters and interval rates; token rate uses sampled session-token deltas.

Current limitations: agent sessions include recent token totals and task titles, but lack the Python collector's summaries, child trees, richer transcript details, live rate-limit events, and exact context-window estimates. The terminal layout supplies the main views, selectors, charts, and controls; it does not reproduce every Textual widget or visual detail. Rate-limit cache files are read when present.
