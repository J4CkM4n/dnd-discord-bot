# Chat-history repairs

The exported sessions showed malformed pickup nouns (`起`, `出木塞`), inspection turning into searches, lost transfers, invalid potion quantities, incorrect actor/recipient narration, dead encounters restarting, pending rolls blocking the party, and a starter story with no usable continuation.

Object commands now resolve owned or visible names unambiguously. Bare pickup selects the sole ground stack or asks for a choice. Inspection and taking an item out preserve quantity. Transfers use the same validation and transaction as other actions. Potion quantities must be integers from 1 to 20; group healing checks the entire supply before spending anything, and combat permits one potion per action. Inert item use preserves the item.

The starter campaign unlocks the cellar after the goblins are defeated, then the sea route and altar after the guard. Barter through a successful conversation or combat can recover the necklace. Returning it at the dock consumes it once and completes the authored ending. Recognizable old starter saves recover the missing routes from encounter and ledger evidence. Existing scenes, disclosures, party position, and consumed loot are preserved. `/continue` offers the current objective and a route through existing exits; it never replays mechanics. Generated search rewards are capped at one per scene and materialized props at three.

Players can inspect items or view status/inventory during a pending roll. A blocked action explicitly requires resubmission after the roll; there is no hidden queue. `/plan status` identifies the pending check and `/plan cancel` lets its owner or a server manager cancel it, including after a restart. Checks whose character has disappeared or whose scene no longer matches expire on load. Cancellation grants no check effect and refunds no spent action or materials.

Discord action, confirmation, plan, transfer and roll paths pass interaction IDs. The service serializes requests and stores receipts with the mechanics, retaining the latest 128 receipts per channel. Duplicate requests reuse their recorded outcome; interruption after commit returns recovery guidance without executing again. Narration uses consistent character placeholders, treats pending actions as unresolved, and buffers model drafts until output guards finish. Generated scene proposals retain their existing evaluation and approval gates.

Inventory cleanup is separate from code and story migration. Run the following against the intended campaign database to create a consistent SQLite backup and a review report:

```powershell
.\.venv\Scripts\python.exe -m tools.audit_inventory --database data/campaigns.db --output-dir data/repair
```

The tool changes no inventory. It lists only known parser artifacts with ledger evidence and excludes admin grants; quantities and event IDs are included for review. Large stacks alone are not evidence of duplication. No production database was present in this checkout, so no live cleanup was applied.

Validation uses the full pytest suite, including the exported-command regressions, storage rollback and retry tests, restart recovery, guarded narration, and a deterministic offline three-player journey through combat, negotiation, necklace pickup and return. It does not exercise a live Discord server or a live model provider.
