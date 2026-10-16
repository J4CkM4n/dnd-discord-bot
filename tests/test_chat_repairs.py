"""Regressions drawn from the eight exported multiplayer chat sessions."""
import asyncio
from copy import deepcopy

import pytest

from core.campaign import NECKLACE, sync_starter
from core.cli import build_demo_game
from core.director import ambient_find, named_find, try_materialize
from core.hints import progress_key
from core.intent import Intent, parse_command
from core.plot import load_plot, new_state
from core.rules_core import resolve, ability_check
from core.state import encode_game
from core.persistence import StorageError
from core.templates import render_hint
from core.world import Enemy
from test_campaign import service, restart, run


@pytest.mark.parametrize("verb", ["執起", "拾起", "拿起", "pick up"])
def test_bare_pickup_cannot_materialize_verb_fragment(verb):
    g = build_demo_game(1)
    it = parse_command(verb, g.party)
    it.actor = "依思"
    assert it.item == ""
    before = deepcopy(encode_game(g))
    assert not resolve(g, it).accepted
    assert encode_game(g) == before
    g.world.here.ground_items = [("木塞", 1)]
    assert resolve(g, it).accepted
    assert ["木塞", 1] in g.inventory["依思"]
    assert not any(n in ("起", "出木塞") for n, _ in g.inventory["依思"])


def test_bare_pickup_asks_when_several_objects_present():
    g = build_demo_game(1)
    g.world.here.ground_items = [("木塞", 1), ("陶杯", 2)]
    it = parse_command("依思 執起", g.party)
    result = resolve(g, it)
    assert not result.accepted and "木塞、陶杯" in "".join(result.lines)
    assert len(g.world.here.ground_items) == 2


@pytest.mark.parametrize("text", ["拿出木塞", "檢查杯", "觀察杯"])
def test_object_reads_do_not_search_or_consume(tmp_path, text):
    s = service(tmp_path)
    s.game.give_item("依思", "木塞", 1)
    s.game.give_item("依思", "黏著魚鱗的陶杯", 1)
    s.game.world.here.hidden_items = [("隱藏鐵牌", 1)]
    s._save()
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(text, user_id="u1"))
    assert "🔎" in "".join(lines)
    assert encode_game(s.game) == before
    assert not s.director.calls


def test_ambiguous_alias_and_missing_inspection_are_atomic(tmp_path):
    s = service(tmp_path)
    s.game.give_item("依思", "陶杯", 1)
    s.game.give_item("依思", "玻璃杯", 1)
    before = deepcopy(encode_game(s.game))
    for text in ("檢查杯", "檢查生鏽的鐵牌"):
        lines, _ = run(s.handle(text, user_id="u1"))
        assert lines[0].startswith("🚫")
        assert encode_game(s.game) == before


@pytest.mark.parametrize("text", ["給石頭大力蕉", "給大力蕉石頭", "把石頭給大力蕉", "give 石頭 大力蕉"])
def test_natural_transfer_moves_stack_to_recipient(tmp_path, text):
    s = service(tmp_path)
    s.game.give_item("依思", "圓石頭", 2)
    lines, _ = run(s.handle(text, user_id="u1"))
    assert "→ 大力蕉" in "".join(lines)
    assert ["圓石頭", 1] in s.game.inventory["依思"]
    assert ["圓石頭", 1] in s.game.inventory["大力蕉"]


@pytest.mark.parametrize("qty", [-1, 0, None, True, 1.5, "2", 21])
def test_invalid_inventory_quantity_never_spends_or_rolls(qty):
    g = build_demo_game(1)
    before = deepcopy(encode_game(g))
    for action in ("use", "give"):
        it = Intent(action, actor="依思", item="治療藥水", qty=qty, args={"to": "大力蕉"})
        assert not resolve(g, it).accepted
        assert encode_game(g) == before


@pytest.mark.parametrize("tail", ["-1", "null", "0"])
def test_text_quantity_is_validated(tmp_path, tail):
    s = service(tmp_path)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(f"用藥水 {tail}", user_id="u1"))
    assert "數量" in "".join(lines)
    assert encode_game(s.game) == before


