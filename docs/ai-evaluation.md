# AI traces and evaluation

The target experience is **黑暗海洋奇幻** with responsive player creativity,
occasional dark wit, and consequences consistent with the engine.

## Capture and inspect a session

Set `AI_DEBUG_CAPTURE=full` in the bot environment and restart. For Compose,
recreate the process to reload `.env`:

```bash
docker compose up -d --force-recreate bot
```

`LOG_LEVEL` independently controls console diagnostics. Full capture writes
`DATA_DIR/debug/trace-*.jsonl`: user input/output, model prompts and responses,
decisions/probabilities, validation, snapshots, and commit/delivery status, linked
by `trace_id`. Known keys and tokens are redacted; headers and provider reasoning
are excluded. Internal campaign facts and player identifiers remain in this
local artifact for reproducibility.

Rotation is 25 MiB per file, seven days, and 250 MiB total, enforced on writes.
Use `AI_DEBUG_CAPTURE=off` and restart to stop capture. Files remain until normal
retention cleanup or explicit removal. Full capture is separate from the
always-on structured operator audit in SQLite.

```bash
# Export all retained turns; add --trace-id ID to select one.
python -m core.trace_eval export data/debug --output session.jsonl

# Objective metrics; missing human annotations yield null labeled metrics.
python -m core.trace_eval evaluate session.jsonl

# Replay captured responses in an isolated temporary database.
python -m core.trace_eval replay session.jsonl --trace-id TRACE_ID
```

Compose uses the existing `campaign-data` volume; no additional volume or HTTP
debug endpoint is needed:

```bash
docker compose exec bot python -m core.trace_eval export /app/data/debug --output /app/data/session.jsonl
docker compose cp bot:/app/data/session.jsonl ./session.jsonl
```

Replay has no live model transport and never connects to the original campaign
database. It compares output, snapshot, request bodies, and call use; mismatches
return exit status 1. It preserves streaming mode, pending checks, RNG state,
and recorded verdict delivery failure. It does not reproduce network timing or
original stream chunk boundaries. Missing records are an explicit error. Secret
replacement can change meaning and cause a legitimate mismatch.

## Human labels

Add one JSON object per turn to `labels.jsonl`, replacing the trace ID:

```json
{"trace_id":"TRACE_ID","category":"creative","clarification":false,"required_details":["帽子"],"hint_useful":true,"canon_consistent":true,"humor":3,"enjoyment":4}
```

```bash
python -m core.trace_eval evaluate session.jsonl --labels labels.jsonl
```

`category` is the expected action category. `clarification` says whether a
clarification was appropriate, to measure unnecessary clarification.
`required_details` checks literal substrings in final user output; omit it when
paraphrasing is acceptable. `hint_useful` and `canon_consistent` are human judgments.
Use a shared 1–5 rubric for `humor` and `enjoyment`: humor means fitting dark wit
when appropriate, not constant jokes. Score serious scenes for their intended
atmosphere; omit humor when irrelevant. Averages include only annotated numeric
or Boolean values. `model_ms` sums model latency, not Discord delivery time.

## Improvement priorities

1. **Calibrate on actual player language.** Build a versioned, labeled set of
   Cantonese, written Chinese, English, mixed-language, negated, hypothetical,
   compound, and reference-dependent inputs. Compare native Jev with fallback.
   Tune the 0.85/0.8 routing thresholds using mistakes, unnecessary clarification,
   and cost; self-reported LLM confidence alone cannot establish correctness.
2. **Expand effects from playtest evidence.** Add a small named engine effect
   when repeated ideas cannot use existing actions. Define costs, limits,
   ownership, failure behavior, and save format first. Keep values engine-owned;
   add settlement and rollback regressions before exposing it to the director.
3. **Review the experience with people.** Judge whether a response acknowledges
   the method, preserves dark atmosphere, offers meaningful choices, and respects
   consequences. Prefer optional hints showing current opportunities over
   directing the player's next action.
4. **Use traces to control latency and failures.** Track fallback, calls, latency,
   proposal rejection, and clarification by category. Keep simple actions on the
   direct path and defer progress under budget pressure. Add an evaluation case
   for recurring failures before changing prompts.

Regression suites `test_system_one.py`, `test_improvisation_hints.py`, and
`test_trace_capture.py` cover contracts, delegation, budgets, public payloads,
typed bypass, creative detail, dark tone, approval/rejection, ownership,
rollback, restart settlement, hints, redaction, streaming, and offline CLI replay.
Live Jev accuracy and subjective play quality still need labeled play sessions.
