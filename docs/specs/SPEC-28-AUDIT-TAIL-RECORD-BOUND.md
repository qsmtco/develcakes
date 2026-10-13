# SPEC-28: Audit-tail record-bound fix (SPEC-26 BUG #3 follow-up)

Status: ready-for-implementation
Origin: SPEC-26 round-2 Debugger audit BUG #3 (pre-existing since SPEC-22 BUG #7 fix-round)
Files: utils/provider_failure_store.py, tests/test_failure_classifier.py
Probes (repro evidence): scratch/probe_recent_cold.py, scratch/probe2.py

## Problem

_tail_audit_records() docstring claims "the last max_lines provider_failure
records" but bounds by RAW lines BEFORE the kind filter, in two stacked places:
1. byte window: window = min(size, max_lines * 1024)
2. line slice:   lines[-max_lines:]

audit-log.jsonl is shared with other writers (agent_call etc.), so unrelated
traffic evicts provider failures from the cold-read window. Verified repro:
50 provider_failure lines followed by 150 unrelated lines -> 0 records
rehydrate. Both cold readers are affected: recent() and summary().

## Fix — backward chunked scan

- Signature: _tail_audit_records(max_records: int = _CAP) -> list[dict]
  (both call sites use the default; rename is safe)
- Module constants, monkeypatchable in tests:
  _TAIL_READ_CHUNK  = 64 * 1024
  _TAIL_MAX_SCAN_BYTES = 8 * 1024 * 1024
- Scan from EOF backward in _TAIL_READ_CHUNK chunks; iterate lines
  newest -> oldest; keep dicts with kind == "provider_failure" until
  max_records collected.
- Stop when ANY of: collected >= max_records, file start reached,
  scanned bytes >= _TAIL_MAX_SCAN_BYTES (bounded; never O(file) unbounded).
- Chunk-boundary fragments: the first (unterminated) line of each chunk is a
  fragment unless the chunk starts at offset 0 — carry it into the next
  (older) chunk's buffer; never parse a known fragment.
- Malformed JSON lines skipped individually (preserve current behavior);
  never raises; missing/unreadable log -> [].
- Return matches in file order (oldest -> newest), truncated to the LAST
  max_records.
- Docstring updated to state real guarantees: record-bound, bounded scan,
  explicit fragment handling (replaces the overstated "complete lines" note).

## Non-goals

- Dedicated provider-failures file (Debugger option 2) — separate proposal
- Any warm-path change: record(), _append_audit, recent()/summary()
  post-processing, _coerce_* helpers are frozen
- No new config knobs, no cutoff changes

## Tests (tests/test_failure_classifier.py; cold idiom = direct file write, no record())

1. test_tail_returns_failures_despite_unrelated_traffic — 50 provider_failure
   lines then 150 other-kind lines; recent() cold rehydrates all 50
2. test_summary_cold_ignores_unrelated_traffic — same log shape;
   summary()["last_24h"] == 50
3. test_tail_stops_at_matched_cap — 150 provider_failure lines; exactly the
   last _CAP returned, file order, newest kept
4. test_tail_multichunk_fragment_handling — monkeypatch _TAIL_READ_CHUNK
   small to force >= 3 chunks with a failure record spanning a boundary;
   valid records survive, no raise, no duplicated/merged records
5. test_tail_scan_byte_cap — monkeypatch _TAIL_MAX_SCAN_BYTES tiny; log
   larger than cap with zero matches -> [] (bounded read proven)
6. test_tail_missing_log_returns_empty — nonexistent path -> [] (pins contract)

Restore monkeypatched constants via addfinalizer/finally.

## Acceptance

- Both probes no longer demonstrate eviction
- tests/test_failure_classifier.py green (68 existing + 6 new = 74)
- ruff clean on touched files
