"""Discord adapter — v4 engine-driven (11 commands).

/explore  — freeform non-combat action (digest → engine → narrate)
/attack /use /skill /defend /flee /observe — one typed command per combat action
"""

import asyncio
import logging
import os
import re

import discord
from discord import app_commands

from engine.dice import quick_roll

log = logging.getLogger("discord_adapter")


def _clip(s: str, limit: int = 1900) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _split(text: str, limit: int = 1900):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


HELP_TEXT = """🎲 **v6 引擎指令 / Commands**（玩家 16 · 管理 10）

**玩家 / Player**
`/pc <名字> <職業>` — 建立角色（屬性由系統公正擲骰）
`/explore <text>` — 探索／對話／移動（自由描述，引擎判定）
　· 範例：`/explore 依思詢問船長關於巴鐸的線索`
`/confirm` — 確認待確認的行動

**戰鬥指令 / Combat**（各指令有自己的自動完成清單）
`/attack [目標] [招式]` — 攻擊（目標清單帶敵人 HP；招式按角色列出）
`/use [物品] [隊友]` — 使用物品／餵隊友（**可救倒地隊友**）
`/skill [技能] [對象]` — 技能（18 項，★＝熟練；對象含敵人/NPC/隊友）
`/defend` — 防禦（敵人攻擊你有劣勢）
　· 戰鬥中判定訊息附一排快捷按鈕：🛡防禦 👁觀察 🏃撤退 ⏭讓路
`/flee` — 撤退（全隊嘗試脫離戰鬥）
`/observe [敵人]` — 找破綻（下一擊有優勢）

**18 項技能 / Skills**（`/inventory` 查看熟練項）
`/explore` 直接描述即可：匿埋（潛行）、嚇佢（恐嚇）、睇穿佢（洞察）、
包紮（醫藥）、爬牆（運動）…引擎自動選技能＋熟練加值擲骰。
技能效果：潛行→下擊有優勢；洞察→看穿 NPC 真實態度；醫藥→救醒倒地隊友；
察覺/調查→搜出隱藏物；社交（說服/欺瞞/恐嚇/表演）→改變 NPC 態度。

`/roll [expr]` — 擲骰（`/roll d20` 會結算待定檢定並自動續寫劇情）
`/inventory` — 物品欄＋法術格＋18 技能（★熟練）＋招式
`/give <item> [to]` — 給隊友；不填 to＝放在地上（可拾回）
`/status` — 隊伍 HP／法術格／場景／戰鬥／📜已知情報
`/rules <關鍵字>` — 查 SRD（火球術/哥布林/豁免…即時無 LLM）
`/continue` — 未完成的敘事先逐字續寫；否則推進遊戲
`/help` — 本說明

**管理員 / Admin**
`/explore-admin <text> <char>` — 以任意角色探索
`/attack-admin` `/use-admin` `/skill-admin` `/defend-admin` `/flee-admin`
`/observe-admin` — 以任意角色執行對應戰鬥行為（自動完成同玩家版）
`/roll-admin <expr> <char>` — 代擲（d20 會結算待定檢定）
`/give-admin <char> <item> [qty]` — 給物品
`/inspire-admin <char>` — 給靈感（一次性優勢）

**頻道內關鍵字**（直接打字，即時回應、無 LLM）：
`status` `inv` `moves` `scene`

**桌邊聊天**：直接打字＝玩家間對話（引擎記錄但不回應）
**角色限制**：只能控制自己擁有的角色（引擎強制）
**NPC 對話**：NPC 只會透露其已知的事實（問過的在 `/status` 已知情報）"""




