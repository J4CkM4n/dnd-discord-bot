"""v5 Pillar B: the plot spine — clocks + trigger-fired beats.

Pre-generate the SITUATION, improvise the scenes: clocks count down,
triggers wait on engine facts (turn / scene / item / enemy death), and
when one fires the ENGINE executes a typed beat (ledger kind=beat);
the narrator only renders it. Beats come to the players — no corridors.

plot.json is the campaign data; fired state persists with the game.
"""

import json
import os


def load_plot() -> dict:
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "plot.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def new_state(plot: dict) -> dict:
    return {
        "clocks": {c["id"]: int(c.get("start", 0))
                   for c in plot.get("clocks", [])},
        "fired": [],
        "last_turn": 0,
        "ended": None,
    }


def _scene_by(g, key: str):
    """Scene by id or fuzzy name."""
    key = (key or "").strip()
    if key in g.world.scenes:
        return g.world.scenes[key]
    for s in g.world.scenes.values():
        if key and (key in s.name or s.name in key):
            return s
    return None


def _cond_met(g, cond: dict) -> bool:
    if "all" in cond:
        return all(_cond_met(g, c) for c in cond["all"])
    if "turn_gte" in cond:
        return g.ledger.turn >= int(cond["turn_gte"])
    if "scene" in cond:
        s = _scene_by(g, cond["scene"])
        return s is not None and g.world.current == s.id
    if "item" in cond:
        want = cond["item"]
        return any(want in n or n in want
                   for stacks in g.inventory.values()
                   for n, _ in stacks)
    if "enemy_dead" in cond:
        foe = g.enemies.get(cond["enemy_dead"])
        return bool(foe and foe.dead) or bool(
            not foe and any(cond["enemy_dead"] == (e.data or {}).get("target")
                            for e in g.ledger.entries
                            if e.kind == "death"))
    if "npc_gone" in cond:
        return not any(cond["npc_gone"] == n["name"]
                       for s in g.world.scenes.values()
                       for n in s.npcs)
    return False


def _apply_effect(g, eff: dict, out: list):
    if "npc_move" in eff:
        spec = eff["npc_move"]
        target = _scene_by(g, spec.get("to_scene", ""))
        for s in g.world.scenes.values():
            hit = [n for n in s.npcs if n["name"] == spec.get("npc")]
            if hit and target is not None:
                s.npcs.remove(hit[0])
                target.npcs.append(hit[0])
                out.append(f"👥 {spec['npc']} 前往了 {target.name}")
                return
    if "npc_disposition" in eff:
        spec = eff["npc_disposition"]
        for s in g.world.scenes.values():
            for n in s.npcs:
                if n["name"] == spec.get("npc"):
                    n["disposition"] = spec.get("to", "neutral")
                    return
    if "spawn" in eff:
        spec = eff["spawn"]
        s = _scene_by(g, spec.get("scene", ""))
        e = spec.get("enemy", {})
        if s is not None and e:
            if e.get("cr"):  # cite a CR band instead of raw stats
                from engine.cr import make_by_cr
                foe = make_by_cr(e.get("name", "敵人"), e["cr"])
            else:
                from .world import Enemy
                foe = Enemy.make(
                    e.get("name", "敵人"), int(e.get("hp", 8)),
                    int(e.get("ac", 13)), int(e.get("attack_bonus", 3)),
                    e.get("dmg", "1d6"))
            foe.loot = list(e.get("loot", []))   # authored quest drops
            from engine.cr import scale_for_party
            scale_for_party(foe, g.party)
            g.encounters.setdefault(s.id, []).append(foe)
            out.append(f"👀 {foe.name} 出沒於 {s.name}……")
    if "clock" in eff:
        pass  # handled by the caller (needs the state dict)
    if "npc_spawn" in eff:
        spec = eff["npc_spawn"]
        s = _scene_by(g, spec.get("scene", ""))
        name = spec.get("name", "")
        if s is not None and name and not any(
                n["name"] == name for sc in g.world.scenes.values()
                for n in sc.npcs):
            s.npcs.append({
                "name": name,
                "desc": spec.get("desc", "陌生的身影"),
                "disposition": spec.get("disposition", "wary"),
                "knows": list(spec.get("knows", [])),
                "loot": list(spec.get("loot", []))})
            out.append(f"👥 {name} 出現在 {s.name}")
        return
    if "cond" in eff:
        spec = eff["cond"]
        who = spec.get("who", "")
        names = [who] if who in g.party else list(g.party)
        for n in names:
            g.add_cond(n, spec.get("name", "poisoned"),
                       spec.get("rounds"))
        out.append(f"☠️ {'、'.join(names)} 受到{spec.get('name', 'poisoned')}"
                   f"（{spec.get('rounds', '持續')} 回合）")
        return
    if "inspire" in eff:
        who = (eff["inspire"] or {}).get("char", "")
        names = [who] if who in g.party else list(g.party)
        for n in names:
            g.add_cond(n, "inspired", None)
        out.append(f"✨ {'、'.join(names)} 獲得靈感（下一次攻擊有優勢）")
        return


def tick(g, plot: dict, state: dict) -> tuple[list[str], bool]:
    """Once per ledger turn: advance clocks, evaluate triggers, fire
    beats (once ever). Returns (engine lines, mutated)."""
    out: list[str] = []
    changed = False
    if state.get("ended"):
        return out, False
    turn = g.ledger.turn
    if turn != state.get("last_turn"):
        state["last_turn"] = turn
        for c in plot.get("clocks", []):
            cid = c["id"]
            state["clocks"][cid] = state["clocks"].get(
                cid, int(c.get("start", 0))) + 1
            changed = True
    # clock expiry -> its beat
    for c in plot.get("clocks", []):
        cid = c["id"]
        if (cid in state["clocks"]
                and state["clocks"][cid] >= int(c.get("max", 10**9))
                and c.get("on_expire")
                and c["on_expire"] not in state["fired"]):
            state["fired"].append(c["on_expire"])
            _fire(g, plot, state, c["on_expire"], out)
            changed = True
    # condition triggers
    for t in plot.get("triggers", []):
        if (t.get("beat") not in state["fired"]
                and _cond_met(g, t.get("when", {}))):
            state["fired"].append(t["beat"])
            _fire(g, plot, state, t["beat"], out)
            changed = True
    return out, changed


def _fire(g, plot: dict, state: dict, beat_id: str, out: list):
    beat = (plot.get("beats") or {}).get(beat_id) or {}
    text = beat.get("text", "")
    effs = beat.get("effects") or []
    sub: list[str] = []
    for eff in effs:
        if "clock" in eff:
            spec = eff["clock"]
            cid = spec.get("id")
            if cid in state["clocks"]:
                state["clocks"][cid] = int(spec.get("set",
                                     state["clocks"][cid]))
            else:
                state["clocks"][cid] = int(spec.get("set", 0))
        else:
            _apply_effect(g, eff, sub)
    line = "🎬 " + text + ("".join(f"\n{s}" for s in sub) if sub else "")
    g.ledger.add("plot", "beat", text + ("；" + "；".join(sub) if sub else ""),
                 beat=beat_id, ending=beat.get("ending"))
    out.append(line)
    if beat.get("ending"):
        state["ended"] = beat["ending"]
        out.append(f"🏁 **劇終：{beat['ending']}**")
