"""v4 digestor: free-form player text → Intent JSON (gemma3:12b).

The LLM only READS language here — its output is data the engine then
validates. Any failure falls back to the deterministic command parser,
and finally to a low-confidence 'talk' intent (a safe sink: facts only).
"""

import json
import re

import httpx

from .intent import ACTIONS, Intent, parse_command


class Digestor:
    def __init__(self, llm_url: str, model: str = "qwen3.5:35b",
                 think: bool | None = False):
        self.url = llm_url.rstrip("/")
        self.model = model
        # reasoning models MUST run with think off here or the budget
        # is consumed before any JSON is emitted (None = model default)
        self.think = think

    async def digest(self, text: str, party_names, scene_name: str = "",
                     exits=(), known_targets=()) -> Intent:
        tgt_line = (f"已知目標（盡量用這些精確名稱）: {'、'.join(known_targets)}\n"
                    if known_targets else "")
        prompt = (
            "你是遊戲引擎的意圖解析器。玩家輸入係香港粵語口語，"
            "經常夾雜英文（例如「i picked a sword」「我 attack 佢」）。"
            "先把輸入用書面中文重述（paraphrase），再判斷動作。\n"
            "把玩家的動作轉成「恰好一個」JSON 物件，不要輸出其他文字。\n\n"
            "動作定義（按意思，不只按字面）：\n"
            "attack ＝企圖傷害或制服場上某人／生物（武器、拳腳、法術、擒拿）。\n"
            "         擋、閃、恐嚇、大叫、追趕都唔算。撞門、劈鎖係對物件，"
            "用 creative。\n"
            "take   ＝拾起／拿走場景中嘅物件（執、撿、攞、pick up）。\n"
            "move   ＝去另一個地方（去、前往、go to）。\n"
            "talk   ＝同 NPC 說話或提問（傾計、問、ask）。想呃佢填 skill="
            "deception，想嚇佢填 skill=intimidation，想遊說填 skill="
            "persuasion。\n"
            "search ＝搜索、調查周圍環境。\n"
            "use    ＝使用身上嘅物品（只限物品——技能名稱例如醫藥／潛行／"
            "洞察係 skill，唔好用 use。餵隊友食藥／藥水都算 use："
            "target 填隊友名——隊友暈咗都可以餵）。\n"
            "cast   ＝施展法術或技能。\n"
            "skill  ＝主動使用一項 D&D 技能（冇施法成分嗰啲）。認住意思：\n"
            "         匿埋／收埋／靜靜雞＝stealth；睇穿佢講大話／睇下佢想"
            "點＝insight；幫佢包紮／急救／救返佢／救醒佢／救助暈低嘅隊友＝"
            "medicine；爬牆／爆門／游水＝"
            "athletics；平衡／翻滾／跳＝acrobatics；偷嘢／解鎖＝sleight of "
            "hand；跟蹤跡／搵食搵水＝survival；研究符文／魔法物件＝arcana；"
            "回想背景＝history；認動植物＝nature；認神祇儀式＝religion；"
            "安撫動物＝animal handling；留意四周＝perception；搜證推理＝"
            "investigation；講大話呃人＝deception；惡言威嚇＝intimidation；"
            "唱歌演戲＝performance；講道理打動人＝persuasion；鬥力角力＝"
            "athletics。\n"
            "creative＝自創花招（對物件或環境嘅非標準動作）。\n"
            "claim  ＝玩家宣稱自己得到能力、等級、物品（「我升到99級」）"
            "——唔係真嘅動作。\n"
            "meta   ＝願望、目標、感受、角色想法。\n"
            "chat   ＝玩家之間嘅對話（唔係對 DM 講）。\n"
            "pass   ＝等待、跳過。\n"
            "defend ＝專心防禦／閃避（ Dodge）。\n"
            "escape ＝撤退、逃走。\n"
            "observe＝觀察敵人搵破綻。\n"
            "help   ＝幫隊友打下手（佢下一次檢定有優勢；幫佢包紮就係 "
            "skill medicine）。\n\n"
            "另外判斷 wants_help：玩家係咪正在尋求幫助、建議、指引、"
            "提示（「點算好」「有咩線索」「我哋而家做咩」「any ideas?」"
            "「stuck 咗」）——無論佢用咩語言、咩講法，按意思判斷，"
            "唔好只靠字面。純動作（攻擊、執嘢、行路）填 false。\n\n"
            f"玩家角色: {'、'.join(party_names)}\n"
            f"目前場景: {scene_name}"
            f"（通道: {'、'.join(exits) if exits else '無'}）\n"
            + tgt_line +
            '欄位: {"paraphrase","action","actor","target","item",'
            '"spell","destination","utterance","skill","wants_help"} — '
            "paraphrase 係書面中文重述；skill 填英文技能名"
            "（stealth/insight/medicine/athletics/acrobatics/"
            "sleight of hand/survival/arcana/history/nature/religion/"
            "animal handling/perception/investigation/deception/"
            "intimidation/performance/persuasion）；wants_help 係 true/"
            "false；用不到嘅欄位填空字串。\n\n"
            "範例（真實粵語＋混英文）：\n"
            '「我用劍劈小明」→ {"paraphrase":"用劍砍小明","action":"attack",'
            '"actor":"","target":"小明","item":"劍"}\n'
            '「揼佢一拳」→ {"paraphrase":"打他一拳","action":"attack",'
            '"target":"最近的敵人"}\n'
            '「篤爆個鎖」→ {"paraphrase":"撬開門鎖","action":"creative",'
            '"utterance":"撬開門鎖"}\n'
            '「執起地下把劍」→ {"paraphrase":"拾起地上的劍","action":"take",'
            '"item":"劍"}\n'
            '「i picked a sword」→ {"paraphrase":"拾起一把劍",'
            '"action":"take","item":"劍"}\n'
            '「我 attack 嗰隻 goblin」→ {"paraphrase":"攻擊那隻哥布林",'
            '"action":"attack","target":"哥布林"}\n'
            '「我升咗 99 級」→ {"paraphrase":"我升到了99級","action":"claim",'
            '"utterance":"升到99級"}\n'
            '「同老闆傾下計」→ {"paraphrase":"和老闆交談","action":"talk",'
            '"target":"老闆"}\n'
            '「周圍望下」→ {"paraphrase":"四處查看","action":"search"}\n'
            '「我匿埋喺暗處」→ {"paraphrase":"躲進陰影","action":"skill",'
            '"skill":"stealth"}\n'
            '「我嚇下個店主」→ {"paraphrase":"威嚇店主","action":"skill",'
            '"skill":"intimidation","target":"店主"}\n'
            '「睇下船長有冇講大話」→ {"paraphrase":"觀察船長是否說謊",'
            '"action":"skill","skill":"insight","target":"船長"}\n'
            '「快啲幫依思包紮」→ {"paraphrase":"替依思急救包紮",'
            '"action":"skill","skill":"medicine","target":"依思"}\n'
            '「快啲救返依思！」→ {"paraphrase":"趕快救醒依思",'
            '"action":"skill","skill":"medicine","target":"依思"}\n'
            '「餵依思喝治療藥水」→ {"paraphrase":"餵依思喝治療藥水",'
            '"action":"use","item":"治療藥水","target":"依思"}\n'
            '「對依思使用醫藥」→ {"paraphrase":"對依思施展醫藥",'
            '"action":"skill","skill":"medicine","target":"依思"}\n'
            '「我爬上去嗰道牆」→ {"paraphrase":"爬上那道牆","action":"skill",'
            '"skill":"athletics"}\n'
            '「我哋而家點算好？」→ {"paraphrase":"我們現在怎麼辦",'
            '"action":"meta","wants_help":true}\n'
            '「any ideas? stuck 咗」→ {"paraphrase":"卡住了，求建議",'
            '"action":"meta","wants_help":true}\n'
            '「我掟個木雕落海」→ {"paraphrase":"把木雕扔進海裡",'
            '"action":"creative","item":"木雕",'
            '"utterance":"把木雕扔進海裡"}\n'
            '「用火把點著張帆」→ {"paraphrase":"用火把點燃船帆",'
            '"action":"creative","item":"火把",'
            '"utterance":"用火把點燃船帆"}\n\n'
            "拿不準時，偏向遊戲動作（玩家的行動不能被漏掉）。\n"
            f'玩家輸入:「{text}」→')
        try:
            # native /api/chat (the OpenAI-compat endpoint ignores the
            # think toggle that reasoning models require)
            body = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 200}}
            if self.think is not None:
                body["think"] = self.think
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"{self.url}/api/chat", json=body)
                r.raise_for_status()
                out = r.json().get("message", {}).get("content") or ""
            try:
                from opencc import OpenCC
                out = OpenCC("s2t").convert(out)  # 剑 -> 劍 (item names!)
            except ImportError:
                pass
            m = re.search(r"\{.*\}", out, re.S)
            data = json.loads(m.group(0))
            action = data.get("action", "")
            if action not in ACTIONS:
                raise ValueError(f"bad action: {action}")
            return Intent(
                action=action,
                actor=(data.get("actor") or "").strip(),
                target=(data.get("target") or "").strip(),
                item=(data.get("item") or "").strip(),
                spell=(data.get("spell") or "").strip(),
                destination=(data.get("destination") or "").strip(),
                utterance=(data.get("utterance") or "").strip(),
                skill=(data.get("skill") or "").strip(),
                raw=text,
                confidence=max(0.0, min(1.0, float(data.get("confidence") or 0.8))),
                wants_help=str(data.get("wants_help", "")).strip().lower()
                in ("true", "1", "yes", "係", "是"))
        except Exception:
            it = parse_command(text, party_names)
            if it is None:
                it = Intent(action="talk", utterance=text, raw=text,
                            confidence=0.3)
            return it
