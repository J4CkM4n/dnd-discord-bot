"""v4 narration bank: deterministic zh prose skeletons per ledger kind.

The engine PREDICTS most of the narration (scene, items, actions,
outcomes) from its own data. These skeleton sentences are the base prose
in degraded mode and the mandatory scaffold handed to the narrator LLM,
which may only add flavour — never numbers, outcomes or actions.

Variant choice is stable (no RNG): the same turn always renders the same
sentence, so replays are byte-identical.
"""


def _stable(key: str) -> int:
    return sum(ord(c) for c in str(key))


def _pick(variants: list, key: str) -> str:
    return variants[_stable(key) % len(variants)]


def render_hint(entry, game=None) -> str:
    """One prose sentence for a ledger entry (empty for silent kinds)."""
    d = entry.data or {}
    actor, target = entry.actor, d.get("target", "")
    kind = entry.kind

    if kind == "attack":
        if d.get("crit"):
            return _pick([f"{actor} 使出全力一擊，正中 {target} 的要害——漂亮的一擊！",
                          f"千鈞一髮之際，{actor} 的攻擊精準貫穿 {target} 的防禦！"],
                         actor + target)
        if d.get("hit"):
            margin = int(d.get("total", 0)) - int(d.get("ac", 0))
            if margin >= 5:
                return f"{actor} 的攻擊紮實命中 {target}，對方連退半步。"
            return _pick([f"{actor} 勉強破防，在 {target} 身上留下一道傷口。",
                          f"{target} 閃避不及，被 {actor} 擦中。"], actor + target)
        if int(d.get("d20", 2)) <= 1:
            return f"{actor} 失手了，招式大亂，門戶大開。"
        return _pick([f"{target} 擋下了 {actor} 的攻擊。",
                      f"{actor} 的攻擊被 {target} 側身避開。"], actor + target)

    if kind == "damage":
        dmg = int(d.get("dmg", 0))
        tier = "沉重的一擊" if dmg >= 6 else "一記打擊"
        return f"{target} 受到{tier}，搖搖欲墜。" if dmg >= 4 \
            else f"{target} 吃了一記輕傷。"

    if kind == "death":
        return f"{target} 應聲倒地，不再動彈。"

    if kind == "heal":
        return f"{actor} 的傷口在光芒中癒合，氣色好轉了些。"

    if kind == "item":
        return entry.text.lstrip("🎒 ")

    if kind == "scene" and game is not None:
        scene = game.world.scenes.get(d.get("scene", ""))
        if scene:
            return (f"眾人來到{scene.name}。{scene.description}"
                    + ("眼前可往「" + "」、「".join(scene.exits.values()) + "」去。"
                       if scene.exits else ""))
        return entry.text

    if kind == "check":
        if int(d.get("d20", 2)) >= 20:
            return f"{actor} 靈光一閃，表現遠超平常。"
        if d.get("ok"):
            margin = int(d.get("total", 0)) - int(d.get("dc", 0))
            return (f"{actor} 穩穩完成了挑戰。" if margin >= 4
                    else f"{actor} 驚險過關。")
        return _pick([f"{actor} 差了一步，未能如願。",
                      f"{actor} 的嘗試落了空。"], actor)

    if kind == "combat":
        if "結束" in entry.text:
            return "塵埃落定，戰鬥結束了。"
        return "殺聲乍起，戰鬥爆發！"

    if kind == "deny" and d.get("snark"):
        item = d.get("item", "那個東西")
        return _pick([
            f"{actor} 在背包裡翻了半天——「{item}」？這裡從來沒有這種東西。"
            f"眾人一臉茫然地看著 {actor}。",
            f"{actor} 神氣地伸手一掏……掏了個空。「{item}」大概存在於"
            "某個平行宇宙的背包裡。",
            f"全場安靜了三秒。{actor}，你要不要先看看自己有什麼？"
            f"（提示：不是{item}。）",
        ], actor + str(item))

    if kind == "creative":
        u = d.get("utterance") or "大膽的嘗試"
        if d.get("ok"):
            return _pick([
                f"{actor} 靈機一動——{u}——居然真的奏效了！",
                f"沒人想到這一招，但{actor}的{u}漂亮地成功了。"],
                actor + u)
        return _pick([
            f"{actor} 的{u}……創意十足，執行可惜了點。",
            f"{u}——這主意不錯，但{actor}的手不答應。"],
            actor + u)

    if kind == "cast":
        return f"{actor} 低聲詠唱，魔力在指尖凝聚。"

    if kind == "rest":
        return "隊伍稍作喘息，恢復了體力。"

    if kind == "talk" and d.get("npc"):
        npc = d.get("npc")
        u = d.get("utterance") or "交談"
        if d.get("ok"):
            return _pick([
                f"{actor} 向 {npc} 表明來意——{npc} 沉吟片刻，開口回應。",
                f"{npc} 注意到 {actor} 的誠意，願意一談。"],
                actor + npc)
        return _pick([
            f"{npc} 對 {actor} 的{u}反應冷淡，興趣缺缺。",
            f"{npc} 搖了搖頭，似乎不想多談。"], actor + npc)

    if kind == "talk":
        return entry.text

    if kind == "reveal":
        npc = d.get("npc", "對方")
        fact = d.get("fact", "")
        return (f"{npc} 壓低聲音，把「{fact}」原原本本告訴了 {actor}——"
                "這是之前沒人知道的細節。")

    if kind == "defend":
        return f"{actor} 收勢凝神，把武器橫在身前，門戶守得滴水不漏。"

    if kind == "materialize":
        item = d.get("item", "那件東西")
        return (f"那{item}靜靜躺在原地，彷彿它一直都在那裡——"
                "只是直到此刻才被目光拾起。")

    if kind == "scene_fire":
        return ("火焰竄起的瞬間，濃煙遮住了半邊天——這地方燒起來了，"
                "而且短期內不會停。")

    if kind == "beat":
        return entry.text  # beats ARE prose directives from the spine

    if kind == "observe":
        return f"{actor} 眯起眼睛盯著 {target}，不放過任何一絲破綻。"

    if kind == "skill":
        from engine.charlib import SKILL_LABEL
        sk = SKILL_LABEL.get(d.get("skill", ""), d.get("skill", "技能"))
        tgt = d.get("target", "")
        return (f"{actor} 運用{sk}"
                + (f"應付 {tgt}" if tgt else "")
                + "，全神貫注。")

    if kind == "insight":
        npc = d.get("npc", "對方")
        return (f"{actor} 盯著 {npc} 的眼神與小動作，"
                "讀出了掩飾不住的東西。")


