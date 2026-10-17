"""Bounded, engine-owned linked plans. Models propose attempts, never results.

Supports pickup/preparation then attack/use, or observation then conversation.
Pickup is one object interaction; preparation and the final action each cost
an action. Remaining steps require an explicit player command.
"""
from collections import Counter
from copy import deepcopy
import hashlib
import json

from engine.charlib import SKILL_ABILITY
from .debug_capture import capture
from .improvisation import DIFFICULTIES
from .intent import Intent


class PlanError(ValueError):
    """A concrete, player-readable reason; contains no hidden campaign facts."""


def plans(g):
    return getattr(g, "_action_plans", None) or {}


def current_plan(g, actor):
    plan = plans(g).get(actor)
    return plan if plan and plan["status"] not in ("completed", "failed", "cancelled") else None


def _text(value, label, limit=160):
    if type(value) is not str or not value.strip() or len(value) > limit:
        raise PlanError(f"請補充明確的{label}。")
    return value.strip()


def parse_plan(raw, g, actor, revision, text):
    """Validate dependency shape before consulting the creative specialists."""
    if len(text) > 1500:
        raise PlanError("計畫描述最多 1500 字，請保留一個主要目的與最多三個步驟。")
    if type(raw) is not dict or set(raw) != {"relationship", "goal", "steps"}:
        raise PlanError("未能確認各步驟的共同目的，行動尚未執行。請說明想用甚麼方法達成甚麼目標。")
    relationship = raw["relationship"]
    if relationship in ("independent", "ambiguous"):
        raise PlanError("請先選一個目的，例如「用某物攻擊某目標」或「拾起藥水救某隊友」；行動尚未執行。")
    if relationship == "hypothetical":
        raise PlanError("這是假設或否定的行動，尚未執行。若要實際嘗試，請直接說明目的和方法。")
    if relationship not in ("linked", "single"):
        raise PlanError("未能確認行動關係，行動尚未執行。")
    steps = raw["steps"]
    if type(steps) is not list or not steps:
        raise PlanError("未能解析出可執行的步驟；請說明行動的先後次序。")
    if len(steps) > 3:
        raise PlanError("一次計畫最多三個相依步驟；請縮短到一個主要目的。")
    if (relationship == "single" and len(steps) != 1) or (relationship == "linked" and len(steps) < 2):
        raise PlanError("步驟數與行動關係不一致，沒有執行部分行動。")
    goal = raw["goal"]
    if goal not in ("attack", "use", "talk"):
        raise PlanError("目前計畫支援拾取、製作後攻擊、拾取後使用物品，或觀察後交談；其他行動請分開提出。")
    expected = {"take": {"action", "item"},
                "prepare": {"action", "method", "materials"},
                "attack": {"action", "target", "item"},
                "use": {"action", "target", "item"},
                "observe": {"action", "target"},
                "talk": {"action", "target", "utterance"}}
    steps = deepcopy(steps)
    for step in steps:
        if type(step) is not dict or type(step.get("action")) is not str \
                or step["action"] not in expected or set(step) != expected[step["action"]]:
            raise PlanError("計畫包含尚未支援的步驟，行動尚未執行。")
        action = step["action"]
        if action == "prepare":
            step["method"] = _text(step["method"], "製作方法")
            materials = step["materials"]
            if type(materials) is not list or not 1 <= len(materials) <= 2 \
                    or any(type(n) is not str or not n or len(n) > 100 for n in materials) \
                    or len(set(materials)) != len(materials):
                raise PlanError("製作需要一至兩種明確材料，每種消耗一份。")
            if any(n not in text for n in materials):
                raise PlanError("製作提案加入了玩家未指定的材料，行動尚未執行。")
        elif action in ("observe", "talk"):
            step["target"] = _text(step["target"], "對象", 100)
            if action == "talk":
                step["utterance"] = _text(step["utterance"], "要說的話", 500)
                if step["utterance"] not in text:
                    raise PlanError("交談內容必須保留玩家原話，不能新增問題或承諾。")
        else:
            if type(step["item"]) is not str or len(step["item"]) > 100:
                raise PlanError("請指定要使用或拾取的物品。")
            if action == "take" and (not step["item"] or step["item"] not in text):
                raise PlanError("請指定要拾取的物品，不能替玩家選取未提及的物資。")
            if action in ("attack", "use"):
                if type(step["target"]) is not str or len(step["target"]) > 100:
                    raise PlanError("請指定一個明確目標。")
                if step["item"] and step["item"] != "$prepared" and step["item"] not in text:
                    raise PlanError("提案使用了玩家未指定的物品，行動尚未執行。")
    actions = [s["action"] for s in steps]
    # Exactly one final goal, with a strictly ordered dependency chain.
    if goal == "talk":
        if actions != ["observe", "talk"] or steps[0]["target"] != steps[1]["target"]:
            raise PlanError("觀察後交談必須保持同一對象與原本次序；沒有執行部分行動。")
    elif actions[-1] != goal or any(a not in ("take", "prepare") for a in actions[:-1]) \
            or actions.count("take") > 1 or actions.count("prepare") > 1 \
            or ("take" in actions and actions[0] != "take"):
        raise PlanError("請保留一個主要目的及其必要前置步驟，不會自動挑其中一個行動執行。")
    preparation = next((s for s in steps if s["action"] == "prepare"), None)
    pickup = next((s["item"] for s in steps if s["action"] == "take"), "")
    final = steps[-1]
    if preparation:
        if goal != "attack" or final["item"] != "$prepared":
            raise PlanError("製作物必須用於這次攻擊，不能改成一般攻擊或其他效果。")
        if pickup and pickup not in preparation["materials"]:
            raise PlanError("拾取物品與製作沒有依賴關係，請先選一個目的。")
    elif final.get("item") == "$prepared" or (pickup and final.get("item") != pickup):
        raise PlanError("拾取物品必須用於最後的行動，不能省略指定的方法。")
    if goal == "use" and not final["item"]:
        raise PlanError("請指定要使用的物品。")
    identifier = hashlib.sha256(json.dumps([actor, revision, text], ensure_ascii=False).encode()).hexdigest()[:12]
    plan = {"version": 1, "id": identifier, "actor": actor, "source_revision": revision,
            "origin_scene": g.world.current, "goal": goal, "player_words": text[:1500],
            "steps": steps, "next_step": 0, "status": "ready", "stop_reason": "",
            "preparation": None, "prepared_item": ""}
    preflight(g, plan)
    return plan