def test_group_healing_preflight_and_combat_action_cost(tmp_path):
    s = service(tmp_path)
    s.game.party["依思"]["hp_now"] = 1
    s.game.party["大力蕉"]["hp_now"] = 0
    lines, _ = run(s.handle("用藥水幫大家補血", user_id="u1"))
    assert s.game.party["依思"]["hp_now"] > 1
    assert s.game.party["大力蕉"]["hp_now"] > 0
    assert not s.game.inventory["依思"]
    assert {e.data["target"] for e in s.game.ledger.entries if e.kind == "heal"} == set(s.game.party)
    s.game.give_item("依思", "治療藥水", 1)
    before = deepcopy(encode_game(s.game))
    run(s.handle("用藥水給大家", user_id="u1"))
    assert encode_game(s.game) == before
    s.game.world.current = "tavern"
    s.game.start_encounter()
    before = deepcopy(encode_game(s.game))
    run(s.handle("用藥水給大家", user_id="u1"))
    assert encode_game(s.game) == before
    run(s.handle("用藥水 大力蕉", user_id="u1"))
    assert "依思" in s.game.combat.acted
    assert not s.game.inventory["依思"]
    before = deepcopy(encode_game(s.game))
    assert not resolve(s.game, Intent("attack", actor="依思", target="哥布林①")).accepted
    assert encode_game(s.game) == before


def test_unknown_healing_target_and_inert_use_keep_inventory():
    g = build_demo_game(1)
    before = deepcopy(encode_game(g))
    assert not resolve(g, Intent("use", actor="依思", item="藥水", target="失蹤隊友")).accepted
    assert encode_game(g) == before
    g.give_item("依思", "木塞")
    assert resolve(g, Intent("use", actor="依思", item="木塞")).accepted
    assert ["木塞", 1] in g.inventory["依思"]


def test_pending_check_allows_reads_and_owner_can_cancel_after_restart(tmp_path):
    s = service(tmp_path)
    ability_check(s.game, "依思", "DEX", 18, auto=False)
    s.game.give_item("大力蕉", "陶杯")
    s._unfinished = s._narration_job(0, "舊檢定", False)
    unfinished = deepcopy(s._unfinished)
    s._save()
    before = deepcopy(s.game._pending_check)
    lines, _ = run(s.handle("檢查杯", user_id="u2"))
    assert "🔎" in "".join(lines) and s.game._pending_check == before
    assert s._unfinished == unfinished
    lines, _ = run(s.handle("大力蕉 搜索", user_id="u2", structured=True))
    assert "未執行" in "".join(lines) and s.game._pending_check == before
    run(s.action_plan("cancel", "u2"))
    assert s.game._pending_check == before
    restored = restart(tmp_path)
    run(restored.action_plan("cancel", "u1"))
    assert restored.game._pending_check is None
    run(restored.settle_roll(20, user_id="u1"))
    assert restored.game._pending_check is None


def test_give_adapter_obeys_pending_life_and_turn_guards(tmp_path):
    s = service(tmp_path)
    s.game.give_item("依思", "石頭", 2)
    ability_check(s.game, "大力蕉", "DEX", 18, auto=False)
    before = deepcopy(encode_game(s.game))
    assert "未執行" in run(s.transfer_item("u1", "石頭", "大力蕉"))
    assert encode_game(s.game) == before
    s.game._pending_check = None
    s.game.party["依思"]["hp_now"] = 0
    before = deepcopy(encode_game(s.game))
    assert "倒地" in run(s.transfer_item("u1", "石頭", "大力蕉"))
    assert encode_game(s.game) == before


def test_duplicate_interactions_are_durable_and_serialized(tmp_path):
    s = service(tmp_path)
    s.game.give_item("依思", "石頭", 5)
    s._save()
    async def retry():
        return await asyncio.gather(*(s.transfer_item("u1", "石頭", "大力蕉", 2, request_id="one") for _ in range(2)))
    results = run(retry())
    assert results[0] == results[1]
    assert ["石頭", 3] in s.game.inventory["依思"]
    assert ["石頭", 2] in s.game.inventory["大力蕉"]
    restored = restart(tmp_path)
    assert run(restored.transfer_item("u1", "石頭", "大力蕉", 2, request_id="one")) == results[0]
    assert restored.game.inventory == s.game.inventory


def test_transfer_commit_failure_and_retry_are_atomic(tmp_path, monkeypatch):
    s = service(tmp_path)
    s.game.give_item("依思", "石頭", 3)
    s._save()
    before = deepcopy(encode_game(s.game))
    commit = s.repository.commit
    def fail(*args, **kwargs):
        raise StorageError("injected disk failure")
    monkeypatch.setattr(s.repository, "commit", fail)
    assert "行動未執行" in run(s.transfer_item("u1", "石頭", "大力蕉", 2, request_id="disk"))
    assert encode_game(s.game) == before
    monkeypatch.setattr(s.repository, "commit", commit)
    run(s.transfer_item("u1", "石頭", "大力蕉", 2, request_id="disk"))
    assert ["石頭", 1] in s.game.inventory["依思"]
    assert ["石頭", 2] in restart(tmp_path).game.inventory["大力蕉"]


