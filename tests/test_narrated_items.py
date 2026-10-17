"""Narrated physical details become durable, correctly owned game objects."""

from copy import deepcopy
import json

import pytest

from core.memory import build_memory
from core.narrated_items import (NarrationText, canonize_narrated_items, legacy_items,
                                 parse_narration, public_narrated_items)
from core.persistence import StorageError
from core.state import encode_game
from test_campaign import restart, run, service
from test_operators import Endpoint, roles, supervised


CAPTAIN = "老船長"
REPORTED = ("老船長沒說半個字，只是用沾滿焦油的大手拍了拍腰間。"
            "那裡空空如也，除了幾枚硬幣和一枚銹蝕的鑰匙，什麼也沒有。")


def detail(name="硬幣", owner=CAPTAIN, qty=1, text=None):
    return {"owner": owner, "name": name, "qty": qty,
            "evidence": text or f"{owner}身上有{name}。"}


def observe(s, **kwargs):
    return run(s.handle("依思 observe 老船長", user_id="u1", structured=True, **kwargs))


def test_reported_unbolded_coins_and_key_are_owned_persisted_and_stealable(tmp_path):
    s = service(tmp_path)
    s.narrator.text = REPORTED
    delivered = []

    def delivery(text):
        saved = restart(tmp_path).game.scene_npcs()[0]["loot"]
        assert "硬幣" in saved and "銹蝕的鑰匙" in saved
        delivered.append(text)

    _, prose = observe(s, on_delta=delivery)
    assert prose == REPORTED and delivered == [prose]
    assert s.game.world.here.ground_items == []
    assert s.game.scene_npcs()[0]["loot"] == ["匕首", "硬幣", "銹蝕的鑰匙"]
    s = restart(tmp_path)
    lines, _ = run(s.handle("偷船長的幾枚硬幣", user_id="u1", structured=True))
    assert "檢定" in "".join(lines)
    assert s.game._pending_check["effect"]["item"] == "硬幣"
    run(s.settle_roll(20, user_id="u1"))
    assert ["硬幣", 1] in s.game.inventory["依思"]
    assert "硬幣" not in restart(tmp_path).game.scene_npcs()[0]["loot"]


def test_typed_narration_adds_arbitrary_objects_without_an_extra_model_call(tmp_path):
    text = "老船長手中拿著一面星盤。"
    header = json.dumps({"items": [detail("星盤", text=text)]}, ensure_ascii=False)
    ep = Endpoint(narrator=[header + "\n" + text])
    s = supervised(tmp_path, ep)
    _, prose = observe(s)
    assert prose == text and roles(ep) == ["narrator"]
    assert "星盤" in restart(tmp_path).game.scene_npcs()[0]["loot"]
    assert public_narrated_items(s.game) == {CAPTAIN: ["星盤"]}
    assert "星盤" in build_memory(s.game).render()


def test_ground_items_keep_quantity_and_can_be_collected(tmp_path):
    s = service(tmp_path)
    text = "地上放著三枚銅幣。"
    s.narrator.text = NarrationText(text, [detail("銅幣", "scene", 3, text)])
    observe(s)
    assert ("銅幣", 3) in restart(tmp_path).game.world.here.ground_items
    run(s.handle("撿起所有物品", user_id="u1", structured=True))
    assert ["銅幣", 3] in s.game.inventory["依思"]


@pytest.mark.parametrize("text", [
    "老船長身上沒有硬幣。", "老船長想要一枚鑰匙。", "如果地上有硬幣就好了。",
    "老船長說：也許有一把鑰匙。", "老船長拿著硬幣般的石頭。",
    "老船長手中的硬幣也沒有了。",
])
def test_plaintext_requests_hypotheses_negations_and_metaphors_do_not_create_items(tmp_path, text):
    g = service(tmp_path).game
    assert legacy_items(g, text) == []