def approve_preparation(raw):
    if type(raw) is not dict or set(raw) != {"method", "skill", "difficulty", "template"}:
        raise PlanError("製作提案格式不完整，沒有消耗材料。")
    if type(raw["skill"]) is not str or raw["skill"] not in SKILL_ABILITY \
            or type(raw["difficulty"]) is not str or raw["difficulty"] not in DIFFICULTIES \
            or raw["template"] != "improvised_throw":
        raise PlanError("製作提案超出允許的檢定或投擲物效果，沒有消耗材料。")
    _text(raw["method"], "製作方法")
    return deepcopy(raw)


def review_result(raw):
    """Reviews may explain a refusal, but cannot append new plan mechanics."""
    if type(raw) is not dict or set(raw) - {"approved", "reason"} \
            or type(raw.get("approved")) is not bool \
            or type(raw.get("reason", "")) is not str or len(raw.get("reason", "")) > 240:
        raise PlanError("計畫審核回覆不完整，沒有執行部分行動。")
    return raw["approved"], raw.get("reason", "").strip()


def preflight(g, plan):
    """Check the whole remaining chain without mutating inventory or RNG."""
    actor = plan["actor"]
    if actor not in g.party or not g.alive(actor):
        raise PlanError("角色已倒地或無法行動；計畫暫停。")
    if g.world.current != plan["origin_scene"]:
        raise PlanError("場景已改變；原計畫暫停，請在目前場景重新提出行動。")
    final = plan["steps"][-1]
    target = final["target"]
    if plan["goal"] == "attack":
        foe = g.scene_enemies().get(target)
        npc = next((n for n in g.scene_npcs() if n["name"] == target), None)
        if not (foe and not foe.dead and foe.hp > 0) and npc is None:
            raise PlanError(f"目前場景找不到可攻擊的「{target}」；尚未攻擊。請重新選擇目標。")
    elif plan["goal"] == "talk":
        if target not in {n["name"] for n in g.scene_npcs()}:
            raise PlanError(f"目前場景找不到交談對象「{target}」；原計畫暫停。")
    elif target and target not in g.party:
        raise PlanError(f"找不到使用物品的隊友「{target}」。")
    available = Counter({n: q for n, q in g.inventory.get(actor, [])})
    for step in plan["steps"][plan["next_step"]:]:
        action = step["action"]
        if action == "take":
            item = step["item"]
            if not any(n == item and q > 0 for n, q in g.world.here.ground_items):
                raise PlanError(f"地上沒有可拾取的「{item}」；計畫尚未執行。可用 `/scene` 查看實際物品。")
            available[item] += 1
        elif action == "prepare":
            for item in step["materials"]:
                if available[item] < 1:
                    raise PlanError(f"缺少材料「{item}」；尚未製作或攻擊。要改變方法，請提出新的行動。")
                available[item] -= 1
            available["$prepared"] += 1
        elif step.get("item"):
            item = plan["prepared_item"] if step["item"] == "$prepared" and plan["prepared_item"] else step["item"]
            if available[item] < 1:
                raise PlanError(f"沒有可使用的「{item}」；不會改用普通攻擊。")


