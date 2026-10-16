"""v4 narrator: ledger facts → 繁中 prose (gemma3:27b).

Decoration only — the engine's lines are already the complete truth of the
turn; this adds novel-flavoured rendering on top. Any failure returns ""
and the game loses nothing (degraded mode by construction).
"""

from .agent_config import legacy_config
from .model_gateway import ModelGateway


class Narrator:
    def __init__(self, llm_url: str = "http://127.0.0.1:11434", model: str = "qwen3.5:35b",
                 think: bool | None = False, *, gateway=None):
        self.gateway = gateway or ModelGateway(legacy_config(llm_url, narr_model=model, narr_think=think))

    async def _call(self, prompt: str, max_tokens: int) -> str:
        return await self.gateway.complete("narrator", prompt, max_tokens=max_tokens)

    async def _stream(self, prompt: str, on_delta=None, max_tokens: int = 400) -> str:
        return await self.gateway.stream("narrator", prompt, on_delta, max_tokens=max_tokens)

    async def continue_text(self, partial: str, on_delta=None, context: str = "") -> str:
        """Resume narration that was cut off mid-output: continue the
        prose from the breakpoint (no re-intro, no repetition)."""
        prompt = (
            f"{context}\n"
            "你正在為 D&D 遊戲擔任 DM，用繁體中文寫敘事。"
            "先前的輸出被中斷了，已寫出的部分如下"
            "（可能停在某句中間）：\n"
            f"「{partial}」\n\n"
            "請從中斷處自然地續寫下去，完成這段敘事：\n"
            "· 直接續寫——不要重複已有文字、不要重新開頭、不要總結\n"
            "· 約 60~120 字，只完成已發生事件；可用現有出口或目標收尾\n"
            "· 不能新增線索、房間、敵人、道具或未結算行動\n"
            "· 絕不寫出骰子數值或判定結果，不給予物品或傷害\n"
            "· 繁體中文，絕不使用簡體字\n"
            "=== 最後指示（最高優先）===\n"
            "· 從「」內文字的斷點直接繼續，第一個字就是下一個字\n")
        try:
            return (await self._stream(prompt, on_delta,
                                       max_tokens=220)).strip()
        except Exception:
            return ""

    async def narrate(self, facts: list, scene: str, party_brief: str,
                      hints: list = (), npc_knows: list = None,
                      npc_name: str = "",
                      extra_directive: str = None,
                      on_delta=None,
                      player_input: str = "",
                      scene_items: list = None,
                      party_inventory: dict = None,
                      world: str = "", memory: str = "") -> str:
        """hints = engine prose skeletons; npc_knows = facts an NPC may
        reveal; extra_directive = turn-level hard rule; on_delta = streaming
        callback; player_input = the player's own words — the first sentence
        must respond to it; scene_items = items physically present in the
        scene (the narrator may ONLY mention these); party_inventory =
        {char: [item names]} the party currently carries; world = tone +
        canon block distilled from gamerules.json (must never be
        contradicted).
        v3 lesson: most critical rules go at the END (canonical tail)."""
        if not facts:
            return ""
        world_head = f"{world}\n" if world else ""
        hint_block = ""
        if hints:
            hint_block = ("\n骨架句（依序作為段落骨架，可潤飾與連接，"
                          "但不得更改其內容）：\n"
                          + "\n".join(f"- {h}" for h in hints))
        npc_block = ""
        if npc_knows is not None and npc_name:
            knows_txt = ("、".join(npc_knows) if npc_knows
                         else "（此NPC沒有可透露的情報）")
            npc_block = (
                f"\n⚠️ NPC 對話規則（{npc_name}）：你只能讓此NPC說出"
                f"以下已知事實：{knows_txt}。\n"
                "絕不得透露不在清單上的資訊（不得給予地點、物品、"
                "攻略建議、劇情推測）；可以閒聊、可以拒絕回答、"
                "可以要求交換條件。\n")
        # item inventory: the narrator may ONLY mention items that exist
        items_block = ""
        if scene_items or party_inventory:
            lines = []
            if scene_items:
                lines.append(f"場景中存在的物品（僅限這些）："
                             f"{'、'.join(scene_items)}")
            else:
                lines.append("場景中沒有可拾取的物品。")
            if party_inventory:
                for ch, items in party_inventory.items():
                    if items:
                        lines.append(f"{ch} 攜帶：{'、'.join(items)}")
            lines.append(
                "⚠️ 絕不提及不在以上清單中的任何物品名稱——"
                "如果你描述的場景需要一件道具，用泛稱"
                "（「某件工具」「雜物」），不要發明具體物品名。"
                "提及清單中的物品時用粗體：**治療藥水**。")
            items_block = "\n".join(lines) + "\n"

        # v3 lesson: the player's own words must be answered, not ignored
        player_block = ""
        if player_input:
            player_block = (
                f"\n玩家提出的嘗試／說話：「{player_input}」\n"
                "第一句回應該嘗試，行動是否完成只以引擎事實為準。\n"
                "拒絕＝沒有執行；等待檢定＝只有嘗試，尚未成功或失敗。"
                "不要為語法錯誤、缺少物品或求助嘲笑玩家。\n")

        # v3 lesson: most critical rules at the END (small models attend
        # most to recent tokens — canonical tail principle)
        tail = (
            f"{items_block}"
            f"{player_block}"
            "\n=== 最後指示（最高優先）===\n"
            "· 繁體中文，絕不使用簡體字\n"
            "· 一律第三人稱敘述（用角色名），絕不用「你」稱呼角色\n"
            "· 絕不寫出任何骰子數值、算式或判定結果\n"
            "· 絕不給予物品、傷害或經驗（那是引擎的工作）\n"
            "· 逐一保持引擎事實的行動者和目標；治療描寫只屬於被治療者\n"
            "· 已倒下的敵人不會復活；等待檢定、取消和拒絕的行動不能寫成已完成\n"
            "· 下一步只能引用已公開的出口、物品、NPC 和目標，不能製造新的線索\n"
            + ("· 開頭列出的世界事實絕不得矛盾或推翻\n" if world else ""))
        if extra_directive:
            tail += f"· {extra_directive}\n"
        prompt = (
            f"{world_head}"
            f"{memory}\n"
            f"場景：{scene}\n隊伍現況：{party_brief}{hint_block}\n"
            "（引擎事實，僅供核對：\n"
            + "\n".join(f"- {f}" for f in facts) + "）\n"
            f"{npc_block}"
            "你是地下城主（DM）。以「骨架句」為基礎，寫 120~200 字的"
            "繁體中文敘事；簡單行動可用 40~80 字。不要重複霧氣、沉默或嘲弄的開頭。\n\n"
            "你可以做的事（DM 的創意空間）：\n"
            "· 描述環境變化（光線、聲音、氣味、天氣）\n"
            "· 讓 NPC 做出反應（表情、動作、情緒）\n"
            "· 描述已公開細節，不加入可互動但引擎沒有追蹤的新事物\n"
            "· 讓世界感覺活著、有反應\n\n"
            "你不可以做的事（引擎管轄）：\n"
            "· 決定成敗（骰子結果由引擎決定）\n"
            "· 給予物品或傷害（引擎追蹤）\n"
            "· 替玩家角色行動或說話\n"
            "· 直接傳送角色到新地點\n\n"
            "有可行下一步時可以用它收尾；事件已完成時可以自然結束。"
            + tail)
        try:
            if on_delta:
                # streaming: on_delta receives the growing text (v3 UX)
                return (await self._stream(prompt, on_delta)).strip()
            # non-streaming (CLI, tests)
            return await self._call(prompt, 400)
        except Exception:
            return ""  # degraded mode: engine lines stand alone
