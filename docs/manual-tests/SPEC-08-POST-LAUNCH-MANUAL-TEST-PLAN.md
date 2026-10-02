# DevelCakes Manual Test Plan — Post SPEC-08 (Transcript Store)

**Purpose:** First live validation of the transcript store against your REAL config
dir (~5,800 conversations). The test suite only ever ran on sandboxed tmp dirs.

**Launch command:** `cd /home/mushy/projects/develcakes && .venv/bin/python main.py`

**CRITICAL pre-flight (already done by Supervisor, verify before launching):**
```bash
ls ~/.config/crabcakes/conversations/*.json | wc -l    # expect ~5,818
ls ~/.config/crabcakes/conversations/*.migrated | wc -l # expect 1 (supervisor archive)
ls ~/.config/crabcakes/*.db*                             # expect NOTHING (pre-migration)
```
Snapshot safety line before step 1:
```bash
cp -r ~/.config/crabcakes/conversations /tmp/conversations-backup-$(date +%s)
```

---

## PART A — First-launch migration (the big one)

**A1. Launch and watch the feed.** Expected within ~30–60s (5.8k files, background
thread — app must stay responsive the whole time):
- A feed card: **"Transcript migration complete"** with body stats: migrated count,
  skipped, turns, seconds, kept-on-JSON count, errors count.
- The UI does NOT freeze during the sweep (scroll the feed, click tabs while it runs).

**A2. Verify the store materialized.**
```bash
ls -la ~/.config/crabcakes/transcript.db*   # db + -wal + -shm, all 0600 (-rw-------)
```

**A3. Verify renames happened.**
```bash
ls ~/.config/crabcakes/conversations/*.json.migrated | wc -l   # should be ~thousands now
ls ~/.config/crabcakes/conversations/*.json | wc -l            # survivors (diverged/active/failures)
```

**A4. Content spot-check (pick any migrated session):**
```bash
F=$(ls ~/.config/crabcakes/conversations/*.json.migrated | head -1)
.venv/bin/python -c "
import json, sqlite3, sys
d = json.load(open('$F'))
key = d.get('session_key') or '$F'.split('/')[-1].replace('.json.migrated','')
c = sqlite3.connect('$HOME/.config/crabcakes/transcript.db')
rows = c.execute('SELECT COUNT(*) FROM turns WHERE session_key=?', (key,)).fetchone()[0]
print(f'file: {len(d[\"messages\"])} msgs | store: {rows} turns | match: {len(d[\"messages\"])==rows}')"
```
Expected: `match: True`.

**A5. Second launch — idempotence.** Quit, relaunch. Expected: NO migration card
(or one reporting migrated=0); `.migrated` count unchanged; DB grows only by new turns.

**A6. Integrity probes.**
```bash
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('$HOME/.config/crabcakes/transcript.db')
print('journal_mode:', c.execute('PRAGMA journal_mode').fetchone()[0])   # wal
print('integrity:  ', c.execute('PRAGMA integrity_check').fetchone()[0]) # ok
print('sessions:', c.execute('SELECT COUNT(*) FROM sessions').fetchone()[0])
print('turns:   ', c.execute('SELECT COUNT(*) FROM turns').fetchone()[0])
print('diverged:', c.execute('SELECT COUNT(*) FROM sessions WHERE diverged=1').fetchone()[0])"
```
Report all five numbers to me (diverged>0 is EXPECTED for long/compacted sessions —
they stay JSON-backed by design).

## PART B — Post-migration session behavior

**B1. Old session, new turn (the core loop).** Open an agent tab with existing
history → send a message → get a reply → check:
- History renders (loaded via store-mode — JSON is `.migrated` now).
- The reply lands. Then verify the delta append:
```bash
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('$HOME/.config/crabcakes/transcript.db')
key='special:coder'   # or whichever you used
print('turns:', c.execute('SELECT COUNT(*) FROM turns WHERE session_key=?',(key,)).fetchone()[0])
print('watermark vs max-seq match:', c.execute('SELECT (SELECT watermark FROM sessions WHERE session_key=?)-(SELECT COALESCE(MAX(seq),-1) FROM turns WHERE session_key=?)',(key,key)).fetchone()[0]==0)"
```

**B2. Quit mid-conversation, relaunch, same tab.** History intact through
store-mode load (this is the SP4A path working end-to-end).

**B3. Compact-diverged session.** Find a long agent session (or make one: paste a
huge doc, force compaction), send more turns. It should keep working; its JSON (if
it still has one) is never renamed; the banner (next migration run) lists it as
kept-on-JSON.

## PART C — Activity pill + regression sweep (SPEC-07 carryover)

- **C1.** Send a message → pill shows ⬡ Pre Flight Check → ◉ Reasoning… → ⬇ Generating…
  (with live tokens/tok-s/elapsed) → ⚙ tool name if a tool fires → ✓ Done → ● Idle.
  Colors change per state (gray/amber/blue/green).
- **C2.** Switch tabs mid-stream → pill on the other tab isn't stuck; return —
  current state shows.
- **C3.** Old Response Status bar is GONE (chat fills the pane — no 40px bar).
- **C4.** Project tab: open a project → pill reflects the project surface.

## PART D — Permissions & security

```bash
stat -c '%a %n' ~/.config/crabcakes/transcript.db*    # all three 600
# Key-shaped check (20+ char key material, not mere mentions of "sk-" in
# conversation content — build/audit chats legitimately quote test fixtures
# like sk-secret-12345 and grep commands):
.venv/bin/python -c "
import re, os
home = os.path.expanduser('~')
blob = open(home+'/.config/crabcakes/transcript.db','rb').read() + open(home+'/.config/crabcakes/transcript.db-wal','rb').read()
hits = re.findall(rb'sk-[A-Za-z0-9_-]{20,}', blob)
print('key-shaped strings:', len(hits), hits[:3])   # expect: 0 []
print('total sk- mentions (content):', len(re.findall(rb'sk-', blob)))  # >0 is fine — it's chat content"
```

## PART E — Kill-switch (operator override)

```bash
CRABCAKES_MIGRATE_STORE=0 .venv/bin/python main.py    # boots, NO migration runs
```
(Already-migrated sessions still load via store — the flag only gates the sweep.)

---

## If anything looks wrong

Stop at the failing step, note which one, and tell me the observed vs expected.
Safety nets at every point: `.migrated` = full original content (reverse-rename
recovers); the backup from the pre-flight line is your belt-and-braces.
