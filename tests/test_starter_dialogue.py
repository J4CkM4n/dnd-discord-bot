"""Starter NPC facts, exhausted dialogue, and repair of existing campaigns."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from core.cli import build_demo_game, seed_starter_knowledge
from core.intent import Intent
from core.memory import build_memory
from core.rules_core import resolve, resolve_pending_check
from core.state import encode_game
from core.templates import render_hint
from test_campaign import Narrator, run, service, restart


def talk(g):
    return resolve(g, Intent("talk", actor="依思", target="老船長",
                             utterance="我想打聽附近的消息"))


def test_first_conversation_reveals_one_concrete_route_then_warning():
    g = build_demo_game(seed=1)
    npc = g.world.here.npcs[0]
    assert npc.get("disclosed", []) == []
    before = build_memory(g, npc_name="老船長").render()
    assert "前往時要小心" not in before
    result = talk(g)
    assert result.accepted
    assert "碼頭北邊的小路通往破舊酒館。" in "\n".join(result.lines)
    assert len(npc["disclosed"]) == 1
    assert g.ledger.entries[-1].data["information"] == "revealed"
    assert "前往時要小心" not in build_memory(g, npc_name="老船長").render()
    talk(g)
    assert len(npc["disclosed"]) == 2
    assert "前往時要小心" in npc["disclosed"][1]


@pytest.mark.parametrize("knows,disclosed,status,expected", [
    ([], [], "unavailable", "暫時沒有可提供的情報"),
    (["已知傳聞"], ["已知傳聞"], "exhausted", "沒有新情報"),
])
def test_no_information_gives_honest_reply_and_public_next_step(knows, disclosed, status, expected):
    g = build_demo_game(seed=1)
    g.world.here.npcs[0].update(knows=knows, disclosed=disclosed)
    before = len(g.ledger.entries)
    result = talk(g)
    text = "\n".join(result.lines)
    assert expected in text and "前往「破舊酒館」" in text and "/explore" in text
    assert not any(e.kind == "reveal" for e in g.ledger.entries[before:])
    entry = g.ledger.entries[-1]
    assert entry.data["information"] == status
    hint = render_hint(entry, g)
    assert "願意一談" not in hint and "開口回應" not in hint
    assert "情報" in hint
    if not knows:
        assert "已把知道的都說了" not in text


def test_social_roll_has_same_no_information_reply_and_no_fake_reveal():
    g = build_demo_game(seed=1)
    g.party["依思"]["stats"]["CHA"] = 1
    g.world.here.npcs[0].update(knows=[], disposition="wary")
    talk(g)
    assert g._pending_check is not None
    assert g.ledger.entries[-1].data["information"] == "pending"
    assert "尚未作出答覆" in render_hint(g.ledger.entries[-1], g)
    ok, text = resolve_pending_check(g, 20)
    assert ok and "暫時沒有可提供的情報" in text and "前往「破舊酒館」" in text
    assert g.ledger.entries[-1].data["information"] == "unavailable"
    assert not any(e.kind == "reveal" for e in g.ledger.entries)


def test_passive_persuasion_discloses_once_and_records_information_status():
    g = build_demo_game(seed=1)
    npc = g.world.here.npcs[0]
    npc.update(knows=["公開消息"], disposition="wary")
    result = talk(g)
    assert "無需擲骰" in "\n".join(result.lines)
    assert npc["disclosed"] == ["公開消息"]
    assert g.ledger.entries[-1].data["information"] == "revealed"
    assert sum(e.kind == "reveal" for e in g.ledger.entries) == 1
    npc["disposition"] = "wary"
    result = talk(g)
    assert "沒有新情報" in "\n".join(result.lines)
    assert g.ledger.entries[-1].data["information"] == "exhausted"
    assert sum(e.kind == "reveal" for e in g.ledger.entries) == 1


def test_saved_campaign_repairs_missing_starter_knowledge_once(tmp_path):
    s = service(tmp_path)
    npc = s.game.world.here.npcs[0]
    npc.pop("knows")
    npc["disclosed"] = ["原有的公開消息"]
    s.game.party["依思"]["hp_now"] = 5
    s.game.ledger.add("engine", "item", "原有歷史")
    s._save()
    original = deepcopy(encode_game(s.game))
    history = [asdict(e) for e in s.game.ledger.entries]
    revision = s._revision
    fresh = restart(tmp_path)
    repaired = encode_game(fresh.game)
    saved_npc = repaired["world"]["scenes"]["dock"]["npcs"][0]
    assert saved_npc["knows"] and saved_npc["disclosed"] == ["原有的公開消息"]
    saved_npc.pop("knows")
    assert repaired == original
    assert [asdict(e) for e in fresh.game.ledger.entries] == history
    assert fresh._revision == revision + 1
    again = restart(tmp_path)
    assert again._revision == fresh._revision
    assert again.game.world.here.npcs[0]["knows"]


@pytest.mark.parametrize("existing", [[], ["自訂消息"]])
def test_explicit_authored_npc_knowledge_is_not_overwritten(tmp_path, existing):
    s = service(tmp_path)
    s.game.world.here.npcs[0].update(knows=existing, disclosed=existing.copy())
    s._save()
    fresh = restart(tmp_path)
    assert fresh.game.world.here.npcs[0]["knows"] == existing
    assert fresh.game.world.here.npcs[0]["disclosed"] == existing
    assert fresh._revision == s._revision


def test_repair_does_not_add_removed_or_custom_npc():
    g = build_demo_game(seed=1)
    g.world.here.npcs = []
    assert not seed_starter_knowledge(g)
    assert not g.world.here.npcs
    g.world.here.npcs = [{"name": "老船長", "desc": "自訂船長"}]
    assert not seed_starter_knowledge(g)
    assert "knows" not in g.world.here.npcs[0]


def test_no_model_fallback_explains_exhaustion_and_next_step(tmp_path):
    s = service(tmp_path)
    s.game.world.here.npcs[0].update(knows=["已公開情報"], disclosed=["已公開情報"])
    s.narrator = Narrator(text="")
    lines, prose = run(s.handle("依思 talk 老船長 還有消息嗎", user_id="u1", structured=True))
    assert "沒有新情報" in "\n".join(lines)
    assert "已沒有新情報" in prose and "前往「破舊酒館」" in prose
    directive = s.narrator.calls[-1][1]["extra_directive"]
    assert "這次交談沒有新情報" in directive
    assert "至少一項" not in directive


def test_repaired_campaign_discloses_starter_fact_on_next_conversation(tmp_path):
    s = service(tmp_path)
    s.game.world.here.npcs[0].pop("knows")
    s._save()
    fresh = restart(tmp_path)
    lines, _ = run(fresh.handle("依思 talk 老船長 我想打聽附近的消息", user_id="u1", structured=True))
    assert "碼頭北邊的小路通往破舊酒館。" in "\n".join(lines)
    assert fresh.game.world.here.npcs[0]["disclosed"] == ["碼頭北邊的小路通往破舊酒館。"]
    loaded = restart(tmp_path)
    assert loaded.game.world.here.npcs[0]["disclosed"] == fresh.game.world.here.npcs[0]["disclosed"]
