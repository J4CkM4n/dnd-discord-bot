# Private adventures and server admins

Copy `.env.example` to `.env` and optionally set `ADMIN_ID` to one Discord user ID.
Enable Discord Developer Mode, right-click the user, and choose Copy User ID.
The value must be one positive numeric ID, with no mention syntax or comma-separated
list. Invalid nonempty values stop startup. Empty keeps existing Manage Server access.
Restart after editing environment variables.

The initial admin has bot access in every server they have joined. Commands cannot
change or demote that identity. Additional admins are scoped to a Discord server:
any current admin may promote or demote another human member of that server. Each
server can have multiple admins, and their access covers all its game channels.
Discord Manage Server is an independent access path. Demotion removes an explicit
promotion and explains when the member still has Manage Server access. Commands
check current promotions and permissions on each request, including privileged
autocomplete, pending confirmations, and check overrides. Pending actions still
belong to the user who proposed them. DMs cannot use admin commands.

| Command | Effect |
| --- | --- |
| `/promote member` | Grant bot admin access in this server |
| `/demote member` | Remove this server's explicit promotion |
| `/admins` | Privately list the protected initial admin and promotions, and explain Manage Server access |
| `/script submit file` | Read a slash-command attachment immediately into private bot storage and prepare a draft |
| `/script status` | Privately preview progress, the starting scene, setting, outline, NPCs, items, encounters, objectives, clocks, events, secrets, notes, and issues |
| `/script approve draft:<ID>` | The submitting admin approves that exact reviewed draft and selects its immutable version |
| `/script discard draft:<ID>` | The submitting admin deletes that unapproved draft |
| `/script clear` | Any current admin cancels this channel's next-game selection |

Admin management and all script replies are ephemeral. These commands have no
default Manage Server registration restriction, so promoted members can invoke
them. Runtime authorization remains required. Existing Discord integration command
overrides may need adjusting if a server owner has manually restricted a command.

## Upload and review

Submit a UTF-8 `.txt` or `.md` attachment using `/script submit`, without posting a
message containing the file in the channel. A UTF-8 BOM is accepted. Files exceeding
1 MiB or 100,000 decoded Unicode characters are rejected explicitly; the bot does
not truncate them. Empty, binary, and invalid UTF-8 input is rejected.

Preparation runs asynchronously. `/script status` reports completed chunks plus
the final reference-linking pass. A bounded text preview is accompanied by the
complete extracted JSON when a definition is available. Review the starting scene,
all extracted content, and the separation between public facts, secrets, and author
notes. Conversion is model-assisted, so review factual fidelity as well as the
engine validation results. `ready` means the engine schema passed; `blocked` or
`failed` drafts cannot be approved. Correct the source or model configuration and
submit again to resolve issues.

Copy the draft ID from the private response into `/script approve`. Approval and
discard require both that ID and the submitting admin's current authorization.
Replacing a draft invalidates older IDs, even when submitted by the same admin.
Other admins can inspect it privately and clear a selection, but cannot approve
or discard someone else's draft. Replacing or discarding an unapproved draft does
not change the previously selected approved version. An already approved version
is retained; use `clear` to cancel its next-game selection.

`/new_game` activates the selection only in a channel with no characters or game
activity. In an existing game, use `/reset_game confirmation:reset confirm` to
archive the old campaign and initialize the selection with an empty party in one
SQLite transaction. A failed save publishes no replacement world, rules, or plot.
With no selection, the original starter adventure is used. Uploads, approvals,
clears, and restarts cannot change a running campaign's pinned definition. Starter
repairs never modify custom adventures, even when their scene names match.

## Model processing and supported definitions

The source is processed by the existing model gateway using the configured
`roles.director` model/provider. Long scripts are split at paragraph boundaries
without losing characters. Each chunk extracts structured content; later chunks
receive an entity registry. A final linking pass resolves references across the
whole adventure, and deterministic validation checks the merged definition.
Preparation has its own bounded call budget, separate from gameplay turns.

Choose a director model capable of structured JSON. Complex scripts may require
a larger `roles.director.max_tokens` and `timeout` in `agents.toml` (for example
6,000 tokens and 180 seconds). Configured provider and token limits are respected.
Truncated responses, provider failures, incomplete conversion, conflicting
definitions, unresolved references, and unsupported mechanics prevent approval.
Interrupted jobs resume from their stored source on bot startup, redoing extraction.

Schema version 1 contains a title, starting scene, public setting, scenes and exits,
NPCs and their private knowledge, visible/hidden item stacks, encounters, plot,
secrets with disclosure references, private author notes, completeness, and issues.
Identifiers must be unique lowercase ASCII identifiers. Scene destinations, NPCs,
items, clocks, and beat references must resolve. Encounter HP, AC, attack bonuses,
and damage dice are checked; missing authored statistics are reported rather than
invented. The schema and validator live in `core/adventures.py`.

The plot uses the existing engine conditions (`all`, `turn_gte`, `scene`, `item`,
`enemy_dead`, `npc_gone`) and effects (`consume_item`, `npc_move`, `npc_disposition`,
`spawn`, `clock`, `npc_spawn`, `cond`, `inspire`). Preauthored party conditions
currently support poisoned and inspired. Other rules, custom spells, traps, and
mechanics require an engine implementation before conversion can be approved.
Secrets disclose through engine-owned NPC knowledge checks or triggered plot beats.
Notes cannot appear in runtime data; declared secrets cannot appear in public
setting facts, scene descriptions, NPC descriptions, or public objectives.

## Storage and privacy

`DATA_DIR/campaigns.db` uses additive migrations. `bot_admins` stores promotions,
promoter IDs, and timestamps; `admin_audit` records independent promotion/demotion
metadata. Neither table is part of campaign resets or archives.

`script_drafts` stores the current private source, owner, preparation state,
definition, and issues for each server/channel. `adventure_versions` retains
immutable approved definitions and their sources. `script_selections` chooses
the next version per server/channel. The active version and definition are pinned
in the campaign snapshot, independently of future selections. `clear` removes a
selection, not retained approved versions or running games. `discard` removes only
the current unapproved draft. The bot process and database operators can read this
private data, including normal database backups.

The bot never reposts the source, creates a public upload message, or stores the
Discord attachment URL. Source text and private author notes go only to private
storage, authorized private reviews, and the configured director provider during
preparation. They are excluded from routing, ordinary gameplay prompts, narration,
command receipts, application logs, and debug captures. Diagnostic payloads and
captures are suppressed for preparation and custom campaigns. Narration receives
public engine facts and disclosures already authorized by the engine. Default
starter gameplay retains its existing opt-in capture behavior.
