"""Regressions from discord-1560644714074611874.json; no live models needed."""
from copy import deepcopy
import sqlite3
import sys

import pytest

from core.campaign import QUEEN_REQUEST, QUEEN_COMPLETED, sync_starter
from core.director import DirectorProposal, verify_proposal, canonize_scene
from core.intent import Intent, parse_command
from core.objects import parse_object_command, inventory_text
from core.rules_core import resolve, ability_check
from core.rules_reference import rules_text
from core.state import encode_game
from core.world import Scene, World
from test_campaign import service, restart, run


@pytest.mark.parametrize("method,field", [("拋向", "thrown_item"), ("掟向", "thrown_item"),
                                          ("敲一下", "weapon_item"), ("攻擊", "weapon_item")])
def test_item_attack_keeps_item_and_target_and_executes_without_models(tmp_path, method, field):
    s = service(tmp_path)
    s.game.give_item("依思", "圓石頭")
    text = f"用石頭{method}大力蕉"
    intent = parse_command(text, s.game.party)
    assert intent.action == "attack" and intent.target == "大力蕉"
    assert intent.args[field] == "石頭"
    lines, _ = run(s.handle(text, user_id="u1"))
    assert "🎲" in "".join(lines)
    assert any(e.kind == "attack" and e.actor == "依思" and e.data["target"] == "大力蕉" for e in s.game.ledger.entries)
    assert not s.director.calls
    if field == "weapon_item":
        assert ["圓石頭", 1] in s.game.inventory["依思"]
    else:
        assert not any(n == "圓石頭" for n, _ in s.game.inventory["依思"])


@pytest.mark.parametrize("text", ["用石頭拋向大力蕉", "用石頭敲一下大力蕉"])
def test_missing_or_ambiguous_weapon_never_falls_back_or_spends_dice(tmp_path, text):
    s = service(tmp_path)
    before = deepcopy(encode_game(s.game))
    rng = s.game._random.getstate()
    lines, _ = run(s.handle(text, user_id="u1"))
    assert "不會改用普通攻擊" in "".join(lines)
    assert encode_game(s.game) == before and s.game._random.getstate() == rng
    s.game.give_item("依思", "圓石頭")
    s.game.give_item("依思", "尖石頭")
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(text, user_id="u1"))
    assert "多個物品" in "".join(lines) and encode_game(s.game) == before


@pytest.mark.parametrize("text", ["不要用石頭拋向大力蕉", "用石頭拋向大力蕉然後逃跑", "如果用石頭敲一下大力蕉？",
                                 "用石頭練習敲大力蕉", "用石頭敲大力蕉但不傷害他", "用石頭拋向大力蕉，逃跑"])
def test_item_attack_grammar_does_not_execute_qualified_prefix(text):
    assert parse_object_command(text, ["依思", "大力蕉"]) is None


@pytest.mark.parametrize("text", ["查看持有物", "身上有咩", "我身上有啲咩？", "inventory", "查看背包"])
def test_inventory_questions_are_reads_even_during_pending_checks(tmp_path, text):
    s = service(tmp_path)
    ability_check(s.game, "大力蕉", "STR", 20, auto=False)
    s._unfinished = s._narration_job(0, "等待檢定")
    unfinished = deepcopy(s._unfinished)
    before = deepcopy(encode_game(s.game))
    lines, narration = run(s.handle(text, user_id="u1"))
    assert lines == [inventory_text(s.game, "依思")]
    assert "治療藥水" in lines[0] and "魔能爆" in lines[0]
    assert not narration and s._unfinished == unfinished
    assert encode_game(s.game) == before


@pytest.mark.parametrize("destination", ["海上甲板", "海上 · 甲板", "海上．甲板", "海上·甲板"])
def test_existing_scene_typography_never_invokes_generation(tmp_path, destination):
    s = service(tmp_path)
    s.game.world.add_scene(Scene("sea", "海上·甲板", exits={"dock": "霧錨鎮碼頭"}))
    s.game.world.here.exits["sea"] = "海上·甲板"
    before = set(s.game.world.scenes)
    lines, _ = run(s.handle(f"前往{destination}", user_id="u1", structured=True))
    assert s.game.world.current == "sea" and set(s.game.world.scenes) == before
    assert not s.director.calls and "場景轉移" in lines[0]
    proposal = DirectorProposal("scene", "sea", {"name": "海上甲板", "description": "小艇的甲板。", "npc": ""})
    assert verify_proposal(s.game, proposal) == "duplicate_scene"
    assert not canonize_scene(s.game, proposal.content)