def test_interruption_after_commit_cannot_repeat_mechanics(tmp_path, monkeypatch):
    s = service(tmp_path)
    s.game.give_item("依思", "石頭", 3)
    s._save()
    commit = s.repository.commit
    count = 0
    def fail_after_commit(*args, **kwargs):
        nonlocal count
        count += 1
        if count > 1:
            raise StorageError("lost narration storage")
        return commit(*args, **kwargs)
    monkeypatch.setattr(s.repository, "commit", fail_after_commit)
    run(s.transfer_item("u1", "石頭", "大力蕉", 2, request_id="interrupted"))
    restored = restart(tmp_path)
    before = deepcopy(encode_game(restored.game))
    text = run(restored.transfer_item("u1", "石頭", "大力蕉", 2, request_id="interrupted"))
    assert "不會重複執行" in text
    assert encode_game(restored.game) == before


def test_admin_can_cancel_an_absent_players_standalone_check(tmp_path):
    s = service(tmp_path)
    ability_check(s.game, "依思", "DEX", 18, auto=False)
    s._save()
    run(s.action_plan("cancel", "moderator", admin=True))
    assert s.game._pending_check is None


@pytest.mark.parametrize("stale", ["character", "scene"])
def test_stale_pending_checks_expire_on_load(tmp_path, stale):
    s = service(tmp_path)
    ability_check(s.game, "依思", "DEX", 18, auto=False)
    s.game._pending_check["scene"] = "dock"
    if stale == "character":
        del s.game.party["依思"]
    else:
        s.game.world.current = "tavern"
    s._save()
    restored = restart(tmp_path)
    assert restored.game._pending_check is None
    assert any(e.kind == "check_expired" for e in restored.game.ledger.entries)
    assert not run(restored.settle_roll(20, user_id="u1"))[1]


def test_narrator_receives_one_name_map_and_only_guarded_text_is_delivered(tmp_path):
    s = service(tmp_path)
    s.game.party["大力蕉"]["hp_now"] = 1
    delivered, inputs = [], []
    async def narrate(*args, on_delta=None, **kwargs):
        inputs.append((args, kwargs))
        on_delta("UNGUARDED DRAFT")
        assert not delivered
        return "[PC1]扶著[PC2]，讓藥水慢慢發揮作用。"
    s.narrator.narrate = narrate
    run(s.handle("用藥水 大力蕉", user_id="u1", on_delta=delivered.append))
    args, kwargs = inputs[0]
    assert set(kwargs["party_inventory"]) == {"[PC1]", "[PC2]"}
    assert "大力蕉" not in str(args) + str(kwargs)
    assert delivered == ["依思扶著大力蕉，讓藥水慢慢發揮作用。"]


def test_inventory_audit_backs_up_wal_and_excludes_admin_grants(tmp_path):
    import json
    import sqlite3
    from tools.audit_inventory import audit
    s = service(tmp_path)
    s.game.give_item("依思", "起", 1)
    s.game.give_item("依思", "出木塞", 50)
    for item in ("起", "出木塞"):
        s.game.ledger.add("director", "materialize", "known artifact", item=item)
        s.game.ledger.add("依思", "item", "依思 拾起 " + item, item=item, qty=1)
    s.game.ledger.add("admin", "item", "管理員給了 50 個出木塞", item="出木塞")
    s._save()
    before = s.repository.load("t")
    report = json.loads(audit(s.repository.path, tmp_path / "review").read_text(encoding="utf-8"))
    assert report["read_only"]
    assert report["channels"][0]["candidates"][0]["item"] == "起"
    assert len(report["channels"][0]["candidates"]) == 1
    with sqlite3.connect(report["backup"]) as backup:
        snapshot = json.loads(backup.execute("SELECT snapshot FROM channels WHERE channel_id='t'").fetchone()[0])
    assert snapshot == before["snapshot"]
    assert s.repository.load("t") == before


def test_search_and_prop_generation_are_bounded_after_consumption():
    g = build_demo_game(1)
    for _ in range(100):
        name = ambient_find(g)
        if name:
            break
    assert name
    g.world.here.hidden_items.clear()
    assert all(ambient_find(g) is None for _ in range(100))
    assert not named_find(g, "木塞")
    assert not try_materialize(g, "依思", "起")
    assert not try_materialize(g, "依思", "出木塞")
    for name in ("陶杯", "木塞", "紙張", "布條"):
        try_materialize(g, "依思", name)
    assert sum(e.kind == "materialize" for e in g.ledger.entries) == 3


