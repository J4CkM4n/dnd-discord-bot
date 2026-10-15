# D&D DM Bot — v6 · Engine × Generative Director

Self-hosted Dungeon Master for Discord. **Game engine does 70%** (rules,
state, combat, dice); **LLM does 30%** (reading player intent, writing
prose). The game is fully playable without any LLM.

## Architecture

```
Player text ──→ [Digestor LLM 12b] ──→ Intent JSON
                                             │
                                             ▼
                                    [GAME ENGINE]  ←── no LLM
                                    validate + resolve
                                    dice · HP · items · combat · scenes
                                             │
                                             ▼
                                    [Ledger]  append-only facts
                                             │
                              ┌──────────────┼──────────────┐
                              ▼              ▼              ▼
                        [Templates]    [Narrator 27b]   [Debug Portal]
                        skeleton prose  polish + flavour  #dnd-health
                        (no LLM)        (s2t + guards)    log stream
```

## Quick Start

```bash
# GB10 (or any box with Ollama)
pip install -r requirements.txt
cp .env.example .env        # fill DISCORD_TOKEN, LLM_URL, V4_CHANNEL_IDS
                            # (V4_* names kept for config stability)
python bot.py

# no-LLM test (proves the engine works standalone)
python core/cli.py selftest

# interactive CLI (zero LLM)
python core/cli.py
```

## Commands (26: 16 player + 10 admin)

| Player | Admin |
|---|---|
| `/pc name occupation` — create character | `/explore-admin text char` |
| `/explore <text>` — freeform action | `/attack-admin char target move` |
| `/confirm` — confirm pending action | `/use-admin char item on` |
| `/attack [target] [move]` — typed combat family | `/skill-admin char skill on` |
| `/use [item] [on]` — item / feed a DOWNED ally | `/defend-admin` `/flee-admin` |
| `/skill [skill] [on]` — 18 skills ★, context targets | `/observe-admin char target` |
| `/defend` `/flee` `/observe [target]` | `/roll-admin expr char` |
| `/inventory` — items + slots + skills + moves | `/give-admin char item qty` |
| `/roll [expr]` — dice (settles checks, auto-continues) | |
| `/give item [to]` — transfer; no recipient = set down | |
| `/status` — party + scene + disclosed intel | |
| `/rules <query>` — SRD lookup (zh aliases, no LLM) | |
| | `/inspire-admin [char]` — grant inspiration |
| `/continue` — finish unfinished narration, else unstuck | |
| `/help` | |

Every combat command has its own typed autocomplete (enemy HP,
item quantities, ★-proficient skills, downed-ally markers) — one
command per action instead of one polymorphic menu. During combat,
verdict messages carry one-tap buttons (🛡 defend · 👁 observe ·
🏃 flee · ⏭ pass); arg-ed actions stay typed commands.

**In-channel keywords** (instant, no LLM): `status` `inv` `moves` `scene`

**Plain text** = table talk (silently ignored, no reaction, no reply)

## The 18 Skills

All SRD skills are wired into the engine (defined in `engine/charlib.py`,
proficiency per class in `CORE_SKILLS`). Describe the action in `/explore`
— 匿埋（潛行）、嚇佢（恐嚇）、包紮（醫藥）、爬牆（運動）— or pick from
`/combat` → ✨技能 (★ = proficient). Engine effects on success:
stealth → next attack with advantage · insight → read an NPC's true
attitude · medicine → revive a downed ally · perception/investigation →
find hidden items · social skills → move NPC disposition up the ladder.
`/inventory` shows each character's full skill sheet.

## Design Principles

1. **Engine decides, model narrates** — the LLM has no write path to game
   state; it reads facts and writes prose
2. **Predict the narration** — the engine renders prose skeletons from its
   own data; the narrator only adds atmosphere
3. **NPC knowledge is data** — each NPC has a `knows` list; the narrator
   receives only the facts the engine has disclosed
4. **No LLM required** — the CLI proves the full game (combat, items,
   scenes, checks) works with zero model calls
5. **v3 lessons ported** — placeholder names, fake-dice scrubbing,
   repetition guard, language enforcement, input sanitization

## File Structure