class CombatPanel(discord.ui.View):
    """v6: one-tap combat basics, attached to the verdict message while a
    battle lasts. Zero-arg actions only — arg-ed actions stay typed
    commands（buttons cannot collect input）. Persistent: survives restarts
    via custom_ids; stale panels answer gracefully when combat is over."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    async def _act(self, interaction: discord.Interaction, text: str):
        v4svc = self.bot._v4_service(str(interaction.channel_id))
        g = v4svc.game
        if not g.combat.active:
            await interaction.response.send_message(
                "🏁 戰鬥已結束——這排按鈕過期了。")
            return
        char = next((n for n, v in g.party.items()
                     if isinstance(v, dict)
                     and str(v.get("owner_id", ""))
                     == str(interaction.user.id)), "")
        if not char:
            await interaction.response.send_message(
                "❓ 找不到你的角色——先用 `/pc` 建立。")
            return
        if char in g.combat.acted:
            waiting = [n for n in g.party
                       if g.alive(n) and n not in g.combat.acted]
            await interaction.response.send_message(
                f"⏳ **{char}** 本回合已行動"
                + (f"——等待 {'、'.join(waiting)}" if waiting else ""),
                ephemeral=True)
            return
        await self.bot._v4_turn(interaction, f"{char} {text}",
                                structured=True)

    @discord.ui.button(label="🛡 防禦", custom_id="cp:defend",
                       style=discord.ButtonStyle.secondary)
    async def defend(self, interaction: discord.Interaction,
                     button: discord.ui.Button):
        await self._act(interaction, "防禦")

    @discord.ui.button(label="👁 觀察", custom_id="cp:observe",
                       style=discord.ButtonStyle.secondary)
    async def observe(self, interaction: discord.Interaction,
                      button: discord.ui.Button):
        await self._act(interaction, "觀察")

    @discord.ui.button(label="🏃 撤退", custom_id="cp:flee",
                       style=discord.ButtonStyle.danger)
    async def flee(self, interaction: discord.Interaction,
                   button: discord.ui.Button):
        await self._act(interaction, "撤退")

    @discord.ui.button(label="⏭ 讓路", custom_id="cp:pass",
                       style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction,
                   button: discord.ui.Button):
        await self._act(interaction, "等待")


class DiscordBot(discord.Client):
    def __init__(self, message_content: bool = True, *, agent_config=None, gateway=None):
        from core.agent_config import load_agent_config
        from core.model_gateway import ModelGateway
        self.agent_config = agent_config or (gateway.config if gateway else load_agent_config())
        self.model_gateway = gateway or ModelGateway(self.agent_config)
        self.model_gateway.log_configuration()
        intents = discord.Intents.default()
        intents.message_content = message_content
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._v4_games: dict[str, object] = {}  # channel_id → V4Service

        @self.tree.error
        async def on_app_command_error(interaction, error):
            # a crashed command must never leave the interaction hanging
            # (「該申請未受回應」) — players always get an answer
            log.exception("command failed", exc_info=error)
            cause = getattr(error, "__cause__", None) or error
            msg = _clip(f"⚠️ 指令出錯：{cause}")
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg)
                else:
                    await interaction.response.send_message(msg)
            except discord.HTTPException:
                pass

        self._register_commands()

    async def setup_hook(self):
        self.add_view(CombatPanel(self))   # persistent combat buttons

    async def on_ready(self):
        log.info("logged in as %s (guilds=%d)", self.user, len(self.guilds))
        for g in self.guilds:
            try:
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
            except Exception as e:
                log.warning("sync failed for %s: %s", g.id, e)

    def _v4_channels(self) -> set:
        """All channel IDs routed to the v4 engine (comma-separated env)."""
        raw = (os.environ.get("V4_CHANNEL_IDS")
               or os.environ.get("V4_CHANNEL_ID")  # legacy single-channel
               or "").strip()
        return {c.strip() for c in raw.split(",") if c.strip()}

    def _v4_service(self, channel_id: str = ""):
        """Per-channel V4Service — each table gets its own game state."""
        cid = str(channel_id) if channel_id else "default"
        if cid not in self._v4_games:
            from core.service import V4Service
            data_dir = os.environ.get("DATA_DIR", "data")
            self._v4_games[cid] = V4Service(
                data_dir, channel_id=cid, agent_config=self.agent_config, gateway=self.model_gateway)
        return self._v4_games[cid]

    def _in_v4(self, interaction_or_channel) -> bool:
        cid = str(getattr(interaction_or_channel, "channel_id", None)
                  or getattr(interaction_or_channel, "id", ""))
        return cid in self._v4_channels()

    # ------------------------------------------------------------------
    #  command registration
    # ------------------------------------------------------------------

    def _register_commands(self):
        svc = self  # closure alias

        # ---- /pc (character creation) ----

        CLASS_KEYS = ["Barbarian", "Bard", "Cleric", "Druid", "Fighter",
                      "Monk", "Paladin", "Ranger", "Rogue", "Sorcerer",
                      "Warlock", "Wizard"]
        CLASS_ZH = {"Barbarian": "野蠻人", "Bard": "吟遊詩人",
                    "Cleric": "牧師", "Druid": "德魯伊", "Fighter": "戰士",
                    "Monk": "武僧", "Paladin": "聖武士", "Ranger": "遊俠",
                    "Rogue": "盜賊", "Sorcerer": "術士",
                    "Warlock": "魔契師", "Wizard": "法師"}

        @self.tree.command(name="pc",
                           description="建立角色 / create character")
        @app_commands.describe(
            name="角色名",
            occupation="職業（下拉選擇）",
            remove="填 remove confirm 以刪除角色（僅限擁有者）")
        @app_commands.choices(occupation=[
            app_commands.Choice(name=f"{CLASS_ZH[c]} {c}", value=c)
            for c in CLASS_KEYS])
        async def pc_cmd(interaction: discord.Interaction, name: str,
                         occupation: app_commands.Choice[str],
                         remove: str = ""):
            await interaction.response.defer(thinking=True)
            service = svc._v4_service(str(interaction.channel_id))
            if remove.strip().lower().startswith("remove"):
                text = await service.remove_character(name, str(interaction.user.id), remove)
            else:
                text = await service.create_character(
                    name, occupation.value if occupation else "Fighter",
                    str(interaction.user.id), interaction.user.display_name)
            await interaction.followup.send(_clip(text))

        # ---- autocomplete helpers ----

        async def skill_ac(interaction, current: str):
            """The 18 skills — proficient (class core) ones starred first."""
            g = svc._v4_service(str(interaction.channel_id)).game
            char = _filled_param(interaction, "character")
            if not char:
                char = next((n for n, v in g.party.items()
                             if isinstance(v, dict)
                             and str(v.get("owner_id", ""))
                             == str(interaction.user.id)), "")
            from engine.charlib import (CORE_SKILLS, SKILL_ABILITY,
                                        SKILL_LABEL)
            prof = set(CORE_SKILLS.get(
                g.party.get(char, {}).get("occupation", ""), []))
            q = (current or "").strip().lower()
            out = []
            for sk, ab in SKILL_ABILITY.items():
                star = "★" if sk in prof else ""
                label = f"{star}{SKILL_LABEL[sk]} {sk}（{ab}）"
                if not q or q in label.lower() or q in sk:
                    out.append(app_commands.Choice(name=label[:100],
                                                   value=SKILL_LABEL[sk]))
            out.sort(key=lambda c: not c.name.startswith("★"))
            return out[:25]

        async def combat_move_ac(interaction, current: str):
            cid = str(interaction.channel_id)
            g = svc._v4_service(str(interaction.channel_id)).game
            char = _filled_param(interaction, "character")
            if not char:
                char = next((n for n, v in g.party.items()
                             if isinstance(v, dict)
                             and str(v.get("owner_id", ""))
                             == str(interaction.user.id)), "")
            if not char:
                return [app_commands.Choice(name="（先用 /explore 建立角色）",
                                            value="")]
            from engine.moves import compute_attack_moves, move_spell_level
            inv = [("item", n, q) for n, q in g.inventory.get(char, [])]
            mx = g.max_slot(char)
            moves = compute_attack_moves(
                g.party[char].get("occupation", ""),
                int(g.party[char].get("level", 1)), inv, mx)
            cur_slots = g.party[char].get("slots") or {}
            q = (current or "").strip().lower()
            out = []
            for m in moves:
                # don't offer leveled moves whose slots are spent — the
                # engine would just deny them (法術格已用盡)
                req = move_spell_level.get(m["name"], 0)
                if req and int(cur_slots.get(str(req), 0)) <= 0:
                    continue
                label = (f"{m['name']}（{m['ability']} 傷害 {m['dmg']}"
                         + ("，範圍" if m["aoe"] else "") + "）")
                if not q or q in label.lower() or q in m["name"].lower():
                    out.append(app_commands.Choice(name=label[:100],
                                                   value=m["name"][:100]))
            return out[:25]

        async def any_char_ac(interaction, current: str):
            g = svc._v4_service(str(interaction.channel_id)).game
            q = (current or "").strip().lower()
            out = []
            for n, v in g.party.items():
                label = f"{n}（{v.get('occupation', '?')}）"
                if not q or q in label.lower():
                    out.append(app_commands.Choice(name=label[:100],
                                                   value=n[:100]))
            return out[:25]

        async def item_ac(interaction, current: str):
            g = svc._v4_service(str(interaction.channel_id)).game
            q = (current or "").strip().lower()
            names = []
            for stacks in g.inventory.values():
                names += [n for n, _ in stacks]
            names += ["治療藥水", "火把", "繩索", "匕首", "長劍", "木盾"]
            seen, out = set(), []
            for n in names:
                if n in seen:
                    continue
                seen.add(n)
                if not q or q in n.lower():
                    out.append(app_commands.Choice(name=n[:100],
                                                   value=n[:100]))
            return out[:25]

        def _filled_param(interaction, name) -> str:
            for o in (interaction.data or {}).get("options", []):
                if o.get("name") == name:
                    return (o.get("value") or "").strip()
            return ""

        # ---- /explore ----

        @self.tree.command(name="explore",
                           description="探索／對話／移動（非戰鬥行動）/ freeform action")
        @app_commands.describe(text="你要做什麼？")
        async def explore_cmd(interaction: discord.Interaction, text: str):
            await self._v4_turn(interaction, text)

        @self.tree.command(name="confirm",
                           description="確認待決行動 / confirm pending action")
        async def confirm_cmd(interaction: discord.Interaction):
            await self._v4_turn(interaction, "confirm", confirmation=True)

        # ---- combat command family ----------------------------------------
        # One command per action, each with its OWN typed autocomplete —
        # a single /combat with a polymorphic `target` could never show
        # enemy HP, item quantities and skill proficiency at once.

        async def _caller_char(interaction) -> str:
            g = svc._v4_service(str(interaction.channel_id)).game
            return next((n for n, v in g.party.items()
                         if isinstance(v, dict)
                         and str(v.get("owner_id", ""))
                         == str(interaction.user.id)), "")

        async def enemy_ac(interaction, current: str):
            g = svc._v4_service(str(interaction.channel_id)).game
            entries = []
            for name, foe in g.enemies.items():
                if foe.dead:
                    continue
                entries.append((f"{name} HP {foe.hp}/{foe.hp_max}（敵）",
                                name))
            if not entries:
                entries.append(("AC 15（自訂）", "AC 15"))
            q = (current or "").strip().lower()
            return [app_commands.Choice(name=d[:100], value=v[:100])
                    for d, v in entries if not q or q in d.lower()][:25]

        async def own_item_ac(interaction, current: str):
            g = svc._v4_service(str(interaction.channel_id)).game
            char = _filled_param(interaction, "character") \
                or await _caller_char(interaction)
            q = (current or "").strip().lower()
            out = []
            for name, qty in g.inventory.get(char, []):
                note = "（回復 2d4+2 HP）" if "藥水" in name else ""
                label = f"{name} ×{qty}{note}"
                if not q or q in name.lower():
                    out.append(app_commands.Choice(name=label[:100],
                                                   value=name[:100]))
            return out[:25]

        async def ally_ac(interaction, current: str):
            """Party members with HP — feeding a DOWNED ally is the
            whole point of the `on` parameter."""
            g = svc._v4_service(str(interaction.channel_id)).game
            char = _filled_param(interaction, "character") \
                or await _caller_char(interaction)
            q = (current or "").strip().lower()
            out = []
            for n, e in g.party.items():
                if not isinstance(e, dict):
                    continue
                hp, mx = int(e.get("hp_now", 0)), int(e.get("hp_max", 0))
                tag = "（倒地！）" if hp <= 0 else ""
                me = "（自己）" if n == char else ""
                label = f"{n} {hp}/{mx}HP{tag}{me}"
                if not q or q in label.lower():
                    out.append(app_commands.Choice(name=label[:100],
                                                   value=n[:100]))
            return out[:25]

        async def skill_on_ac(interaction, current: str):
            """Context targets for a skill: enemies + NPCs + party."""
            g = svc._v4_service(str(interaction.channel_id)).game
            entries = []
            for name, foe in g.enemies.items():
                if not foe.dead:
                    entries.append((f"{name} HP {foe.hp}/{foe.hp_max}（敵）",
                                    name))
            for n in g.world.here.npcs:
                entries.append((f"{n['name']}（NPC）", n["name"]))
            for n, e in g.party.items():
                if isinstance(e, dict):
                    hp = int(e.get("hp_now", 0))
                    tag = "（倒地！）" if hp <= 0 else "（隊友）"
                    entries.append((f"{n} {hp}/{e.get('hp_max', 0)}HP{tag}",
                                    n))
            q = (current or "").strip().lower()
            return [app_commands.Choice(name=d[:100], value=v[:100])
                    for d, v in entries if not q or q in d.lower()][:25]

        @self.tree.command(name="attack",
                           description="⚔️ 攻擊（戰鬥中）/ attack a target")
        @app_commands.describe(target="目標（敵人清單）", move="招式")
        @app_commands.autocomplete(target=enemy_ac, move=combat_move_ac)
        async def attack_cmd(interaction: discord.Interaction,
                             target: str, move: str = ""):
            char = await _caller_char(interaction)
            if not char:
                await interaction.response.send_message("❓ 找不到你的角色。")
                return
            text = f"{char} 攻擊 {target}" + (f" {move}" if move else "")
            await self._v4_turn(interaction, text, structured=True)

        @self.tree.command(name="use",
                           description="🎒 使用物品／餵隊友（可救倒地）/ use item")
        @app_commands.describe(item="物品（自己的背包）",
                               on="給誰用（留空＝自己，可餵倒地隊友）")
        @app_commands.autocomplete(item=own_item_ac, on=ally_ac)
        async def use_cmd(interaction: discord.Interaction,
                          item: str, on: str = ""):
            char = await _caller_char(interaction)
            if not char:
                await interaction.response.send_message("❓ 找不到你的角色。")
                return
            text = f"{char} 使用 {item}" + (f" {on}" if on else "")
            await self._v4_turn(interaction, text, structured=True)

        @self.tree.command(name="skill",
                           description="✨ 技能（★＝熟練）/ use a skill")
        @app_commands.describe(skill="技能（18 項，★＝熟練）",
                               on="對象（敵人／NPC／隊友）")
        @app_commands.autocomplete(skill=skill_ac, on=skill_on_ac)
        async def skill_cmd(interaction: discord.Interaction,
                            skill: str, on: str = ""):
            char = await _caller_char(interaction)
            if not char:
                await interaction.response.send_message("❓ 找不到你的角色。")
                return
            text = f"{char} 技能 {skill}" + (f" {on}" if on else "")
            await self._v4_turn(interaction, text, structured=True)

        @self.tree.command(name="defend",
                           description="🛡 防禦（敵人攻擊你有劣勢）/ dodge")
        async def defend_cmd(interaction: discord.Interaction):
            char = await _caller_char(interaction)
            if not char:
                await interaction.response.send_message("❓ 找不到你的角色。")
                return
            await self._v4_turn(interaction, f"{char} 防禦", structured=True)

        @self.tree.command(name="flee",
                           description="🏃 撤退（全隊脫離戰鬥）/ escape")
        async def flee_cmd(interaction: discord.Interaction):
            char = await _caller_char(interaction)
            if not char:
                await interaction.response.send_message("❓ 找不到你的角色。")
                return
            await self._v4_turn(interaction, f"{char} 撤退", structured=True)

        @self.tree.command(name="observe",
                           description="👁 觀察敵人（下一擊有優勢）/ find a weak spot")
        @app_commands.describe(target="觀察哪個敵人（留空＝第一個）")
        @app_commands.autocomplete(target=enemy_ac)
        async def observe_cmd(interaction: discord.Interaction,
                              target: str = ""):
            char = await _caller_char(interaction)
            if not char:
                await interaction.response.send_message("❓ 找不到你的角色。")
                return
            text = f"{char} 觀察" + (f" {target}" if target else "")
            await self._v4_turn(interaction, text, structured=True)

        # ---- /inventory ----

        @self.tree.command(name="inventory",
                           description="物品欄與招式一覽 / inventory & moves")
        @app_commands.describe(character="角色（留空＝自己）")
        @app_commands.autocomplete(character=any_char_ac)
        async def inventory_cmd(interaction: discord.Interaction,
                                character: str = ""):
            g = svc._v4_service(str(interaction.channel_id)).game
            # default to the caller's character
            char = character.strip()
            if not char:
                char = next((n for n, v in g.party.items()
                             if str(v.get("owner_id", ""))
                             == str(interaction.user.id)), "")
            if not char or char not in g.party:
                await interaction.response.send_message(
                    "❓ 請指定角色（用 character 參數挑選）。")
                return
            e = g.party[char]
            lines = [f"🎒 **{char}** — {e.get('occupation', '?')} "
                     f"Lv{e.get('level', 1)} "
                     f"HP {e['hp_now']}/{e['hp_max']}", ""]
            # inventory
            inv = g.inventory.get(char, [])
            if inv:
                lines.append("**物品 / Items**")
                for name, qty in inv:
                    lines.append(f"  🎒 {name}" + (f" ×{qty}" if qty > 1 else ""))
            else:
                lines.append("**物品 / Items**：（空）")
            # spell slots
            slots = e.get("slots") or {}
            if slots:
                lines.append("")
                lines.append("**法術格 / Spell Slots**")
                lines.append("  " + " · ".join(
                    f"L{k} {v}" for k, v in slots.items()))
            # skills: the 18 SRD skills, proficiency starred
            from engine.charlib import (CORE_SKILLS, SKILL_ABILITY,
                                        SKILL_LABEL)
            prof = set(CORE_SKILLS.get(e.get("occupation", ""), []))
            lines.append("")
            lines.append(f"**技能 / Skills**（★＝熟練，共 {len(prof)} 項）")
            by_ab = {}
            for sk, ab in SKILL_ABILITY.items():
                by_ab.setdefault(ab, []).append(sk)
            for ab in ("STR", "DEX", "INT", "WIS", "CHA"):
                row = " · ".join(
                    ("★" if s in prof else "") + SKILL_LABEL[s]
                    for s in by_ab.get(ab, []))
                lines.append(f"  {ab}：{row}")
            lines.append("　　（用 `/explore` 描述動作，或 `/skill`）")
            # moves
            from engine.moves import (compute_attack_moves,
                                      move_spell_level)
            inv_tuples = [("item", n, q) for n, q in inv]
            mx = g.max_slot(char)
            moves = compute_attack_moves(
                e.get("occupation", ""), int(e.get("level", 1)),
                inv_tuples, mx)
            all_moves = compute_attack_moves(
                e.get("occupation", ""), int(e.get("level", 1)),
                inv_tuples, 99)
            lines.append("")
            lines.append("**招式 / Moves**")
            for m in all_moves:
                req = move_spell_level.get(m["name"], 0)
                aoe = "（範圍）" if m["aoe"] else ""
                if m in moves:
                    cost = f"（耗 {req} 環法術格）" if req else ""
                    src = "（裝備）" if m.get("improvised") else ""
                    lines.append(f"  ⚔️ {m['name']} {m['ability']} "
                                 f"{m['dmg']}{aoe}{cost}{src}")
                else:
                    lines.append(f"  🔒 {m['name']} {m['ability']} "
                                 f"{m['dmg']}{aoe} — 需 {req} 環（未解鎖）")
            await interaction.response.send_message(
                "\n".join(lines)[:1900])

        # ---- /roll ----

        @self.tree.command(name="roll", description="擲骰 / dice roll")
        @app_commands.describe(expr="骰式：d20、2d6+3、adv")
        async def roll_cmd(interaction: discord.Interaction,
                           expr: str = "d20"):
            from engine.dice import quick_roll
            expr = (expr or "d20").strip() or "d20"

            # a d20 that settles a pending check auto-continues the story
            if await self._v4_settle(interaction, expr):
                return

            try:
                result = quick_roll(expr)
            except ValueError as e:
                await interaction.response.send_message(f"⚠️ {e}")
                return
            await interaction.response.send_message(
                f"🎲 **{interaction.user.display_name}** {result}")

        # ---- /give ----

        @self.tree.command(name="give",
                           description="把物品給隊友 / give item to party member")
        @app_commands.describe(item="物品名", to="給誰（角色名）")
        @app_commands.autocomplete(item=item_ac)
        async def give_cmd(interaction: discord.Interaction,
                           item: str, to: str = ""):
            await interaction.response.defer(thinking=True)
            text = await svc._v4_service(str(interaction.channel_id)).transfer_item(
                str(interaction.user.id), item, to)
            await interaction.followup.send(_clip(text))

        # ---- /rules (SRD retrieval, zero LLM) ----

        def _rules_db_path() -> str:
            data_dir = os.environ.get("DATA_DIR", "data")
            return os.environ.get(
                "RULES_DB", os.path.join(data_dir, "rules.db"))

        # the SRD is English-titled; map common zh queries to it
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

        def _srd_query_terms(q: str) -> list:
            terms = [q]
            for zh, en in _SRD_ZH.items():
                if zh in q:
                    terms.append(en)
            return list(dict.fromkeys(terms))

        def rules_text(query: str) -> str:
            """Top SRD matches by title, then body — deterministic,
            no embeddings, no LLM. zh queries map to English titles."""
            import sqlite3
            q = (query or "").strip()
            if not q:
                return "❓ 查詢什麼？例：`/rules 火球術`、`/rules 哥布林`"
            try:
                conn = sqlite3.connect(
                    f"file:{_rules_db_path()}?mode=ro", uri=True,
                    timeout=2.0)
                try:
                    rows = []
                    for term in _srd_query_terms(q):
                        rows = conn.execute(
                            "SELECT title, kind, text FROM chunks "
                            "WHERE title LIKE ? "
                            "ORDER BY length(title) LIMIT 3",
                            (f"%{term}%",)).fetchall()
                        if rows:
                            break
                    if not rows:
                        rows = conn.execute(
                            "SELECT title, kind, text FROM chunks "
                            "WHERE text LIKE ? LIMIT 3",
                            (f"%{q}%",)).fetchall()
                finally:
                    conn.close()
            except sqlite3.Error as e:
                return f"⚠️ 規則庫不可用（{e}）"
            if not rows:
                return f"🔍 SRD 裡沒有「{query}」的條目（試英文名？）"
            out = [f"📚 **SRD 查詢：{query}**"]
            for title, kind, text in rows:
                body = " ".join((text or "").split())[:280]
                out.append(f"\n**{title}**（{kind}）\n{body}…")
            return "\n".join(out)[:1900]

        @self.tree.command(name="rules",
                           description="查 SRD 規則（法術/怪物/物品，即時無 LLM）/ SRD lookup")
        @app_commands.describe(query="關鍵字（法術名、怪物名、規則）")
        async def rules_cmd(interaction: discord.Interaction,
                            query: str):
            await interaction.response.send_message(rules_text(query))

        # ---- /status ----

        @self.tree.command(name="status", description="隊伍／場景狀態 / game status")
        async def status_cmd(interaction: discord.Interaction):
            g = svc._v4_service(str(interaction.channel_id)).game
            lines = [f"📍 場景：{g.world.here.name}"]
            if g.combat.active:
                waiting = [n for n in g.party
                           if g.alive(n) and n not in g.combat.acted]
                lines.append(f"⚔️ 戰鬥 R{g.combat.round} — 待行動："
                             + ("、".join(waiting) or "敵方回合結算中"))
            for n, e in g.party.items():
                slots = e.get("slots") or {}
                slot_txt = (" slots " + "·".join(
                    f"L{k}{v}" for k, v in slots.items())) if slots else ""
                d = e.get("death") or {}
                tag = ""
                if d.get("dead"):
                    tag = " 🪦已死"
                elif int(e.get("hp_now", 1) or 0) <= 0:
                    tag = (f" 🩶倒地（豁免 成{d.get('ok', 0)}/3"
                           f"·敗{d.get('fail', 0)}/3）"
                           + ("·已穩定" if d.get("stable") else ""))
                conds = e.get("conds") or {}
                if "inspired" in conds:
                    tag += " ✨靈感"
                if "poisoned" in conds:
                    tag += " ☠中毒"
                lines.append(f"  {n} {e.get('occupation', '?')}"
                             f" Lv{e.get('level', 1)}"
                             f"（XP {e.get('xp', 0)}/{100 * int(e.get('level', 1))}）"
                             f" HP {e['hp_now']}/{e['hp_max']}{slot_txt}{tag}")
            for n, f in g.enemies.items():
                if not f.dead:
                    lines.append(f"  👹 {n} {f.hp}/{f.hp_max} AC{f.ac}")
            # intel the party has already pried out of NPCs (engine-tracked)
            known = []
            for sid, s in g.world.scenes.items():
                for n in s.npcs:
                    for f in n.get("disclosed", []):
                        known.append(f"  📜 **{n['name']}**：{f}")
            if known:
                lines.append("📜 已知情報（打聽過的都在這裡，不用重問）")
                lines += known
            await interaction.response.send_message("\n".join(lines)[:1900])

        # ---- /continue ----

        @self.tree.command(name="continue",
                           description="遊戲卡住時推進 / nudge if stalled")
        async def continue_cmd(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            v4svc = svc._v4_service(str(interaction.channel_id))
            g = v4svc.game
            # 1. unfinished narration? FINISH IT FIRST — word by word
            if v4svc.has_unfinished():
                narr_msg = await interaction.channel.send(
                    "📖（續）DM 正在寫作…")
                last_edit = [0.0]

                def on_delta(acc: str):
                    now = asyncio.get_event_loop().time()
                    if now - last_edit[0] >= 2.0 and len(acc) > 6:
                        last_edit[0] = now
                        asyncio.create_task(self._edit_safe(narr_msg, acc))

                try:
                    cont, text = await v4svc.continue_narration(on_delta)
                except Exception:
                    log.exception("continue narration failed")
                    cont, text = False, ""
                if cont and text:
                    await narr_msg.edit(content=_clip("📖（續）" + text))
                    # resolve the deferred interaction (clears 正在思考…)
                    await interaction.followup.send(
                        "📖 未完成的敘事已接續完成（見上）")
                    return
                try:  # nothing came of it — clean up, fall through
                    await narr_msg.delete()
                except discord.HTTPException:
                    pass
            await interaction.followup.send(_clip(v4svc.nudge()))

        # ---- /help ----

        @self.tree.command(name="help", description="指令說明 / commands")
        async def help_cmd(interaction: discord.Interaction):
            for chunk in _split(HELP_TEXT):
                if not interaction.response.is_done():
                    await interaction.response.send_message(chunk)
                else:
                    await interaction.followup.send(chunk)

        # =============================================================
        #  admin commands
        # =============================================================

        @self.tree.command(name="explore-admin",
                           description="(Admin) 以任意角色行動 / act as any character")
        @app_commands.describe(text="行動內容", character="角色")
        @app_commands.autocomplete(character=any_char_ac)
        async def say_admin_cmd(interaction: discord.Interaction,
                                text: str, character: str):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("🚫 僅管理員。")
                return
            await self._v4_admin(interaction, character, text)

        # admin combat family — mirrors the player commands, acting as
        # any character (same typed autocompletes, character-aware)

        def _admin_only(interaction) -> bool:
            return bool(interaction.user.guild_permissions.manage_guild)

        @self.tree.command(name="attack-admin",
                           description="(Admin) 以任意角色攻擊")
        @app_commands.describe(character="角色", target="目標", move="招式")
        @app_commands.autocomplete(character=any_char_ac, target=enemy_ac,
                                   move=combat_move_ac)
        async def attack_admin_cmd(interaction: discord.Interaction,
                                   character: str, target: str,
                                   move: str = ""):
            if not _admin_only(interaction):
                await interaction.response.send_message("🚫 僅管理員。")
                return
            text = f"{character} 攻擊 {target}" + (f" {move}" if move else "")
            await self._v4_admin(interaction, character, text, structured=True)

        @self.tree.command(name="use-admin",
                           description="(Admin) 以任意角色使用物品／餵隊友")
        @app_commands.describe(character="角色", item="物品",
                               on="給誰用（可餵倒地隊友）")
        @app_commands.autocomplete(character=any_char_ac, item=own_item_ac,
                                   on=ally_ac)
        async def use_admin_cmd(interaction: discord.Interaction,
                                character: str, item: str, on: str = ""):
            if not _admin_only(interaction):
                await interaction.response.send_message("🚫 僅管理員。")
                return
            text = f"{character} 使用 {item}" + (f" {on}" if on else "")
            await self._v4_admin(interaction, character, text, structured=True)

        @self.tree.command(name="skill-admin",
                           description="(Admin) 以任意角色使用技能")
        @app_commands.describe(character="角色", skill="技能", on="對象")
        @app_commands.autocomplete(character=any_char_ac, skill=skill_ac,
                                   on=skill_on_ac)
        async def skill_admin_cmd(interaction: discord.Interaction,
                                  character: str, skill: str,
                                  on: str = ""):
            if not _admin_only(interaction):
                await interaction.response.send_message("🚫 僅管理員。")
                return
            text = f"{character} 技能 {skill}" + (f" {on}" if on else "")
            await self._v4_admin(interaction, character, text, structured=True)

        @self.tree.command(name="defend-admin",
                           description="(Admin) 以任意角色防禦")
        @app_commands.describe(character="角色")
        @app_commands.autocomplete(character=any_char_ac)
        async def defend_admin_cmd(interaction: discord.Interaction,
                                   character: str):
            if not _admin_only(interaction):
                await interaction.response.send_message("🚫 僅管理員。")
                return
            await self._v4_admin(interaction, character, f"{character} 防禦",
                            structured=True)

        @self.tree.command(name="flee-admin",
                           description="(Admin) 以任意角色撤退")
        @app_commands.describe(character="角色")
        @app_commands.autocomplete(character=any_char_ac)
        async def flee_admin_cmd(interaction: discord.Interaction,
                                 character: str):
            if not _admin_only(interaction):
                await interaction.response.send_message("🚫 僅管理員。")
                return
            await self._v4_admin(interaction, character, f"{character} 撤退",
                            structured=True)

        @self.tree.command(name="observe-admin",
                           description="(Admin) 以任意角色觀察敵人")
        @app_commands.describe(character="角色", target="觀察哪個敵人")
        @app_commands.autocomplete(character=any_char_ac, target=enemy_ac)
        async def observe_admin_cmd(interaction: discord.Interaction,
                                    character: str, target: str = ""):
            if not _admin_only(interaction):
                await interaction.response.send_message("🚫 僅管理員。")
                return
            text = f"{character} 觀察" + (f" {target}" if target else "")
            await self._v4_admin(interaction, character, text, structured=True)

        @self.tree.command(name="inspire-admin",
                           description="(Admin) 給角色靈感（一次性攻擊/檢定優勢）/ grant inspiration")
        @app_commands.describe(character="角色（留空＝全隊）")
        @app_commands.autocomplete(character=any_char_ac)
        async def inspire_admin_cmd(interaction: discord.Interaction,
                                    character: str = ""):
            if not _admin_only(interaction):
                await interaction.response.send_message("🚫 僅管理員。")
                return
            await interaction.response.defer(thinking=True)
            text = await svc._v4_service(str(interaction.channel_id)).admin_inspire(character)
            await interaction.followup.send(_clip(text))

        @self.tree.command(name="xp-admin",
                           description="(Admin) 給 XP（自動升級）/ grant XP with level-ups")
        @app_commands.describe(amount="XP 量（可負數）", character="角色（留空＝全隊）")
        @app_commands.autocomplete(character=any_char_ac)
        async def xp_admin_cmd(interaction: discord.Interaction,
                               amount: int, character: str = ""):
            if not _admin_only(interaction):
                await interaction.response.send_message("🚫 僅管理員。")
                return
            await interaction.response.defer(thinking=True)
            text = await svc._v4_service(str(interaction.channel_id)).admin_xp(amount, character)
            await interaction.followup.send(_clip(text))

        @self.tree.command(name="roll-admin",
                           description="(Admin) 代擲 / roll for any character")
        @app_commands.describe(expr="骰式", character="角色")
        @app_commands.autocomplete(character=any_char_ac)
        async def roll_admin_cmd(interaction: discord.Interaction,
                                 expr: str = "d20", character: str = ""):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("🚫 僅管理員。")
                return
            expr = (expr or "d20").strip() or "d20"
            # an admin d20 settles a pending check and auto-continues too
            if await self._v4_settle(interaction, expr,
                                echo=f"(admin) {character}".rstrip(), admin=True, actor=character):
                return
            try:
                result = quick_roll(expr)
            except ValueError as e:
                await interaction.response.send_message(f"⚠️ {e}")
                return
            await interaction.response.send_message(
                f"🎲 (admin) **{character or '?'}** {result}")

        @self.tree.command(name="give-admin",
                           description="(Admin) 給物品 / give item")
        @app_commands.describe(character="角色", item="物品", qty="數量")
        @app_commands.autocomplete(character=any_char_ac, item=item_ac)
        async def give_admin_cmd(interaction: discord.Interaction,
                                 character: str, item: str, qty: int = 1):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("🚫 僅管理員。")
                return
            await interaction.response.defer(thinking=True)
            out = await svc._v4_service(str(interaction.channel_id)).admin_give(character, item, qty)
            await interaction.followup.send(_clip(out))

    # ------------------------------------------------------------------
    #  message handling: table talk only (no DM reply, no emoji)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    #  shared v4 turn runners (defined last, used above)
    # ------------------------------------------------------------------

    async def _v4_turn(self, interaction, text, echo=None,
                       structured=False, confirmation=False):
        """Player v4 turn — v3 word-by-word UX:
        defer → echo (followup, resolves the thinking indicator) →
        engine verdict (instant) → narration streams into its own
        message and STAYS there (final edit completes it)."""
        # defer FIRST: acknowledges the interaction (prevents 3s timeout)
        await interaction.response.defer(thinking=True)
        # echo as the interaction followup: resolves 「正在思考…」
        # immediately — without any followup the ghost never clears
        await interaction.followup.send(
            f"🎭 **{interaction.user.display_name}** "
            f"{_clip(text, 200)}")

        narr_msg = [None]   # created once the verdict is posted
        last_edit = [0.0]

        async def on_resolved(lines):
            # engine verdict: instant, its own message
            g_ = self._v4_service(str(interaction.channel_id)).game
            view = CombatPanel(self) if g_.combat.active else None
            await interaction.channel.send(_clip("\n".join(lines)),
                                           view=view)
            # narration streams word-by-word into THIS message and
            # keeps its streamed content (never replaced afterwards)
            narr_msg[0] = await interaction.channel.send(
                "📖 DM 正在寫作…")

        def on_delta(acc: str):
            msg = narr_msg[0]
            if msg is None:
                return
            now = asyncio.get_event_loop().time()
            if now - last_edit[0] >= 2.0 and len(acc) > 12:
                last_edit[0] = now
                asyncio.create_task(self._edit_safe(msg, acc))

        try:
            service = self._v4_service(str(interaction.channel_id))
            if confirmation:
                lines, narration = await service.confirm(
                    str(interaction.user.id), on_delta=on_delta, on_resolved=on_resolved,
                    admin=interaction.user.guild_permissions.manage_guild)
            else:
                lines, narration = await service.handle(
                    text, interaction.user.display_name,
                    user_id=str(interaction.user.id), structured=structured,
                    on_delta=on_delta, on_resolved=on_resolved)
        except Exception as e:
            log.exception("v4 turn failed")
            if narr_msg[0] is not None:
                await narr_msg[0].edit(content=f"⚠️ {e}")
            else:
                await interaction.followup.send(f"⚠️ {e}")
            return

        # finalize the streamed message: guards may have scrubbed
        # parts, so the last edit is authoritative; drop it entirely
        # when the narrator produced nothing (degraded mode)
        await self._finalize_narr(narr_msg[0], narration)
        if not narr_msg[0] and lines:
            g_ = self._v4_service(str(interaction.channel_id)).game
            view = CombatPanel(self) if g_.combat.active else None
            await interaction.channel.send(_clip("\n".join(lines)),
                                           view=view)

    async def _edit_safe(self, msg, content):
        try:
            await msg.edit(content=_clip(content) + " ▍")
        except discord.HTTPException:
            pass

    async def _finalize_narr(self, msg, narration: str):
        """End state of the streamed narration message: the final
        (guard-applied) text, or deleted when nothing was produced."""
        if msg is None:
            return
        try:
            if narration:
                await msg.edit(content=_clip("📖 " + narration))
            else:
                await msg.delete()
        except discord.HTTPException:
            pass

    async def _v4_settle(self, interaction, expr, echo="", *, admin=False, actor=""):
        """A d20 /roll that settles a pending check AUTO-CONTINUES the
        story: die echo → engine verdict (instant) → narration streams
        word-by-word into its own message and stays.
        Returns True when it was a valid settle (command consumed)."""
        v4svc = self._v4_service(str(interaction.channel_id))
        pend = getattr(v4svc.game, "_pending_check", None)
        if not pend or expr.lower() not in ("d20", "1d20"):
            return False
        await interaction.response.defer(thinking=True)
        # The service rolls only after ownership checks, and posts after commit.
        expected_event_id = pend.get("event_id")
        await interaction.followup.send(f"🎲 **{interaction.user.display_name}** {echo} 請求結算檢定")

        narr_msg = [None]
        last_edit = [0.0]

        async def on_resolved(lines):
            g_ = self._v4_service(str(interaction.channel_id)).game
            view = CombatPanel(self) if g_.combat.active else None
            await interaction.channel.send(_clip("\n".join(lines)),
                                           view=view)
            narr_msg[0] = await interaction.channel.send(
                "📖 DM 正在寫作…")

        def on_delta(acc: str):
            msg = narr_msg[0]
            if msg is None:
                return
            now = asyncio.get_event_loop().time()
            if now - last_edit[0] >= 2.0 and len(acc) > 12:
                last_edit[0] = now
                asyncio.create_task(self._edit_safe(msg, acc))

        try:
            lines, narration = await v4svc.settle_roll(
                on_delta=on_delta, on_resolved=on_resolved,
                user_id=str(interaction.user.id), admin=admin, actor=actor,
                expected_event_id=expected_event_id)
        except Exception as e:
            log.exception("v4 roll settle failed")
            if narr_msg[0] is not None:
                await narr_msg[0].edit(content=f"⚠️ {e}")
            else:
                await interaction.followup.send(f"⚠️ {e}")
            return True
        await self._finalize_narr(narr_msg[0], narration)
        if not narr_msg[0] and lines:
            g_ = self._v4_service(str(interaction.channel_id)).game
            view = CombatPanel(self) if g_.combat.active else None
            await interaction.channel.send(_clip("\n".join(lines)),
                                           view=view)
        return True

    async def _v4_admin(self, interaction, character, text,
                        structured=False):
        """Admin v4 turn for any character — SAME streaming UX as
        player turns (the old admin path bypassed handle(): no
        guards, no word-by-word, narration as one blob)."""
        await interaction.response.defer(thinking=True)
        # echo as the followup — resolves 「正在思考…」
        await interaction.followup.send(
            f"🎛 **{interaction.user.display_name}** (admin) — "
            f"**{character}** {_clip(text, 200)}")

        narr_msg = [None]
        last_edit = [0.0]

        async def on_resolved(lines):
            g_ = self._v4_service(str(interaction.channel_id)).game
            view = CombatPanel(self) if g_.combat.active else None
            await interaction.channel.send(_clip("\n".join(lines)),
                                           view=view)
            narr_msg[0] = await interaction.channel.send(
                "📖 DM 正在寫作…")

        def on_delta(acc: str):
            msg = narr_msg[0]
            if msg is None:
                return
            now = asyncio.get_event_loop().time()
            if now - last_edit[0] >= 2.0 and len(acc) > 12:
                last_edit[0] = now
                asyncio.create_task(self._edit_safe(msg, acc))

        try:
            lines, narration = await self._v4_service(
                str(interaction.channel_id)).handle(
                text, f"{interaction.user.display_name} (admin)",
                structured=structured, admin_actor=character,
                user_id=str(interaction.user.id),
                on_delta=on_delta, on_resolved=on_resolved)
        except Exception as e:
            log.exception("v4 admin turn failed")
            if narr_msg[0] is not None:
                await narr_msg[0].edit(content=f"⚠️ {e}")
            else:
                await interaction.followup.send(f"⚠️ {e}")
            return
        await self._finalize_narr(narr_msg[0], narration)
        if not narr_msg[0] and lines:
            g_ = self._v4_service(str(interaction.channel_id)).game
            view = CombatPanel(self) if g_.combat.active else None
            await interaction.channel.send(_clip("\n".join(lines)),
                                           view=view)

    async def on_message(self, message: discord.Message):
        if message.author.bot or not self.user:
            return
        content = message.content.strip()
        if not content:
            return
        v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
        if v4ch and str(message.channel.id) == v4ch:
            # plain text = table talk: ledger silently, no reaction, no reply
            try:
                self._v4_service(str(message.channel.id)).table_talk(
                    content, message.author.display_name)
            except Exception:
                pass
            return
        # non-v4 channels: ignore (v3 is retired)

    async def _v4_admin_run(self, interaction, character, text):
        """Deprecated shim — admin turns now go through handle() with
        admin_actor (guards + streaming). Kept only for external callers."""
        svc = self._v4_service(str(interaction.channel_id))
        lines, _ = await svc.handle(
            text, "admin", admin_actor=character, user_id=str(interaction.user.id))
        return lines


async def run_discord(token: str):
    bot = DiscordBot(message_content=True)
    await bot.start(token)