def test_decorative_loot_and_scene_loops_do_not_reset_progress():
    g = build_demo_game(1)
    key = progress_key(g, {})
    g.give_item("依思", "陶杯")
    g.world.current = "tavern"
    assert progress_key(g, {"clocks": {"queen": 30}, "last_turn": 20}) == key


def test_heal_and_pending_narrative_hints_keep_resolved_identity():
    g = build_demo_game(1)
    result = resolve(g, Intent("use", actor="依思", item="藥水", target="大力蕉"))
    assert result.accepted
    entry = next(e for e in reversed(g.ledger.entries) if e.kind == "heal")
    assert "大力蕉" in render_hint(entry, g) and "依思" not in render_hint(entry, g)
    resolve(g, Intent("creative", actor="依思", utterance="嘗試翻過障礙", args={"dc": 18}))
    entry = next(e for e in reversed(g.ledger.entries) if e.kind == "creative")
    assert entry.data["ok"] is None and "失敗" not in entry.text
    assert "尚未決定成敗" in render_hint(entry, g)


def test_cleared_tavern_recovers_old_save_and_does_not_restart_empty_combat(tmp_path):
    s = service(tmp_path)
    for foe in s.game.encounters["tavern"]:
        foe.dead, foe.hp = True, 0
    s._save()
    restored = restart(tmp_path)
    assert "cellar" in restored.game.world.scenes["tavern"].exits
    lines, _ = run(restored.handle("依思 前往破舊酒館", user_id="u1", structured=True))
    assert "戰鬥開始" not in "".join(lines) and not restored.game.combat.active
    assert "哥布林已被擊退" in "".join(lines)
    count = len(restored.game.world.scenes)
    restored._save()
    again = restart(tmp_path)
    assert len(again.game.world.scenes) == count
    assert "酒館酒窖" in again.nudge()


def test_campaign_sync_cannot_respawn_an_authored_npc():
    g = build_demo_game(1)
    for foe in g.encounters["tavern"]:
        foe.dead, foe.hp = True, 0
    sync_starter(g)
    dock = g.world.scenes["dock"]
    assert any(n["name"] == "海妖女王" for n in dock.npcs)
    dock.npcs = [n for n in dock.npcs if n["name"] != "海妖女王"]
    g.ledger.add("依思", "death", "海妖女王 倒下", target="海妖女王")
    sync_starter(g)
    assert not any(n["name"] == "海妖女王" for n in dock.npcs)


def test_offline_three_player_campaign_reaches_authored_ending(tmp_path):
    s = service(tmp_path)
    s.game.party["第三位"] = deepcopy(s.game.party["依思"])
    s.game.party["第三位"].update(owner_id="u3", hp_now=100, hp_max=100)
    s.game.inventory["第三位"] = []
    for n in s.game.party:
        s.game.party[n].update(hp_now=100, hp_max=100)
    s.plot = load_plot()
    s.plot_state = new_state(s.plot)
    # Pin dice rather than replacing combat or awarding quest items.
    s.game._random.seed(38)
    run(s.handle("前往破舊酒館", user_id="u1", structured=True))
    for destination in ("酒館酒窖", "海上·甲板", "海底祭壇"):
        for _ in range(80):
            if not s.game.combat.active:
                break
            for uid in ("u1", "u2", "u3"):
                live = [n for n, f in s.game.enemies.items() if not f.dead and f.hp > 0]
                if live:
                    run(s.handle("攻擊 " + live[0], user_id=uid, structured=True))
        assert not s.game.combat.active
        assert destination in "、".join(s.game.world.here.exits.values())
        run(s.handle("前往" + destination, user_id="u3", structured=True))
    run(s.handle("說 巴鐸，我們來取回項鍊", user_id="u2", structured=True))
    if getattr(s.game, "_pending_check", None):
        run(s.settle_roll(20, user_id="u2"))
    assert (NECKLACE, 1) in s.game.world.here.ground_items
    run(s.handle("拾起" + NECKLACE, user_id="u3", structured=True))
    for dest in ("海上·甲板", "酒館酒窖", "破舊酒館", "碼頭"):
        run(s.handle("前往" + dest, user_id="u1", structured=True))
    assert s.plot_state["ended"] == "queen_alliance"
    assert s.game._starter_progress["completed"]
    assert not any(n == NECKLACE for stacks in s.game.inventory.values() for n, _ in stacks)
    captain = next(n for n in s.game.world.here.npcs if n["name"] == "老船長")
    assert "已完成約定" in captain["knows"][0]
    before = deepcopy(encode_game(s.game))
    assert not run(s.continue_narration())[0]
    assert encode_game(s.game) == before