```
gamerules.json          # single source of truth: world tone + canon +
                        # narrative constraints (injected into the
                        # narrator prompt; edit here to change the world)

core/                   # the game engine (v6)
├── intent.py           # Intent dataclass + deterministic parser
├── world.py            # Scene graph, NPCs (with knows), encounters
├── turn.py             # Game state: party, combat, inventory, slots
├── rules_core.py       # validate + resolve (the only write path)
├── ledger.py           # append-only facts with stable per-channel event IDs
├── digestor.py         # 12b: freeform text → Intent JSON
├── narrator.py         # 27b: facts + skeletons → prose
├── templates.py        # deterministic prose skeletons
├── guards.py           # output defenses (placeholders, scrubbing, s2t)
├── service.py          # serialized commands, staged turns, commit before output
├── persistence.py      # SQLite snapshots, event appends, decision audit, narration
├── state.py            # complete state codec + legacy JSON import support
├── memory.py           # bounded factual recall for director and narrator
├── director.py         # engine gate: prop/scene canonization
├── director_llm.py     # generative Director (unplanned places/finds)
├── plot.py             # plot spine: clocks + trigger-fired beats
└── cli.py              # no-LLM REPL + selftest

engine/                 # shared pure math (no LLM, no Discord)
├── charlib.py          # AC, PB, slots, save/skill proficiencies
├── dice.py             # dice expressions (4d6kh3, adv, etc.)
├── checks.py           # total modifier calculator
└── moves.py            # class move tables + compute_attack_moves

adapters/
└── discord_bot.py      # 24 slash commands (typed combat family) + routing

health/
└── health_board.py     # debug portal (log stream + /moves)

tools/                  # operational scripts
└── ingest_srd.py       # build rules.db from SRD markdown

legacy/                 # v3 code (archived, not imported)
└── tools-v3/           # v3-era tools (seed_inv, playtest, status, ...)
```

## Models

| Role | Model | Purpose |
|---|---|---|
| Digestor | qwen2.5:7b-instruct | Player text → Intent JSON (<1s; 9/9 Cantonese routing bench) |
| Narrator | qwen3.5:35b (think **off**) | Facts → streamed prose (2.5s warm; ~13× faster than the old gemma3:27b) |
| Director | qwen2.5:7b-instruct | Unplanned scenes/items as typed JSON — the engine canonizes |

All optional — the game runs with zero LLM (`python core/cli.py selftest`).
Note: qwen3.x are REASONING models — narrator requires `V4_NARR_THINK=off`
or thinking consumes the whole token budget (and the OpenAI-compat endpoint
ignores the flag; we use Ollama's native `/api/chat`).

## Director decisions and campaign memory

The director uses **Judge–Evaluate–Verify (JEV)**. Ownership, action eligibility,
existing exits/discoveries, and scene limits are checked before generation.
The model proposes one scene or inert item; the engine validates its types,
limits, uniqueness, and item restrictions before staging it with the action.
Rejected actions leave no generated world objects behind.

Set `V4_DIRECTOR_EVALUATE=on` to add one advisory model call checking relevance
and consistency against canon, current state, and campaign history. It uses the
digestor model and its thinking setting. Default: `off`. Invalid, unavailable,
or negative evaluations reject the proposal; no retries occur. Existing
deterministic gameplay remains available. This is a decision workflow, not a
trained new model; narrative consistency judgments remain fallible.

The director and narrator recall up to **12 historical engine facts / 3,000
characters**, ranked by scene, referenced characters, plot relevance, and
recency. Current state takes precedence over history. Narration, table talk,
and rejected proposals never become factual memory. Hidden items and
undisclosed NPC knowledge stay out of narrator recall. Authored `world.canon`
continues to be shared background in prompts.

## Persistence and migration

`DATA_DIR/campaigns.db` (default `data/campaigns.db`) is the authoritative store.
It contains isolated channel snapshots, append-only events, director decision
records, and narration status. Saves use SQLite transactions with WAL and full
synchronization. Each command stages its changes, commits the snapshot and new
events, then sends the verdict and starts narration. Narration completion is
saved separately, without rewriting the gameplay snapshot or past events.

Snapshots include pending checks, player-bound confirmations, combat effects,
plot progress, RNG state, and continuation references. `/confirm` and `/roll`
enforce ownership. A failed write leaves committed state intact; a failed
callback or narrator leaves a resumable `/continue`. Commands serialize within
each channel, including narration, while other channels can progress. Run one
bot process per database; revision checks reject stale writers.

On first access to a channel, v4–v6 `v4_state_<channel>.json` saves (or
`v4_state.json` for an unnamed channel) import once in a transaction. Originals
are preserved and never updated; subsequent starts use SQLite. Corrupt or
unsupported saves produce an explicit error instead of a fresh campaign.
The health board reads SQLite first and still handles unimported JSON channels.

Back up the live database with SQLite's backup API or the `sqlite3` CLI:

```bash
sqlite3 data/campaigns.db '.backup /path/to/campaigns-backup.db'
```

Use that backup for recovery. Copying just the live `.db` file can omit changes
still in its WAL. Preserved JSON files are pre-migration checkpoints, not live
backups. The ledger is an audit trail; complete snapshots are required to
restore gameplay. Existing history is loaded into memory on startup, though
only new events are written on subsequent saves.

Logs report decision reasons, model latency, commit latency, appended event
counts, and recall sizes. Offline verification:

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -q
python core/cli.py selftest
```

## License

SRD content: CC-BY 4.0 (Wizards of the Coast)
Code: see LICENSE
