"""v4 narrator: ledger facts → 繁中 prose (gemma3:27b).

Decoration only — the engine's lines are already the complete truth of the
turn; this adds novel-flavoured rendering on top. Any failure returns ""
and the game loses nothing (degraded mode by construction).
"""

import httpx


class Narrator:
    def __init__(self, llm_url: str, model: str = "qwen3.5:35b",
                 think: bool | None = False):
        self.url = llm_url.rstrip("/")
        self.model = model
        # None = omit (model default); False/True forces Ollama's native
        # think toggle — REQUIRED for qwen3.x-style reasoning models,
        # whose thinking would otherwise consume the whole token budget
        self.think = think

    def _body(self, prompt: str, max_tokens: int, stream: bool) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": stream,
            "options": {"temperature": 0.7, "num_predict": max_tokens}}
        if self.think is not None:
            body["think"] = self.think
        return body

    async def _call(self, prompt: str, max_tokens: int) -> str:
        """Non-streaming native /api/chat call."""
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{self.url}/api/chat",
                             json=self._body(prompt, max_tokens, False))
            r.raise_for_status()
            return (r.json().get("message", {}).get("content")
                    or "").strip()

    async def _stream(self, prompt: str, on_delta=None,
                      max_tokens: int = 400) -> str:
        """Streaming native /api/chat (ndjson) — on_delta receives the
        growing text as tokens arrive (v3 word-by-word UX).
        v6 latency guard: per-chunk read timeout — a stalled generation
        returns whatever arrived instead of hanging the player."""
        import json as _json
        text = ""
        # generous total, but ANY single read taking >15s = stall
        timeout = httpx.Timeout(180.0, connect=5.0, read=15.0)
        async with httpx.AsyncClient(timeout=timeout) as c:
            try:
                async with c.stream(
                        "POST", f"{self.url}/api/chat",
                        json=self._body(prompt, max_tokens, True)) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            chunk = _json.loads(line)
                        except _json.JSONDecodeError:
                            continue
                        delta = (chunk.get("message", {})
                                 .get("content", ""))
                        if delta:
                            text += delta
                            if on_delta:
                                try:
                                    on_delta(text)
                                except Exception:
                                    pass
                        if chunk.get("done"):
                            break
            except httpx.HTTPError as e:
                # stall/timeout mid-stream: keep the partial prose
                import logging
                logging.getLogger("dnd-bot").warning(
                    "narration stream interrupted (%s), %d chars so far",
                    type(e).__name__, len(text))
        return text

    async def continue_text(self, partial: str, on_delta=None) -> str:
        """Resume narration that was cut off mid-output: continue the
        prose from the breakpoint (no re-intro, no repetition)."""
        prompt = (
            "你正在為 D&D 遊戲擔任 DM，用繁體中文寫敘事。"
            "先前的輸出被中斷了，已寫出的部分如下"
            "（可能停在某句中間）：\n"
            f"「{partial}」\n\n"
            "請從中斷處自然地續寫下去，完成這段敘事：\n"
            "· 直接續寫——不要重複已有文字、不要重新開頭、不要總結\n"
            "· 約 60~120 字，最後留一個鉤子（疑問、選擇或暗示）\n"
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
                      world: str = "") -> str:
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
                f"\n玩家這回合做／說了：「{player_input}」\n"
                "第一句必須回應玩家這句話。回應≠複述："
                "禁止「『……』某某大聲喊道／說道」式的直白引述——"
                "要讓這句話撞進世界，用旁人的反應、突然的安靜、"
                "荒謬的落差來呈現它。\n"
                "如果引擎拒絕了這個行動：讓角色照樣做，但讓世界誠實地"
                "回應——側目、沉默三秒、有人嗤笑出聲、NPC一臉"
                "「你在考驗我的耐心」。\n"
                "寫法結構對照（僅示範結構——示範的字句絕不得出現在"
                "你的敘事裡）：\n"
                "✗ 引述原話＋「大聲喊道」。\n"
                "✓ 那句話在屋樑間震出一層灰；全場靜得連骰子落桌都"
                "聽得見，然後有人沒忍住笑了出聲。\n")

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
            + ("· 開頭列出的世界事實絕不得矛盾或推翻\n" if world else ""))
        if extra_directive:
            tail += f"· {extra_directive}\n"
        prompt = (
            f"{world_head}"
            f"場景：{scene}\n隊伍現況：{party_brief}{hint_block}\n"
            "（引擎事實，僅供核對：\n"
            + "\n".join(f"- {f}" for f in facts) + "）\n"
            f"{npc_block}"
            "你是地下城主（DM）。以「骨架句」為基礎，寫 120~200 字的"
            "繁體中文敘事。\n\n"
            "你可以做的事（DM 的創意空間）：\n"
            "· 描述環境變化（光線、聲音、氣味、天氣）\n"
            "· 讓 NPC 做出反應（表情、動作、情緒）\n"
            "· 透露玩家可能發現的細節（腳印、氣味、聲音來源）\n"
            "· 製造小插曲（奇怪的聲響、遠處的影子）\n"
            "· 鋪陳下一步的線索或危機預兆\n"
            "· 讓世界感覺活著、有反應\n\n"
            "你不可以做的事（引擎管轄）：\n"
            "· 決定成敗（骰子結果由引擎決定）\n"
            "· 給予物品或傷害（引擎追蹤）\n"
            "· 替玩家角色行動或說話\n"
            "· 直接傳送角色到新地點\n\n"
            "結尾必須留一個鉤子：未解的疑問、迫近的選擇、或暗示。"
            + tail)
        try:
            if on_delta:
                # streaming: on_delta receives the growing text (v3 UX)
                return (await self._stream(prompt, on_delta)).strip()
            # non-streaming (CLI, tests)
            return await self._call(prompt, 400)
        except Exception:
            return ""  # degraded mode: engine lines stand alone
