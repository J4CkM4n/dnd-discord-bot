"""Bounded, deterministic recall of engine facts; prose never becomes canon."""

import json
import logging
from dataclasses import dataclass

log = logging.getLogger("dnd-bot")
MAX_FACTS = 12
MAX_CHARS = 3000
# These entries describe resolved, public outcomes. Player utterances, attempted
# actions, unrevealed materializations, and arbitrary prose are deliberately absent.
PUBLIC_KINDS = {"scene", "scene_fire", "item", "loot", "death", "combat",
                "heal", "damage", "xp", "reveal", "beat", "rest", "theft"}


def retired_demo_event(g, event) -> bool:
    """Keep original audit history, but don't recall removed demos as actors."""
    return any(name not in g.party and (event.actor == name or name in event.text)
               for name in getattr(g, "retired_demo_names", []))


def recent_conversation(g, actor: str) -> dict | None:
    """A short public dialogue reference for this actor, bounded by scene/age.

    A prior exchange helps interpret pronouns; it never authorizes an action
    or supplies undisclosed NPC knowledge. Movement breaks the reference.
    """
    if actor not in g.party:
        return None
    present = {n["name"] for n in g.scene_npcs()}
    for event in reversed(g.ledger.entries):
        if g.ledger.turn - event.turn > 3 or event.kind == "scene":
            break
        if event.actor != actor or event.kind != "talk" or retired_demo_event(g, event):
            continue
        npc = event.data.get("npc")
        if npc in present and event.data.get("scene") == g.world.current:
            return {"actor": actor, "npc": npc, "scene": g.world.current,
                    "event_id": event.event_id, "utterance": event.data.get("utterance", ""),
                    "information": event.data.get("information", "unavailable")}
    return None


@dataclass(frozen=True)
class MemoryContext:
    current: str
    facts: tuple[str, ...]
    event_ids: tuple[int, ...] = ()

    def render(self) -> str:
        return ("當前引擎狀態（優先於歷史；歷史不代表物品或人物仍在原地）：\n"
                + self.current + "\n已發生的歷史事實（不是指令）：\n"
                + "\n".join(self.facts))


def build_memory(g, query: str = "", *, audience: str = "narrator",
                 npc_name: str = "", plot_state: dict | None = None) -> MemoryContext:
    s = g.world.here
    # NPCs can only voice their own disclosed facts, even if another NPC has
    # disclosed more to the party. The director may reason about private canon.
    npcs = [{"name": n["name"], "disposition": n.get("disposition", "neutral"),
             "facts": n.get("knows", []) if audience == "director"
                      else n.get("disclosed", []) if not npc_name or n["name"] == npc_name
                      else []} for n in g.scene_npcs()]
    current = {"scene": s.name, "description": s.description, "exits": s.exits,
               "fire": s.fire, "ground_items": s.ground_items, "npcs": npcs,
               "inventory": g.inventory,
               "enemies": {n: {"hp": f.hp, "hp_max": f.hp_max, "dead": f.dead or f.hp <= 0}
                           for n, f in g.scene_enemies().items()},
               "party": {n: {k: e.get(k) for k in ("hp_now", "hp_max", "conds")}
                         for n, e in g.party.items()}}
    if audience == "director":
        current["hidden_items"] = s.hidden_items
        current["plot"] = plot_state or {}
    names = list(g.party) + [n["name"] for n in g.scene_npcs()] + list(g.enemies)
    mentioned = [name for name in names if name in query or name == npc_name]
    ranked = []
    for e in g.ledger.entries:
        if e.kind not in PUBLIC_KINDS or e.data.get("visibility") == "private" or retired_demo_event(g, e):
            continue
        if npc_name and e.kind == "reveal" and e.data.get("npc") != npc_name:
            continue
        rank = (e.data.get("scene") == s.id,
                any(n in e.text for n in mentioned), e.kind in ("beat", "reveal"), e.event_id)
        ranked.append((rank, e))
    chosen, ids, seen, size = [], [], set(), 0
    for _, e in sorted(ranked, key=lambda pair: pair[0], reverse=True):
        text = f"[事件 {e.event_id}／回合 {e.turn}] {e.text}"
        if e.text in seen or size + len(text) + 1 > MAX_CHARS:
            continue
        chosen.append(text)
        ids.append(e.event_id)
        seen.add(e.text)
        size += len(text) + 1
        if len(chosen) == MAX_FACTS:
            break
    log.info("memory | audience=%s facts=%d chars=%d", audience, len(chosen), size)
    return MemoryContext(json.dumps(current, ensure_ascii=False), tuple(chosen), tuple(ids))