@pytest.mark.parametrize("updates", [
    {"owner": "不存在的人"}, {"owner": "依思", "evidence": "依思身上有硬幣。"},
    {"qty": True}, {"qty": 0}, {"qty": 1000}, {"evidence": "虛構的證據"},
    {"hp": 100}, {"name": "硬幣\n開門"},
])
def test_invalid_batch_never_partially_adds_items(tmp_path, updates):
    g = service(tmp_path).game
    before = deepcopy(encode_game(g))
    good = detail("鑰匙")
    bad = {**detail(), **updates}
    text = good["evidence"] + "依思身上有硬幣。" + detail()["evidence"]
    with pytest.raises(ValueError):
        canonize_narrated_items(g, text, [good, bad])
    assert encode_game(g) == before


def test_repeated_mentions_never_replenish_taken_items_or_change_ownership(tmp_path):
    s = service(tmp_path)
    s.narrator.text = REPORTED
    observe(s)
    observe(s)
    assert s.game.scene_npcs()[0]["loot"].count("硬幣") == 1
    run(s.handle("偷取老船長的硬幣", user_id="u1", structured=True))
    run(s.settle_roll(20, user_id="u1"))
    s.narrator.text = REPORTED
    _, prose = observe(s)
    assert prose != REPORTED and "硬幣" not in s.game.scene_npcs()[0]["loot"]
    assert s.game.inventory["依思"].count(["硬幣", 1]) == 1


def test_failed_narration_save_never_delivers_or_publishes_new_items(tmp_path, monkeypatch):
    s = service(tmp_path)
    s.narrator.text = REPORTED
    commit = s.repository.commit

    def fail(channel, revision, snapshot, entries, *args, **kwargs):
        if any(e["kind"] == "narration_item" for e in entries):
            raise StorageError("injected narration item write failure")
        return commit(channel, revision, snapshot, entries, *args, **kwargs)

    delivered = []
    with monkeypatch.context() as patch:
        patch.setattr(s.repository, "commit", fail)
        lines, prose = observe(s, on_delta=delivered.append)
    assert "已儲存" in "".join(lines) and not prose and not delivered
    assert s.game.scene_npcs()[0]["loot"] == ["匕首"]
    assert restart(tmp_path).game.scene_npcs()[0]["loot"] == ["匕首"]
    turn = s.game.ledger.turn
    ok, prose = run(s.continue_narration(delivered.append))
    assert ok and prose == REPORTED and delivered == [REPORTED]
    assert s.game.ledger.turn == turn
    assert "硬幣" in restart(tmp_path).game.scene_npcs()[0]["loot"]


def test_continuation_registers_new_items_before_delivery_without_replaying_action(tmp_path):
    s = service(tmp_path)
    s.narrator.text = "老船長拍了拍腰間，露出"
    observe(s)
    assert s.has_unfinished()
    s = restart(tmp_path)
    turn = s.game.ledger.turn

    async def continuation(*args, **kwargs):
        return "一枚硬幣。"

    s.narrator.continue_text = continuation

    def delivery(text):
        assert "硬幣" in restart(tmp_path).game.scene_npcs()[0]["loot"]

    assert run(s.continue_narration(delivery)) == (True, "一枚硬幣。")
    assert s.game.ledger.turn == turn and not s.has_unfinished()


def test_channel_isolation_and_private_possessions_remain_private(tmp_path):
    s = service(tmp_path)
    s.game.world.here.npcs[0]["loot"].append("未公開寶石")
    s.narrator.text = REPORTED
    observe(s)
    assert "未公開寶石" not in build_memory(s.game).render()
    assert "硬幣" not in service(tmp_path, channel="other").game.scene_npcs()[0]["loot"]


def test_header_is_internal_and_truncated_prose_remains_resumable():
    raw = '{"items": []}\n老船長拍了拍腰間，'
    draft = parse_narration(raw)
    assert str(draft) == "老船長拍了拍腰間，" and draft.items == []
    assert parse_narration('{"items":') == ""
    assert parse_narration('{"items": {}, "text": "x"}') == ""