# ---- turn context: the v3 "every turn ends with a hook" guarantee ----

def scene_header(g) -> str:
    """Context bar: where, description, NPCs, exits."""
    s = g.world.here
    lines = [f"📍 {s.name}" + (" 🔥**火海中**" if s.fire else "")]
    if s.description:
        lines.append(f"   {s.description[:80]}")
    if s.npcs:
        lines.append(f"   👥 {'、'.join(n['name'] for n in s.npcs)}")
    if s.exits:
        lines.append(f"   🚪 {'、'.join(s.exits.values())}")
    return "\n".join(lines)


def suggested_actions(g, actor: str = "") -> list[str]:
    """3-5 concrete next-step options, scene-aware, story-driving."""
    s = g.world.here
    opts = []
    # downed companions are the top priority — show the way to save them
    for n, e in g.party.items():
        if isinstance(e, dict) and int(e.get("hp_now", 1) or 0) <= 0:
            opts.append(f"🩹 **救 {n}**：`/explore 用醫藥救{n}`"
                        "（DC 10）或 `/explore 餵{n}治療藥水`")
    # NPCs first (they're the story drivers)
    for n in s.npcs:
        knows = n.get("knows", [])
        seen = n.get("disclosed", [])
        disp = n.get("disposition", "neutral")
        if knows:
            if len(seen) >= len(knows):
                opts.append(f"和 **{n['name']}** 閒聊（情報已全部問出）")
            else:
                opts.append(f"向 **{n['name']}** 打聽"
                            f"（已問出 {len(seen)}/{len(knows)}）")
        elif disp == "hostile":
            opts.append(f"面對 **{n['name']}**（敵對）")
        else:
            opts.append(f"和 **{n['name']}** 交談")
        if disp not in ("friendly", "allied"):
            opts.append(f"👁 用 **洞察** 看穿 {n['name']} 的真實態度")
    # exits with context
    for sid, label in list(s.exits.items())[:2]:
        dest = g.world.scenes.get(sid)
        hint = f"（{dest.description[:20]}…）" if dest and dest.description else ""
        opts.append(f"前往「{label}」{hint}")
    # search
    if s.hidden_items:
        opts.append("🔍 搜索這裡（有隱藏物品）")
    else:
        opts.append("🔍 搜索這裡")
    # combat or rest
    if g.combat.active:
        waiting = [n for n in g.party
                   if g.alive(n) and n not in g.combat.acted]
        opts.append("⚔️ `/attack` `/use` `/skill`（待行動："
                    + ('、'.join(waiting) or "敵方結算中") + "）")
    else:
        hurt = any(e.get("hp_now", 99) < e.get("hp_max", 1) // 2
                   for e in g.party.values() if isinstance(e, dict))
        if hurt:
            opts.append("🌿 休息恢復（`/explore 我們休息`）")
        # creative prompt — concrete, because the engine HONORS these
        opts.append("✨ 創意行動真的有用：掟嘢／放火／撒鹽搞亂敵人"
                    "（下擊優勢）／臨時掩體（`/explore` 描述）")
    return opts[:5]


def render_turn_context(g, actor: str = "") -> str:
    """Full guidance block appended after engine output:
    scene header + party HP + combat status + suggested actions.
    This replaces v3's constitutional 'every turn ends with a hook'."""
    lines = [scene_header(g)]
    hp_line = " · ".join(f"{n} {e['hp_now']}/{e['hp_max']}HP"
                         for n, e in g.party.items()
                         if isinstance(e, dict))
    if hp_line:
        lines.append(f"❤️ {hp_line}")
    if g.combat.active:
        waiting = [n for n in g.party
                   if g.alive(n) and n not in g.combat.acted]
        lines.append(f"⚔️ 戰鬥 R{g.combat.round} — "
                     f"待行動：{'、'.join(waiting) or '（敵方回合結算中）'}")
        for n, f in g.enemies.items():
            if not f.dead:
                lines.append(f"   👹 {n} {f.hp}/{f.hp_max}HP AC{f.ac}")
    opts = suggested_actions(g, actor)
    if opts:
        lines.append("")
        lines.append("**👉 你可以：**")
        for o in opts:
            lines.append(f"  · {o}")
    return "\n".join(lines)

    return ""  # deny/pass/auto/meta stay silent in prose
