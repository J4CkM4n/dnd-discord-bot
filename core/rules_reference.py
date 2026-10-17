"""Read-only SRD lookup with an offline reference for implemented game rules."""
from contextlib import closing
import logging
import os
from pathlib import Path
import sqlite3

log = logging.getLogger("dnd-bot")

_SRD_ZH = {
    "哥布林": "Goblin", "地精": "Goblin", "狗頭人": "Kobold",
    "龍": "Dragon", "幼龍": "Dragon", "獸人": "Orc",
    "骷髏": "Skeleton", "殭屍": "Zombie", "蜘蛛": "Spider",
    "魚人": "Merfolk", "史萊姆": "Slime", "巨魔": "Troll",
    "巨人": "Giant", "精靈": "Elf", "矮人": "Dwarf",
    "半身人": "Halfling", "法師": "Wizard", "牧師": "Cleric",
    "戰士": "Fighter", "盜賊": "Rogue", "遊俠": "Ranger",
    "術士": "Warlock", "野蠻人": "Barbarian", "吟遊詩人": "Bard",
    "德魯伊": "Druid", "聖武士": "Paladin",
    "火球": "Fireball", "火球術": "Fireball",
    "治療": "Healing", "治療藥水": "Potion of Healing",
    "藥水": "Potion", "魔法飛彈": "Magic Missile",
    "閃電": "Lightning", "隕石": "Meteor",
    "先攻": "Initiative", "豁免": "Saving Throw",
    "擅長": "Proficiency", "休息": "Rest",
    "死亡豁免": "Death Saving Throw",
    "掩蔽": "Cover", "擒抱": "Grapple", "衝刺": "Dash",
    "閃避": "Dodge", "協助": "Help", "撤退": "Disengage",
}


def _game_entries():
    from engine.charlib import SKILL_ABILITY, SKILL_LABEL
    from engine.moves import class_moves, move_spell_level
    from .cli import build_starter_game
    rows = [
        ("攻擊 Attack", "戰鬥", "用 `/attack` 選擇目標和招式。未指定招式時使用職業的第一個可用招式；每回合每人可行動一次。"),
        ("防禦 Dodge", "戰鬥", "用 `/defend` 防守，敵人下一次攻擊該角色有劣勢。"),
        ("觀察 Help", "戰鬥", "用 `/observe` 觀察敵人破綻，下一位攻擊該敵人的隊友有優勢。"),
        ("治療藥水 Potion of Healing", "物品", "用 `/use` 選擇治療藥水與隊友，可救治倒地隊友；先檢查 `/inventory`。"),
        ("休息 Rest", "恢復", "用 `/rest` 選擇短休或長休。短休恢復部分生命及契約法術格；長休恢復生命及法術格。倒地角色需要先救治。"),
    ]
    for skill, ability in SKILL_ABILITY.items():
        rows.append((f"{SKILL_LABEL[skill]} {skill}", "技能",
                     f"用 `/skill` 選擇 {SKILL_LABEL[skill]} 與對象，使用 {ability} 檢定；若需擲骰，依提示用 `/roll d20` 結算。"))
    for occupation, moves in class_moves.items():
        rows.append((occupation, "職業", "可用招式：" + "、".join(m[0] for m in moves) + "。用 `/inventory` 查看目前已解鎖的招式。"))
        for name, ability, damage, _ in moves:
            cost = move_spell_level.get(name, 0)
            rows.append((name, "招式", f"{occupation} 招式，使用 {ability}，傷害 {damage}。"
                         + (f"消耗 {cost} 環法術格。" if cost else "不消耗法術格。")))
    for foes in build_starter_game().encounters.values():
        for foe in foes:
            rows.append((foe.name + " Goblin", "起始冒險怪物", f"HP {foe.hp_max}，AC {foe.ac}，攻擊修正 {foe.attack_bonus:+d}，傷害 {foe.dmg}；實際數值可能隨隊伍等級調整。"))
    return rows


def rules_text(query: str, db_path=None) -> str:
    q = (query or "").strip()
    if not q:
        return "❓ 查詢什麼？例：`/rules 火焰箭`、`/rules 哥布林`、`/rules 休息`"
    terms = list(dict.fromkeys([q, *[en for zh, en in _SRD_ZH.items() if zh in q]]))
    path = Path(db_path or os.environ.get("RULES_DB") or Path(os.environ.get("DATA_DIR", "data")) / "rules.db")
    rows = []
    available = False
    try:
        # as_uri escapes Windows drive letters, spaces, '?' and '#' correctly.
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2.0)) as conn:
            available = True
            for term in terms:
                rows = conn.execute("SELECT title, kind, text FROM chunks WHERE title LIKE ? ORDER BY length(title) LIMIT 3",
                                    (f"%{term}%",)).fetchall()
                if rows:
                    break
            if not rows:
                rows = conn.execute("SELECT title, kind, text FROM chunks WHERE text LIKE ? LIMIT 3", (f"%{q}%",)).fetchall()
    except (sqlite3.Error, OSError, ValueError):
        available = False
        log.warning("SRD reference unavailable | path=%s", path)
    heading = f"📚 **SRD 查詢：{q}**"
    if not rows:
        rows = [row for row in _game_entries() if any(t.casefold() in row[0].casefold() for t in terms)][:3]
        heading = f"📚 **遊戲規則：{q}**（本遊戲實作）"
        if not available:
            heading += "\n完整 SRD 尚未載入，以下提供目前可用的遊戲規則。"
    if not rows:
        return (f"🔍 找不到「{q}」的條目。" + ("完整 SRD 尚未載入，請通知管理員匯入規則資料。" if not available else "可試英文名稱。")
                + "可先查 `/rules 攻擊`、`/rules 休息` 或 `/rules 治療藥水`。")
    return (heading + "\n" + "\n".join(f"\n**{title}**（{kind}）\n{' '.join(body.split())[:280]}" for title, kind, body in rows))[:1900]
