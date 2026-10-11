"""v5 Director LLM — the generative half of the Director.

The v5 contract, applied to SPACE and DISCOVERY: players may walk into
unplanned places and find unplanned things (that's why a non-narrative
LLM layer exists at all). This class only PROPOSES typed JSON — the
engine (v4/director.canonize_*) validates and writes; the LLM never
touches game state. Uses the small fast digestor model (~1s).
"""

import json
import logging
import re

import httpx

log = logging.getLogger("dnd-bot")


class DirectorLLM:
    def __init__(self, llm_url: str, model: str = "qwen2.5:7b-instruct"):
        self.url = llm_url.rstrip("/")
        self.model = model

    async def _call(self, prompt: str, max_tokens: int = 200) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.8, "num_predict": max_tokens}}
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{self.url}/api/chat", json=body)
            r.raise_for_status()
            out = (r.json().get("message", {}).get("content") or "")
        try:
            from opencc import OpenCC
            out = OpenCC("s2t").convert(out)   # names enter world state
        except ImportError:
            pass
        return out

    async def gen_scene(self, tone: str, from_name: str,
                        from_desc: str, where: str,
                        gamerules_canon=()) -> dict | None:
        """Propose an UNPLANNED scene the player walks into. Typed:
        short name, one-line description, optional neutral NPC hint.
        The engine builds the graph node + bidirectional exits."""
        canon = "、".join(gamerules_canon[:6]) if gamerules_canon else "（無）"
        prompt = (
            f"世界基調：{tone}\n已知事實（不得矛盾）：{canon}\n"
            f"玩家目前在「{from_name}」（{from_desc[:50]}），"
            f"描述要去：「{where}」。\n"
            "這是一個未規劃的地方——請生成它。輸出恰好一個 JSON：\n"
            '{"name":"2-6字地點名","description":"30-60字場景描述",'
            '"npc":""或"2-4字人物名"}\n'
            "規則：符合基調；不得推翻已知事實；不得包含戰鬥數值、"
            "寶物或任務關鍵物；npc 留空＝沒有人物。只輸出 JSON。")
        try:
            out = await self._call(prompt)
            m = re.search(r"\{.*\}", out, re.S)
            d = json.loads(m.group(0))
            name = str(d.get("name", "")).strip()
            desc = str(d.get("description", "")).strip()
            npc = str(d.get("npc", "")).strip()
            if not (1 < len(name) <= 8) or not desc or len(desc) > 120:
                log.warning("director gen_scene rejected shape: %r",
                            {"name": name, "desc": desc[:40]})
                return None
            return {"name": name, "description": desc, "npc": npc[:8]}
        except Exception as e:
            log.warning("director gen_scene failed: %s", e)
            return None

    async def gen_item(self, tone: str, scene_name: str,
                       scene_desc: str, action: str) -> dict | None:
        """Propose an UNPLANNED but INERT discovery for a search that
        found nothing authored. The engine classifies it (props only —
        no weapons/treasure/magic) before materializing."""
        prompt = (
            f"世界基調：{tone}\n場景「{scene_name}」（{scene_desc[:50]}）。\n"
            f"玩家行動：「{action[:60]}」——一無所獲太掃興了。\n"
            "生成一件符合場景、平凡而有趣的小物件。輸出恰好一個 JSON：\n"
            '{"name":"2-10字物品名"}\n'
            "規則：必須是機械上無意義的小物（不得是武器/防具/藥水/"
            "金錢/珠寶/魔法物/鑰匙/情報物）；名字要有畫面感。"
            "只輸出 JSON。")
        try:
            out = await self._call(prompt, max_tokens=80)
            m = re.search(r"\{.*\}", out, re.S)
            d = json.loads(m.group(0))
            name = str(d.get("name", "")).strip()
            if not (1 < len(name) <= 12):
                log.warning("director gen_item rejected shape: %r", name)
                return None
            return {"name": name}
        except Exception as e:
            log.warning("director gen_item failed: %s", e)
            return None
