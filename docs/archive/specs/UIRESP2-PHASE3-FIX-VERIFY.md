# PHASE 3 FINAL-AUDIT — FIX VERIFICATION REQUEST

**To:** Debugger
**Mode:** post-fix verification (your final-audit findings → my fixes). No fresh 11-section probe — verify the five dispositions below and give the loop's closing verdict.

## Your findings → dispositions (commit ecd6300)

1. **MEDIUM racy `TestSeqNumHandler` tests** → FIXED: both `test_seq_num_on_project_open_reconstruction` and `test_seq_num_migration_assigns_to_cards_without_it` now take `monkeypatch` and set `fh.threading = _SyncThreading` (the shim you verified at :4159), making `on_project_opened`'s load hop synchronous. Stress-verified 3× in the exact order that flaked (`TestWindowCompaction` then `TestSeqNumHandler`): 23 passed each run, ~0.45 s stable.
2. **Spec/code placeholder drift** → FIXED by aligning CODE to spec: `ui/handlers/feed_handler.py:1328` is now `project_name="",   # assigned inside _ui by reverse lookup (spec §2.3.5)` — the empty placeholder, no writer-thread `_active_project_name` read at all. (You traced the drift as benign-but-drifting; the spec's empty placeholder is the cleaner contract — no writer-thread state read in card construction.)
3. **Missing spec invariant-4 test** → ADDED: `TestWindowPruning::test_load_feed_under_100ms_at_window_default` — `make_feed(9500)` → `save_feed` → `compact_feed(window=FEED_WINDOW_DEFAULT)` → `load_feed` timed. Structural companion per §9: `len(loaded) == FEED_WINDOW_DEFAULT` exactly. Passes in 0.50 s total (load well under 100 ms).
4. **ARCHITECTURE.md line counts** → FIXED: 2266→2276 (`feed_handler.py`, both §2 and §11 trees), 935→948 (`feed_store.py`).
5. **Process note (sweep-in commits)** → logged for the post-mortem §6; no repo action (your own assessment: the parent spec sweep-in was legitimate; the two drafts are on-topic supervisor work; history surgery is the worse evil).

## Verify

```bash
git show ecd6300 --stat
PYTHONDONTWRITEBYTECODE=1 xvfb-run -a python3 -m pytest tests/test_feed_store.py tests/test_low12_13_feed.py tests/test_feed_handler.py -q   # 247 passed
# stress (the flake): 3× TestWindowCompaction + TestSeqNumHandler — all 23/23
grep -n 'project_name=""' ui/handlers/feed_handler.py    # the placeholder
grep -c "_SyncThreading" tests/test_feed_handler.py      # now used by the seq tests too
```

**Closing question:** with these five dispositions landed, do you give the loop its clean bill (ACCEPT) for the post-mortem? If any finding is not genuinely closed, name it.