def test_exit_matching_uses_exact_label_before_partial_and_rejects_ambiguity(tmp_path):
    w = service(tmp_path).game.world
    w.add_scene(Scene("cellar", "酒館酒窖"))
    w.here.exits = {"cellar": "酒館酒窖", "tavern": "酒館"}
    assert w.find_exit("酒館") == "tavern"
    w.here.exits["tavern"] = "酒館大廳"
    assert w.find_exit("酒館") is None


def test_merged_exit_labels_remain_independent_aliases_and_reject_ambiguity():
    w = World({"start": Scene("start", "起點", exits={"tower": "前往高塔\n沿階梯上樓"}),
               "tower": Scene("tower", "高塔"), "garden": Scene("garden", "花園")}, "start")
    assert w.find_exit("前往高塔") == w.find_exit("沿階梯上樓") == "tower"
    w.here.exits["garden"] = "前往高塔"
    assert w.find_exit("前往高塔") is None
    assert w.find_exit("沿階梯上樓") == "tower"
    assert w.find_exit("tower") == "tower"
    w.here.exits["tower"] = "前往高塔\n\n沿階梯上樓"
    assert w.find_exit("不存在的出口") is None


def test_social_skill_keeps_teammate_and_never_reveals_captain_information(tmp_path):
    from test_operators import Endpoint, roles, supervised
    endpoint = Endpoint()
    s = supervised(tmp_path, endpoint)
    lines, _ = run(s.handle("依思 技能 欺瞞 大力蕉", user_id="u1"))
    assert lines
    assert not s.game.world.here.npcs[0].get("disclosed")
    assert not any(e.kind == "reveal" for e in s.game.ledger.entries)
    assert any(e.kind == "skill" and e.data["target"] == "大力蕉" for e in s.game.ledger.entries)
    assert roles(endpoint) == ["narrator"]


@pytest.mark.parametrize("skill", ["欺瞞", "醫藥", "洞察"])
def test_unknown_skill_target_is_rejected_without_state_or_rng_changes(tmp_path, skill):
    s = service(tmp_path)
    before = deepcopy(encode_game(s.game))
    rng = s.game._random.getstate()
    lines, _ = run(s.handle(f"技能 {skill} 不存在的人", user_id="u1"))
    assert "重新指定目標" in "".join(lines)
    assert encode_game(s.game) == before and s.game._random.getstate() == rng


def test_medicine_does_not_switch_explicit_healthy_target_to_wounded_ally(tmp_path):
    s = service(tmp_path)
    s.game.party["依思"]["hp_now"] = 0
    result = resolve(s.game, Intent("skill", actor="大力蕉", skill="medicine", target="大力蕉"))
    assert result.accepted
    assert s.game.ledger.entries[-1].data["target"] == "大力蕉"
    assert s.game.party["依思"]["hp_now"] == 0


@pytest.mark.parametrize("draft", ["[PC2]仔細搜索了碼頭。", "老船長蹲下翻找，找到一些海藻。"])
def test_wrong_narrated_actor_is_replaced_before_delivery_and_persistence(tmp_path, draft):
    s = service(tmp_path)
    s.game.world.here.hidden_items = [("海藻", 1)]
    s.narrator.text = draft
    delivered = []
    lines, narration = run(s.handle("依思 搜索", user_id="u1", structured=True, on_delta=delivered.append))
    assert lines and narration and "依思" in narration
    assert "大力蕉" not in narration and "老船長蹲下" not in narration
    assert delivered == [narration]
    assert all(e.text != draft for e in restart(tmp_path).game.ledger.entries if e.kind == "narration")


def test_continuation_cannot_transfer_search_to_another_actor(tmp_path):
    s = service(tmp_path)
    s.game.world.here.hidden_items = [("海藻", 1)]
    s.narrator.text = "[PC1]蹲下搜索，仔細"
    _, narration = run(s.handle("依思 搜索", user_id="u1", structured=True))
    assert narration == "依思蹲下搜索，仔細" and s.has_unfinished()
    s = restart(tmp_path)
    before = deepcopy(encode_game(s.game))
    unfinished = deepcopy(s._unfinished)
    delivered = []

    async def wrong_actor(*args, **kwargs):
        return "查看。老船長翻找並搜到了海藻。"

    s.narrator.continue_text = wrong_actor
    assert run(s.continue_narration(delivered.append)) == (False, "")
    assert not delivered and encode_game(s.game) == before and s._unfinished == unfinished

    async def correct_actor(*args, **kwargs):
        return "查看，終於找到海藻。"

    s.narrator.continue_text = correct_actor
    assert run(s.continue_narration(delivered.append)) == (True, "查看，終於找到海藻。")
    assert not s.has_unfinished() and delivered == ["查看，終於找到海藻。"]


