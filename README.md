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
command per action instead of one polymorphic menu.

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
   can only reveal those facts
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
├── ledger.py           # append-only event log (replayable)
├── digestor.py         # 12b: freeform text → Intent JSON
├── narrator.py         # 27b: facts + skeletons → prose
├── templates.py        # deterministic prose skeletons
├── guards.py           # output defenses (placeholders, scrubbing, s2t)
├── ledger.py           # append-only event log (replayable)
├── service.py          # orchestration + persistence
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

## License

SRD content: CC-BY 4.0 (Wizards of the Coast)
Code: see LICENSE
