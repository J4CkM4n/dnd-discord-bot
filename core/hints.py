"""Public-only, deterministic stall detection and guidance."""
import json


def progress_key(game, plot):
    scene = game.world.here
    return json.dumps({"scene": scene.id, "exits": scene.exits,
        "inventory": game.inventory, "items": scene.ground_items, "fire": scene.fire,
        "npcs": [(n["name"], n.get("disposition"), n.get("disclosed", [])) for n in scene.npcs],
        "party": [(n, p.get("hp_now"), p.get("conds")) for n, p in game.party.items()],
        "plot": plot, "aid": getattr(game, "_aid", {}), "dodge": getattr(game, "_dodge", {})},
        ensure_ascii=False, sort_keys=True)


def public_hint(game):
    pending = getattr(game, "_pending_check", None)
    if pending:
        options = [f"由 {pending['actor']} 的玩家用 `/roll d20` 完成眼前的檢定"]
    elif game.combat.active:
        options = ["用 `/attack` 選擇眼前的敵人", "用 `/defend` 找好掩護"]
    else:
        options = [f"用 `/explore 前往{label}` 試試這條路" for label in game.world.here.exits.values()][:1]
        options += [f"用 `/explore 和{n['name']}交談` 問問眼前的人" for n in game.world.here.npcs][:1]
        if not options:
            options = ["用 `/explore 搜索` 留意周圍", "用 `/scene` 查看眼前的環境"]
    return "💡 不妨換個角度試試：" + "；或者".join(options[:2]) + "。"


def track_attempt(state, game, config, *, before, plot, eligible, wants_help=False):
    """Counters live outside canonical facts; rejected attempts also count."""
    stalled = state.setdefault("stalls", {"scene": game.world.current, "count": 0,
                                         "attempts": 0, "last_hint": -config.cooldown})
    progressed = before != progress_key(game, plot)
    if progressed or stalled["scene"] != game.world.current:
        stalled.update(scene=game.world.current, count=0)
    if eligible:
        stalled["attempts"] += 1
        stalled["count"] = 0 if progressed else stalled["count"] + 1
    due = eligible and stalled["count"] >= config.stalled_attempts \
        and stalled["attempts"] - stalled["last_hint"] >= config.cooldown
    if wants_help or due:
        stalled["last_hint"] = stalled["attempts"]
        stalled["guidance"] = public_hint(game)
        return stalled["guidance"]
    return ""