def describe(plan):
    labels = []
    for i, step in enumerate(plan["steps"]):
        action = step["action"]
        label = (f"拾取 {step['item']}" if action == "take" else step["method"] if action == "prepare"
                 else f"觀察 {step['target']}" if action == "observe"
                 else f"向 {step['target']} 問：{step['utterance']}" if action == "talk"
                 else f"{'攻擊' if action == 'attack' else '使用物品於'} {step['target'] or plan['actor']}")
        mark = "✓" if i < plan["next_step"] else "…"
        labels.append(f"{mark} {label}")
    return "📋 計畫：" + " → ".join(labels)


def _event(g, plan, outcome, **details):
    data = {"plan_id": plan["id"], "step": plan["next_step"], "status": plan["status"], **details}
    g.ledger.add(plan["actor"], "action_plan", outcome, **data)
    capture("action_plan", outcome=outcome, **data)


def _pause(g, plan, reason):
    plan["status"], plan["stop_reason"] = "blocked", reason
    _event(g, plan, reason)
    return ["⏸ " + reason]


def resolve_plan(g, plan, *, resume=False):
    from .rules_core import ResolveResult, resolve, ability_check, _post_rotation
    actor = plan["actor"]
    if not resume:
        if not hasattr(g, "_action_plans"):
            g._action_plans = {}
        previous = current_plan(g, actor)
        if previous:
            previous["status"] = "cancelled"
            _event(g, previous, "玩家提出新計畫，原計畫的剩餘步驟不再執行。")
        g._action_plans[actor] = plan = deepcopy(plan)
        _event(g, plan, "開始相依行動計畫")
    if plan["status"] == "awaiting_check":
        return ResolveResult(["🎯 製作尚待檢定，請先用 `/roll d20`。"])
    if plan["status"] in ("completed", "failed", "cancelled"):
        return ResolveResult(["這個計畫已結束；沒有重複執行。"])
    try:
        preflight(g, plan)
    except PlanError as exc:
        return ResolveResult(_pause(g, plan, str(exc)))
    plan["status"], plan["stop_reason"] = "ready", ""
    lines = [describe(plan)]
    # One explicit ground pickup is an object interaction alongside this action.
    step = plan["steps"][plan["next_step"]]
    if step["action"] == "take":
        item = step["item"]
        for i, (name, qty) in enumerate(g.world.here.ground_items):
            if name == item and qty > 0:
                if qty == 1:
                    g.world.here.ground_items.pop(i)
                else:
                    g.world.here.ground_items[i] = (name, qty - 1)
                g.give_item(actor, name, 1)
                lines.append(f"🎒 {actor} 拾起 {name}×1（物件互動）")
                _event(g, plan, lines[-1], item=name)
                plan["next_step"] += 1
                break
        step = plan["steps"][plan["next_step"]]
    if step["action"] == "prepare":
        template = plan["preparation"]
        # The service only admits a plan after all required reviews complete.
        approve_preparation(template)
        g.ledger.next_turn()
        plan["status"] = "awaiting_check"
        for item in step["materials"]:
            g.take_item(actor, item, 1)
            g.ledger.add(actor, "item", f"{actor} 消耗 {item}×1", item=item, qty=-1)
        _event(g, plan, "開始製作，消耗材料及一次行動；尚未攻擊。")
        lines.append("🧪 製作消耗一次行動；攻擊另需一次行動，不會自動執行。")
        _, line = ability_check(g, actor, SKILL_ABILITY[template["skill"]],
                                DIFFICULTIES[template["difficulty"]], template["skill"],
                                auto=g.combat.active, effect={"kind": "linked_prepare", "plan_id": plan["id"]})
        lines.append(line)
        if g.combat.active:
            lines += _post_rotation(g, actor)
        return ResolveResult(lines)
    item = plan["prepared_item"] if step.get("item") == "$prepared" else step.get("item", "")
    if step["action"] == "attack" and item:
        # The standard engine throw owns AC, DEX, dice, death, loot and rotation.
        it = Intent("attack", actor=actor, target=step["target"],
                    args={"thrown_item": item})
    else:
        it = Intent(step["action"], actor=actor, target=step["target"], item=item,
                    utterance=step.get("utterance", ""))
    result = resolve(g, it)
    if not result.accepted:
        # The caller's staged transaction rolls back the pickup as well.
        return ResolveResult(result.lines, accepted=False)
    if step["action"] == "use" and g.combat.active:
        # Legacy item use has no rotation of its own. A plan still spends an
        # action; never let pickup/healing chains bypass the per-round slot.
        result.lines += _post_rotation(g, actor)
    plan["next_step"] += 1
    if plan["next_step"] < len(plan["steps"]):
        plan["status"] = "ready"
        _event(g, plan, "觀察完成，等待玩家繼續交談。")
        return ResolveResult(lines + result.lines + ["尚未交談；用 `/confirm` 或 `/plan resume` 繼續，`/plan cancel` 取消。"])
    plan["status"] = "completed"
    _event(g, plan, "計畫最後一步已執行。")
    return ResolveResult(lines + result.lines)


