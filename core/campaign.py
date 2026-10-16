"""Authored starter milestones, public objectives and idempotent save recovery."""
from collections import deque

from .world import Scene, Enemy

NECKLACE = "海妖女王的項鍊"


def is_starter(g):
    return all(sid in g.world.scenes and g.world.scenes[sid].name == name
               for sid, name in (("dock", "霧錨鎮碼頭"), ("tavern", "破舊酒館")))


def defeated(g, name):
    return any(e.kind == "death" and e.data.get("target") == name for e in g.ledger.entries) or any(
        f.name == name and (f.dead or f.hp <= 0) for foes in g.encounters.values() for f in foes)


def sync_starter(g):
    """Only recognizable starter worlds; never replace player/generated content.

    Paths unlock on resolved milestones. Repeated calls/restarts do not refill
    loot, respawn enemies, repeat disclosures or move the party.
    """
    if not is_starter(g):
        return []
    progress = getattr(g, "_starter_progress", None)
    lines = []
    tavern_clear = all(defeated(g, n) for n in ("哥布林①", "哥布林②"))
    if not progress and not tavern_clear:
        return lines
    if progress is None:
        progress = g._starter_progress = {}
    if tavern_clear and not progress.get("tavern"):
        progress["tavern"] = True
        lines.append("酒館的哥布林已清除；酒窖門後的通道現在可以通行。")
    if not progress.get("tavern"):
        return lines
    tavern, dock = g.world.scenes["tavern"], g.world.scenes["dock"]
    if tavern.description == "哥布林佔據的酒館，桌椅翻倒，角落有可疑的酒窖門。":
        tavern.description = "哥布林已被擊退，翻倒的桌椅間留著戰鬥痕跡；角落的酒窖門已打開。"
    authored = [
        Scene("cellar", "酒館酒窖", "潮濕酒窖通往走私者的海洞。巴鐸的護衛擋著通路，身後有航路圖。",
              exits={"tavern": "破舊酒館"}, search_dc=10),
        Scene("sea", "海上·甲板", "小艇泊在海洞外；航路圖標出退潮時通往海底祭壇的石道。",
              exits={"cellar": "酒館酒窖", "altar": "海底祭壇"}),
        Scene("altar", "海底祭壇", "退潮露出通往祭壇的石階。女王的族人困在封印後，泉水就在祭壇深處。",
              exits={"sea": "海上·甲板"}),
    ]
    for scene in authored:
        if scene.id not in g.world.scenes:
            g.world.add_scene(scene)
    tavern.exits.setdefault("cellar", g.world.scenes["cellar"].name)
    # The authored guard is seeded once, including campaigns that already
    # contain a generated cellar or historical guard death.
    if not progress.get("guard_seeded"):
        if not defeated(g, "巴鐸的護衛") and not any(f.name == "巴鐸的護衛" for foes in g.encounters.values() for f in foes):
            g.encounters.setdefault("cellar", []).append(Enemy.make("巴鐸的護衛", 8, 12, 2, "1d4", loot=["潮汐航路圖"]))
        progress["guard_seeded"] = True
    for npc in dock.npcs:
        if npc.get("name") == "老船長":
            fact = "酒館的哥布林已被你們清除。酒窖通往走私海洞；巴鐸的護衛守著航路圖。"
            if progress.get("guard") or defeated(g, "巴鐸的護衛"):
                fact = "酒館和海洞的護衛已被你們擊敗。沿海上·甲板的退潮石道可到海底祭壇，向巴鐸取回項鍊。"
            if progress.get("completed"):
                fact = "項鍊已交回女王，祭壇封印解開，族人獲釋；你們已完成約定。"
            obsolete = {"破舊酒館被哥布林佔據，前往時要小心。"}
            npc["knows"] = [fact] + [f for f in npc.get("knows", []) if f != fact and f not in obsolete
                                   and not f.startswith(("酒館的哥布林已被你們清除。", "酒館和海洞的護衛已被你們擊敗。"))]
    if not progress.get("queen_seeded"):
        known_queen = any(n["name"] == "海妖女王" for s in g.world.scenes.values() for n in s.npcs) or any(
            f.name == "海妖女王" for foes in g.encounters.values() for f in foes)
        if not known_queen and not defeated(g, "海妖女王"):
            dock.npcs.append({"name": "海妖女王", "desc": "隔著潮水顯現的身影", "disposition": "friendly",
                "knows": ["巴鐸偷走了我的項鍊。找回它，帶回碼頭，我便能解開祭壇的封印，釋放族人並指明生命泉水。"]})
        progress["queen_seeded"] = True
    if defeated(g, "巴鐸的護衛"):
        if not progress.get("guard"):
            progress["guard"] = True
            lines.append("巴鐸的護衛已倒下。航路通往海上·甲板及海底祭壇；巴鐸退守祭壇。")
        g.world.scenes["cellar"].exits.setdefault("sea", g.world.scenes["sea"].name)
        if g.world.scenes["cellar"].description.startswith("潮濕酒窖通往走私者的海洞。巴鐸的護衛擋"):
            g.world.scenes["cellar"].description = "護衛已倒下。海洞外的小艇旁，退潮石道通往海底祭壇。"
        # An old save may have fired the old missing-location beat already.
        # Recover its promised NPC only if he has never appeared or died.
        known_bador = any(e.data.get("npc") == "巴鐸" or e.data.get("target") == "巴鐸" for e in g.ledger.entries)
        if not progress.get("bador_seeded"):
            if not known_bador and not defeated(g, "巴鐸") and not any(n["name"] == "巴鐸" for s in g.world.scenes.values() for n in s.npcs):
                g.world.scenes["altar"].npcs.append({"name": "巴鐸", "desc": "失去護衛的商人", "disposition": "wary",
                    "knows": ["項鍊是我偷的。我願意交還，只求你們放我離開。"], "loot": [NECKLACE]})
            progress["bador_seeded"] = True
    # A successful conversation is a real, non-combat way to recover the quest
    # object. The offer has to be revealed by the engine before this transfer.
    if not progress.get("necklace_released"):
        negotiated = next((e for e in reversed(g.ledger.entries) if e.kind == "reveal" and e.data.get("npc") == "巴鐸"), None)
        npc = next((n for n in g.world.here.npcs if n["name"] == "巴鐸"), None)
        if negotiated and "我願意交還" in negotiated.data.get("fact", "") and npc and g.world.current == "altar" and NECKLACE in npc.get("loot", []):
            npc["loot"].remove(NECKLACE)
            g.world.here.ground_items.append((NECKLACE, 1))
            progress["necklace_released"] = True
            lines.append("巴鐸依照交談中的承諾，把海妖女王的項鍊放在地上；可以拾起。")
    if any(n == NECKLACE and q > 0 for stacks in g.inventory.values() for n, q in stacks) and not progress.get("necklace"):
        progress["necklace"] = True
        lines.append("已找回女王的項鍊；把它帶回霧錨鎮碼頭，完成約定。")
    for line in lines:
        g.ledger.add("campaign", "milestone", line)
    return ["📜 " + line for line in lines]