def test_valid_npc_reaction_and_explicit_healing_target_remain_allowed(tmp_path):
    s = service(tmp_path)
    s.game.party["大力蕉"]["hp_now"] = 1
    s.narrator.text = "[PC1]扶住[PC2]，讓藥水慢慢發揮作用。老船長在旁點頭。"
    _, narration = run(s.handle("用藥水 大力蕉", user_id="u1"))
    assert narration == "依思扶住大力蕉，讓藥水慢慢發揮作用。老船長在旁點頭。"


def test_completed_queen_information_is_current_after_restart_and_not_repeated(tmp_path):
    s = service(tmp_path)
    s.game._starter_progress = {"tavern": True, "guard": True, "completed": True}
    s.game.world.here.npcs.append({"name": "海妖女王", "disposition": "friendly", "knows": [QUEEN_REQUEST], "disclosed": [QUEEN_REQUEST]})
    s.game.ledger.add("依思", "reveal", QUEEN_REQUEST, npc="海妖女王", fact=QUEEN_REQUEST)
    sync_starter(s.game)
    s._save()
    s = restart(tmp_path)
    for _ in range(2):
        lines, _ = run(s.handle("說 海妖女王，你好", user_id="u1", structured=True))
        assert QUEEN_REQUEST not in "".join(lines)
        assert QUEEN_REQUEST not in s.narrator.calls[-1][1]["npc_knows"]
        s = restart(tmp_path)
    reveals = [e.data["fact"] for e in s.game.ledger.entries if e.kind == "reveal"]
    assert reveals == [QUEEN_REQUEST, QUEEN_COMPLETED]


def test_new_character_has_equipment_once_and_default_attack_matches_class(tmp_path):
    s = service(tmp_path)
    text = run(s.create_character("劍聖", "Paladin", "u3", "Samuel"))
    assert "起始裝備：長劍、盾牌" in text
    assert restart(tmp_path).game.inventory["劍聖"] == [["長劍", 1], ["盾牌", 1]]
    resolve(s.game, Intent("attack", actor="劍聖", target="大力蕉"))
    assert any(e.kind == "attack" and "長劍斬擊" in e.text for e in s.game.ledger.entries)


@pytest.mark.parametrize("text", ["買刀", "武器屋を探す"])
def test_unavailable_starter_shop_gives_equipment_guidance_without_review(tmp_path, text):
    s = service(tmp_path)
    before = deepcopy(encode_game(s.game))
    lines, _ = run(s.handle(text, user_id="u1"))
    assert "沒有武器商店" in lines[0] and "/inventory" in lines[0]
    assert not s.director.calls and encode_game(s.game) == before


@pytest.mark.parametrize("query", ["哥布林", "火焰箭", "治療藥水", "休息"])
def test_missing_srd_database_has_useful_offline_rules_without_creating_files(tmp_path, query):
    path = tmp_path / "missing.db"
    text = rules_text(query, path)
    assert query in text and "遊戲規則" in text and "完整 SRD 尚未載入" in text
    assert "unable to open" not in text and not path.exists()


def test_srd_database_with_uri_characters_is_read_only_and_preferred(tmp_path):
    path = tmp_path / "SRD #1 規則.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE chunks(title, kind, text)")
        conn.execute("INSERT INTO chunks VALUES ('Goblin', 'monster', 'Imported SRD content')")
    text = rules_text("哥布林", path)
    assert "SRD 查詢" in text and "Imported SRD content" in text
    assert "本遊戲實作" not in text


def test_offline_srd_import_needs_no_embedding_server(tmp_path, monkeypatch):
    from tools.ingest_srd import main
    source = tmp_path / "srd"
    source.mkdir()
    (source / "Goblin.md").write_text("# Goblin\n" + "A small creature described in this local SRD source. " * 4, encoding="utf-8")
    db = tmp_path / "data" / "rules.db"
    monkeypatch.setattr(sys, "argv", ["ingest_srd.py", str(source), str(db), "--no-embeddings"])
    main()
    assert "small creature" in rules_text("哥布林", db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT length(vec) FROM chunks").fetchone()[0] == 0
