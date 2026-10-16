"""Public-only, deterministic stall detection and guidance."""
import json


def progress_key(game, plot):
    # Walking in circles, collecting decorative junk and advancing clocks
    # must not disguise a stalled objective.
    from .director import classify
    return json.dumps({"milestones": getattr(game, "_starter_progress", None),
        "inventory": {ch: [(n, q) for n, q in stacks if classify(n) != "prop"] for ch, stacks in game.inventory.items()},
        "npcs": [(n["name"], n.get("disclosed", [])) for s in game.world.scenes.values() for n in s.npcs],
        "deaths": [e.data.get("target") for e in game.ledger.entries if e.kind == "death"],
        "checks": [e.event_id for e in game.ledger.entries if e.kind == "check"],
        "plot": {k: plot.get(k) for k in ("fired", "ended")}},
        ensure_ascii=False, sort_keys=True)


def public_hint(game):
    pending = getattr(game, "_pending_check", None)
    if pending:
        options = [f"由 {pending['actor']} 的玩家用 `/roll d20` 完成眼前的檢定"]
    elif game.combat.active:
        options = ["用 `/attack` 選擇眼前的敵人", "用 `/defend` 找好掩護"]
    else:
        from .campaign import objective_hint
        if hint := objective_hint(game):
            return "💡 " + hint
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
    if progressed:
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