def settle_preparation(g, actor, plan_id, ok, lines):
    plan = plans(g).get(actor)
    if not plan or plan["id"] != plan_id or plan["status"] != "awaiting_check":
        return
    if g.world.current != plan["origin_scene"] or actor not in g.party or not g.alive(actor):
        lines += _pause(g, plan, "製作條件已改變，混合物未完成；已消耗的材料不會重複扣除。")
        plan["status"] = "failed"
        return
    if not ok:
        plan["status"] = "failed"
        plan["stop_reason"] = "製作失敗，材料已消耗；沒有製成投擲物，也沒有攻擊。"
        lines.append("❌ " + plan["stop_reason"])
        _event(g, plan, plan["stop_reason"])
        return
    number = 1 + sum(e.kind == "action_plan" and bool(e.data.get("crafted_item"))
                     for e in g.ledger.entries)
    existing = {n for stacks in g.inventory.values() for n, _ in stacks}
    existing.update(n for scene in g.world.scenes.values() for n, _ in scene.ground_items)
    item = f"混合投擲瓶（{number}）"
    while item in existing:
        number += 1
        item = f"混合投擲瓶（{number}）"
    g.give_item(actor, item, 1)
    if not hasattr(g, "_prepared_items"):
        g._prepared_items = {}
    g._prepared_items[item] = {"template": "improvised_throw", "method": plan["preparation"]["method"]}
    plan["prepared_item"] = item
    plan["next_step"] += 1
    plan["status"] = "ready"
    lines.append(f"🧪 製作成功：{item}（一次性，投擲命中造成 1d4 傷害）。尚未攻擊；"
                 "下一次可行動時用 `/confirm` 或 `/plan resume` 繼續，亦可用 `/plan cancel` 取消。")
    _event(g, plan, "製作成功，等待玩家繼續攻擊。", item=item, crafted_item=True)


def plan_status(g, actor):
    plan = plans(g).get(actor)
    if not plan:
        return ["（沒有已保存的行動計畫）"]
    labels = {"ready": "等待繼續", "awaiting_check": "等待製作檢定", "blocked": "暫停",
              "failed": "失敗", "completed": "已完成", "cancelled": "已取消"}
    return [describe(plan), "狀態：" + labels.get(plan["status"], plan["status"]),
            plan["stop_reason"] or ("用 `/roll d20` 結算製作。" if plan["status"] == "awaiting_check"
                else "用 `/confirm` 或 `/plan resume` 繼續，`/plan cancel` 取消。" if current_plan(g, actor) else "")]