def objective(g):
    """Only unlocked, public objectives; never expose a future room or secret."""
    if not is_starter(g):
        return "", ""
    progress = getattr(g, "_starter_progress", None) or {}
    if progress.get("completed"):
        return "與海妖女王的約定已完成，族人獲釋；可自由探索或與船長回顧旅程。", "dock"
    if progress.get("necklace"):
        return "把海妖女王的項鍊帶回霧錨鎮碼頭。", "dock"
    if progress.get("guard"):
        return "沿海上·甲板的退潮石道前往海底祭壇，與巴鐸交談或擊敗他，取回項鍊。", "altar"
    if progress.get("tavern"):
        return "進入酒館酒窖，處理巴鐸的護衛，取得通往海洞的航路。", "cellar"
    return "前往破舊酒館，處理佔據酒館的哥布林，調查角落的酒窖門。", "tavern"


def objective_hint(g):
    text, target = objective(g)
    if not text:
        return ""
    prefix = "📜 目前目標：" + text
    if g.combat.active:
        return prefix + " 先處理眼前戰鬥：`/attack`、`/defend`、`/flee`。"
    if target == g.world.current:
        if target == "altar":
            if any(n == NECKLACE and q > 0 for n, q in g.world.here.ground_items):
                return prefix + " 下一步：`/explore 拾起海妖女王的項鍊`。"
            return prefix + " 下一步：`/explore 說 巴鐸，我們來取回項鍊`。"
        return prefix
    queue = deque([(g.world.current, None)])
    seen = {g.world.current}
    while queue:
        sid, first = queue.popleft()
        for dest, label in g.world.scenes[sid].exits.items():
            if dest not in g.world.scenes or dest in seen:
                continue
            step = first or label
            if dest == target:
                return prefix + f" 下一步：前往「{step}」（`/explore 前往{step}`）。"
            seen.add(dest)
            queue.append((dest, step))
    return prefix + " 可用 `/scene` 查看目前出口。"