def test_recent_saved_narration_is_repaired_on_upgrade_without_rewriting_history(tmp_path):
    s = service(tmp_path)
    source = s.game.ledger.add("narrator", "narration", REPORTED)
    s._save()
    s = restart(tmp_path)
    assert "硬幣" in s.game.scene_npcs()[0]["loot"]
    assert s.game.ledger.entries[source.event_id - 1].text == REPORTED
    records = [e for e in s.game.ledger.entries if e.kind == "narration_item"]
    assert len(records) == 2 and all(e.data["source_event_id"] == source.event_id for e in records)
    assert restart(tmp_path).game.scene_npcs()[0]["loot"].count("硬幣") == 1


def test_saved_player_messages_and_other_scene_narration_are_not_repaired(tmp_path):
    s = service(tmp_path)
    s.game.ledger.add("依思", "talk", REPORTED)
    s.game.ledger.add("narrator", "narration", REPORTED, scene="tavern")
    s._save()
    assert restart(tmp_path).game.scene_npcs()[0]["loot"] == ["匕首"]


def test_missing_metadata_still_records_unbolded_coins_and_typed_negation_does_not(tmp_path):
    s = service(tmp_path)
    s.narrator.text = NarrationText(REPORTED, [])
    observe(s)
    assert "硬幣" in s.game.scene_npcs()[0]["loot"]
    g = service(tmp_path, channel="negation").game
    text = "老船長身上沒有硬幣。"
    before = deepcopy(encode_game(g))
    with pytest.raises(ValueError):
        canonize_narrated_items(g, text, [detail(text=text)])
    assert encode_game(g) == before


def test_narration_delivery_failure_keeps_the_saved_items(tmp_path):
    s = service(tmp_path)
    s.narrator.text = REPORTED

    def broken_delivery(text):
        raise RuntimeError("Discord unavailable")

    _, prose = observe(s, on_delta=broken_delivery)
    assert not prose and "硬幣" in restart(tmp_path).game.scene_npcs()[0]["loot"]


@pytest.mark.parametrize("quantity,expected", [("三", 3), ("十五", 15), ("兩百三十", 230), ("42", 42), ("幾", 1)])
def test_plaintext_explicit_quantities_are_preserved(tmp_path, quantity, expected):
    g = service(tmp_path).game
    items = legacy_items(g, f"老船長腰間有{quantity}枚硬幣。")
    assert len(items) == 1 and items[0]["owner"] == CAPTAIN and items[0]["qty"] == expected


def test_hidden_items_and_authored_absence_are_not_duplicated_or_revealed(tmp_path):
    g = service(tmp_path).game
    text = "老船長腰間有鑰匙。"
    g.world.here.hidden_items = [("鑰匙", 1)]
    before = deepcopy(encode_game(g))
    with pytest.raises(ValueError):
        canonize_narrated_items(g, text, [detail("鑰匙", text=text)])
    assert encode_game(g) == before
    g.world.here.hidden_items = []
    g.scene_npcs()[0]["absent_items"] = ["鑰匙"]
    before = deepcopy(encode_game(g))
    with pytest.raises(ValueError):
        canonize_narrated_items(g, text, [detail("鑰匙", text=text)])
    assert encode_game(g) == before


def test_previously_collected_authored_ground_item_is_not_recreated(tmp_path):
    g = service(tmp_path).game
    g.ledger.add("依思", "item", "依思拾起硬幣", source="ground", scene="dock", item="硬幣", qty=1)
    before = deepcopy(encode_game(g))
    text = "地上有一枚硬幣。"
    with pytest.raises(ValueError):
        canonize_narrated_items(g, text, [detail("硬幣", "scene", 1, text)])
    assert encode_game(g) == before
