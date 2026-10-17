# Chat outcome repair plan

The October 17 Discord export shows successful theft without an inventory
transfer, narration assigning the result to another player, escape described as
combat starting, a returning captain losing his injuries, and two Japanese
requests being rejected or misclassified.

1. Model theft as an attempt to take a recorded possession from a present
   character. Validate the target and item before rolling; transfer it once to
   the acting character only after success. Preserve pending attempts across
   saves and prevent stolen possessions from appearing again as combat loot.
2. Include the actor, target, item, and outcome in committed events. Reject
   narration that changes the recipient or adds unsupported injury, including
   continued narration. Render combat start, end, and escape from explicit
   outcomes.
3. Preserve an NPC's identity, descriptive information, possessions, location,
   and remaining health when hostility begins. Reuse that character on return
   and reject generated duplicates, including duplicates of saved encounters.
4. Support the Japanese observe-then-ask request in its original order and the
   theft request made while companions distract the captain. Preserve
   conditions and hypothetical or negative wording; do not execute another
   player's action or grant unearned assistance.
5. Add regressions for the chat examples, failed and stale attempts, save/load,
   duplicate loot, narration delivery, and linked action confirmation. Run the
   relevant tests, full suite, and CLI self-test, review the diff, then commit
   the plan, implementation, and tests locally.

This repair changes engine behavior and future authored state. It does not
rewrite the existing campaign save or publish a deployment.

Implementation choices: theft uses complete engine-authored prose so a model
cannot change the recipient, invent a possession, or add injury. New starter
games record one captain's dagger; loading a save never refills it. NPC metadata
travels with the saved combatant, and encounter scaling runs only at first
spawn. The Japanese observation request keeps a saved second step requiring
`/confirm`; conditional theft requires a companion's actual `/help` action.

Validation: 561 tests passed, including 40 regressions for these outcomes and
requests. The offline CLI self-test passed. Theft attempt and settlement traces
replay with matching outputs and snapshots, and a failed storage commit leaves
the pending roll and both inventories intact.
