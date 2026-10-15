"""Offline integration tests for durable turns, director gates, and recall."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict

import pytest

from core.cli import build_demo_game
from core.director import MAX_DIRECTOR_SCENES
from core.director_llm import DirectorLLM
from core.intent import Intent
from core.memory import build_memory, MAX_CHARS, MAX_FACTS
from core.persistence import StorageError
from core.service import V4Service
from core.state import encode_game
from core.world import Enemy


class Narrator:
    def __init__(self, text="霧中傳來細碎的腳步聲。"):
        self.text = text
        self.calls = []

    async def narrate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.text

    async def continue_text(self, partial, on_delta=None, context=""):
        self.calls.append((partial, context))
        return "腳步聲漸漸遠去。"


class Director:
    def __init__(self):
        self.scene = {"name": "霧中小巷", "description": "石板上覆著潮濕的青苔。", "npc": ""}
        self.item = {"name": "破舊木雕"}
        self.calls = []
        self.approve = True

    async def gen_scene(self, *args, **kwargs):
        self.calls.append(("scene", args, kwargs))
        return deepcopy(self.scene)

    async def gen_item(self, *args, **kwargs):
        self.calls.append(("item", args, kwargs))
        return deepcopy(self.item)

    async def evaluate(self, *args):
        self.calls.append(("evaluate", args))
        return self.approve, "fits" if self.approve else "conflicts"


def service(tmp_path, channel="t", evaluate=False):
    s = V4Service(str(tmp_path), "http://unused", channel_id=channel,
                  director_evaluate=evaluate)
    s.game._random.seed(1)
    for n, uid in (("依思", "u1"), ("大力蕉", "u2")):
        s.game.party[n]["owner_id"] = uid
    s.plot = {}
    s.plot_state = {"clocks": {}, "fired": [], "last_turn": 0, "ended": None}
    s.rules = {}
    s.director = Director()
    s.narrator = Narrator()
    s._save()
    return s


def restart(tmp_path, channel="t"):
    s = V4Service(str(tmp_path), "http://unused", channel_id=channel)
    s.plot = {}
    s.rules = {}
    s.director = Director()
    s.narrator = Narrator()
    return s


def run(awaitable):
    return asyncio.run(awaitable)


def decisions(s):
    with s.repository.connect() as conn:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM director_decisions WHERE channel_id=? ORDER BY id", (s.channel_id,))]


@pytest.mark.parametrize("text", ["依思 go 小巷", "依思 搜索"])
@pytest.mark.parametrize("failure", ["ownership", "downed", "acted", "missing"])
def test_invalid_actions_cannot_generate(tmp_path, text, failure):
    s = service(tmp_path)
    uid = "u1"
    if failure == "ownership":
        uid = "u2"
    elif failure == "downed":
        s.game.party["依思"]["hp_now"] = 0
    elif failure == "missing":
        del s.game.party["依思"]
    else:
        s.game.enemies = {"敵人": Enemy.make("敵人", 10, 10)}
        s.game.combat.order = [{"name": "依思", "npc": False}]
        s.game.combat.acted = ["依思"]
    s._save()
    before = encode_game(s.game)
    lines, _ = run(s.handle(text, user_id=uid, structured=True))
    assert lines and "🚫" in lines[0]
    assert not s.director.calls
    assert encode_game(s.game) == before
    assert encode_game(restart(tmp_path).game) == before


def test_new_scene_is_committed_before_callback_and_narration(tmp_path):
    s = service(tmp_path, evaluate=True)
    callbacks = []

    async def verdict(lines):
        restored = restart(tmp_path)
        assert restored.game.world.current == "dir1"
        assert restored.has_unfinished()
        assert s.game.world.current == "dir1"
        callbacks.append(lines)

    run(s.handle("依思 go 小巷", user_id="u1", structured=True, on_resolved=verdict))
    assert callbacks
    assert [c[0] for c in s.director.calls] == ["scene", "evaluate"]
    assert s.game.world.here.exits == {"dock": "霧錨鎮碼頭"}
    assert s.game.world.scenes["dock"].exits["dir1"] == "霧中小巷"
    assert decisions(s)[0]["accepted"] is True
    assert {e.turn for e in s.game.ledger.entries if e.kind in ("director_scene", "scene")} == {1}
    assert not restart(tmp_path).has_unfinished()
    assert "石板" in s.narrator.calls[0][1]["memory"]


@pytest.mark.parametrize("failure", ["evaluation", "timeout", "bad_shape", "duplicate"])
def test_scene_rejection_has_no_world_side_effect(tmp_path, failure):
    s = service(tmp_path, evaluate=True)
    if failure == "evaluation":
        s.director.approve = False
    elif failure == "timeout":
        async def timeout(*args):
            raise TimeoutError()
        s.director.evaluate = timeout
    elif failure == "bad_shape":
        s.director.scene["name"] = ["wrong type"]
    else:
        s.director.scene["name"] = s.game.world.here.name
    before = encode_game(s.game)
    run(s.handle("依思 go 小巷", user_id="u1", structured=True))
    assert encode_game(s.game) == before
    assert not decisions(s)[0]["accepted"]
    assert not any(e.kind == "director_scene" for e in s.game.ledger.entries)


def test_existing_exit_and_cap_skip_generation(tmp_path):
    s = service(tmp_path)
    s.game.director_scene_count = MAX_DIRECTOR_SCENES
    s._save()
    run(s.handle("依思 go 小巷", user_id="u1", structured=True))
    assert not s.director.calls
    assert decisions(s)[0]["reasons"] == ["scene_limit"]
    run(s.handle("依思 go 破舊酒館", user_id="u1", structured=True))
    assert not s.director.calls
    assert s.game.world.current == "tavern"


def test_authored_discovery_skips_director(tmp_path):
    s = service(tmp_path)
    s.game.world.here.hidden_items = [("繩索", 1)]
    s._save()
    run(s.handle("依思 搜索", user_id="u1", structured=True))
    assert not s.director.calls
    assert ["繩索", 1] in s.game.inventory["依思"]


def test_blocked_director_item_cannot_be_conjured(tmp_path):
    s = service(tmp_path)
    s.director.item = {"name": "魔法劍"}
    run(s.handle("依思 搜索", user_id="u1", structured=True))
    assert s.director.calls[0][0] == "item"
    assert decisions(s)[0]["reasons"] == ["blocked_item"]
    assert "魔法劍" not in json.dumps(encode_game(s.game), ensure_ascii=False)


def test_failed_commit_rolls_back_database_and_live_state(tmp_path):
    s = service(tmp_path)
    before = s.repository.load("t")
    with s.repository.connect() as conn:
        conn.execute("CREATE TRIGGER fail_save BEFORE INSERT ON events "
                     "BEGIN SELECT RAISE(ABORT, 'injected disk failure'); END")
    posted = []

    async def callback(lines):
        posted.append(lines)

    lines, narr = run(s.handle("依思 go 小巷", user_id="u1", structured=True, on_resolved=callback))
    assert "未能儲存" in lines[0] and not narr
    assert not posted and s.game.world.current == "dock"
    assert s.repository.load("t") == before
    assert decisions(s) == []
    with s.repository.connect() as conn:
        conn.execute("DROP TRIGGER fail_save")
    run(s.handle("依思 go 小巷", user_id="u1", structured=True))
    assert s.game.world.current == "dir1"
    assert [e.event_id for e in s.game.ledger.entries] == list(range(1, len(s.game.ledger.entries) + 1))


@pytest.mark.parametrize("failure", ["callback", "narrator", "narration_commit", "cancel"])
def test_committed_turn_survives_delivery_failures(tmp_path, failure):
    s = service(tmp_path)
    callback = None
    if failure == "callback":
        async def callback(lines):
            raise RuntimeError("Discord offline")
    elif failure in ("narrator", "cancel"):
        async def broken(*args, **kwargs):
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise RuntimeError("Ollama offline")
        s.narrator.narrate = broken
    else:
        real_commit = s.repository.commit
        def fail_narration(channel, revision, snapshot, *args, **kwargs):
            if snapshot is None:
                raise StorageError("disk unavailable")
            return real_commit(channel, revision, snapshot, *args, **kwargs)
        s.repository.commit = fail_narration
    action = s.handle("依思 go 小巷", user_id="u1", structured=True, on_resolved=callback)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            run(action)
    else:
        run(action)
    restored = restart(tmp_path)
    assert restored.game.world.current == s.game.world.current == "dir1"
    assert restored.has_unfinished() and s.has_unfinished()
    turn = restored.game.ledger.turn
    success, text = run(restored.continue_narration())
    assert success and text
    assert restored.game.ledger.turn == turn
    assert sum(e.kind == "director_scene" for e in restored.game.ledger.entries) == 1
    assert not restart(tmp_path).has_unfinished()


def test_confirmation_survives_restart_and_is_bound_to_player(tmp_path):
    s = service(tmp_path)

    class Digest:
        async def digest(self, *args, **kwargs):
            return Intent("move", actor="依思", destination="小巷", confidence=0.5)

    s.digestor = Digest()
    run(s.handle("我想去小巷", user_id="u1"))
    assert not s.director.calls and s.pending
    restored = restart(tmp_path)
    assert restored.pending and restored.pending_user_id == "u1"
    run(restored.confirm("u2"))
    assert restored.pending and not restored.director.calls
    run(restored.confirm("u1"))
    assert restored.pending is None
    assert restored.game.world.current == "dir1"
    assert len(restored.director.calls) == 1
    run(restored.confirm("u1"))
    assert len(restored.director.calls) == 1


def test_pending_check_survives_restart_and_enforces_owner(tmp_path):
    s = service(tmp_path)
    s.game.world.here.search_dc = 20
    s.game.world.here.hidden_items = [("繩索", 1)]
    s._save()
    run(s.handle("依思 搜索", user_id="u1", structured=True))
    restored = restart(tmp_path)
    pending = deepcopy(restored.game._pending_check)
    assert pending["user_id"] == "u1" and pending["scene"] == "dock"
    run(restored.settle_roll(20, user_id="u2"))
    run(restored.settle_roll(20, user_id="u1", expected_event_id=-1))
    run(restored.settle_roll(0, user_id="u1"))
    assert restored.game._pending_check == pending
    run(restored.handle("依思 go 小巷", user_id="u1", structured=True))
    assert not restored.director.calls and restored.game.world.current == "dock"
    run(restored.settle_roll(20, user_id="u1", expected_event_id=pending["event_id"]))
    assert restored.game._pending_check is None
    assert ["繩索", 1] in restored.game.inventory["依思"]
    restored = restart(tmp_path)
    run(restored.settle_roll(20, user_id="u1"))
    assert restored.game.inventory["依思"].count(["繩索", 1]) == 1


def test_snapshot_preserves_effects_rng_and_active_enemy_identity(tmp_path):
    s = service(tmp_path)
    g = s.game
    g._aid, g._dodge, g._stealth, g._help_check = ({"依思": True} for _ in range(4))
    g.world.current = "tavern"
    g.start_encounter()
    g.enemies["哥布林①"].hp = 2
    g.combat.acted = ["依思"]
    s.plot_state["fired"] = ["a_beat"]
    s._save()
    restored = restart(tmp_path)
    assert encode_game(restored.game) == encode_game(s.game)
    assert restored.plot_state["fired"] == ["a_beat"]
    assert restored.game.enemies["哥布林①"] is restored.game.encounters["tavern"][0]
    assert restored.game.roll("2d6+2") == s.game.roll("2d6+2")


def test_truncated_narration_resumes_without_replaying_action(tmp_path):
    s = service(tmp_path)
    s.narrator.text = "霧中傳來"
    run(s.handle("依思 go 小巷", user_id="u1", structured=True))
    restored = restart(tmp_path)
    job = deepcopy(restored._unfinished)
    assert "idx0" not in job and job["start_event_id"] > 0
    # Unrelated durable admin events must not change the continuation boundary.
    run(restored.admin_give("依思", "火把"))
    assert restored._unfinished == job
    success, text = run(restored.continue_narration())
    assert success and "腳步" in text
    assert restored.game.ledger.turn == 1
    assert restored.game.ledger.entries[-1].kind == "narration"
    assert not restart(tmp_path).has_unfinished()


def test_same_channel_serializes_but_other_channels_progress(tmp_path):
    s = service(tmp_path)
    other = service(tmp_path, "other")

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(*args, **kwargs):
            started.set()
            await release.wait()
            return "霧散了。"

        s.narrator.narrate = slow
        first = asyncio.create_task(s.handle("依思 go 小巷", user_id="u1", structured=True))
        await started.wait()
        queued = asyncio.create_task(s.admin_give("依思", "火把"))
        await other.admin_give("依思", "木雕")
        assert not queued.done()
        assert ["木雕", 1] in other.game.inventory["依思"]
        assert ["木雕", 1] not in s.game.inventory["依思"]
        release.set()
        await asyncio.gather(first, queued)

    run(scenario())
    assert ["火把", 1] in restart(tmp_path).game.inventory["依思"]
    assert restart(tmp_path, "other").game.world.current == "dock"


def test_json_import_is_lossless_idempotent_and_preserves_original(tmp_path):
    g = build_demo_game(seed=7)
    g.ledger.add("engine", "scene", "在碼頭", scene="dock")
    g.ledger.add("narrator", "narration", "未完成")
    blob = encode_game(g)
    blob["v"] = 6
    blob.pop("effects")
    blob.pop("rng")
    blob.pop("director_scene_count")
    blob["entries"] = [{k: v for k, v in asdict(e).items() if k != "event_id"}
                       for e in g.ledger.entries]
    blob["unfinished"] = {"idx0": 0, "text": "未完成", "player_input": "去碼頭"}
    path = tmp_path / "v4_state_t.json"
    path.write_text(json.dumps(blob, ensure_ascii=False))
    original = path.read_bytes()
    s = restart(tmp_path)
    assert s.game.world.current == "dock" and len(s.game.ledger.entries) == 2
    assert s.has_unfinished()
    assert s._unfinished["start_event_id"] == 1
    revision = s._revision
    assert restart(tmp_path)._revision == revision
    assert path.read_bytes() == original
    with s.repository.connect() as conn:
        assert conn.execute("SELECT imported_from FROM channels").fetchone()[0] == str(path)


@pytest.mark.parametrize("data", ['{"v":99}', '{"v":6,"world":{}}', '{broken'])
def test_bad_import_is_not_replaced_with_demo(tmp_path, data):
    path = tmp_path / "v4_state_t.json"
    path.write_text(data)
    with pytest.raises(StorageError):
        restart(tmp_path)
    assert path.read_text() == data


@pytest.mark.parametrize("history_size", [0, 1000])
def test_history_is_append_only_and_narration_does_not_rewrite_snapshot(tmp_path, history_size):
    s = service(tmp_path)
    for i in range(history_size):
        s.game.ledger.add("engine", "reveal", f"已知事實 {i}", fact=str(i))
    s._save()
    with s.repository.connect() as conn:
        old = conn.execute("SELECT event_id,payload FROM events ORDER BY event_id").fetchall()
    calls = []
    commit = s.repository.commit

    def counted(channel, revision, snapshot, events, *args, **kwargs):
        calls.append((snapshot is None, len(events)))
        return commit(channel, revision, snapshot, events, *args, **kwargs)

    s.repository.commit = counted
    run(s.handle("依思 go 小巷", user_id="u1", structured=True))
    assert calls == [(False, 2), (True, 1)]
    with s.repository.connect() as conn:
        assert conn.execute("SELECT event_id,payload FROM events WHERE event_id<=? ORDER BY event_id",
                            (history_size,)).fetchall() == old
    assert len(restart(tmp_path).game.ledger.entries) == history_size + 3


def test_memory_is_bounded_relevant_and_never_promotes_prose(tmp_path):
    s = service(tmp_path)
    g = s.game
    g.world.here.hidden_items = [("秘密鑰匙", 1)]
    g.world.here.npcs[0].update(knows=["秘密路徑", "公開傳聞"], disclosed=["公開傳聞"])
    for kind in ("narration", "table", "deny", "materialize", "talk"):
        g.ledger.add("someone", kind, "虛構王冠", item="虛構王冠")
    for i in range(100):
        g.ledger.add("engine", "item", "舊事件" + str(i) + "字" * 150, scene="tavern")
    g.ledger.add("engine", "reveal", "碼頭的事實", scene="dock", npc="老船長")
    context = build_memory(g, "老船長", npc_name="老船長")
    assert len(context.facts) <= MAX_FACTS and len("\n".join(context.facts)) <= MAX_CHARS
    assert "碼頭的事實" in context.facts[0]
    assert "秘密" not in context.render() and "虛構王冠" not in context.render()
    assert "公開傳聞" in context.render()
    assert "秘密路徑" in build_memory(g, audience="director").render()
    assert context == build_memory(g, "老船長", npc_name="老船長")


def test_narrator_sees_only_disclosed_npc_knowledge_and_visible_items(tmp_path):
    s = service(tmp_path)
    s.game.world.here.npcs[0].update(knows=["可以透露", "仍然隱藏"], disclosed=[])
    s.game.world.here.hidden_items = [("秘密寶物", 1)]
    s._save()
    run(s.handle("依思 talk 老船長 你好", user_id="u1", structured=True))
    args, kwargs = s.narrator.calls[-1]
    context = json.dumps([args, kwargs], ensure_ascii=False)
    assert "可以透露" in context
    assert "仍然隱藏" not in context and "秘密寶物" not in context


@pytest.mark.parametrize("response", [
    '{"relevant":"true","consistent":true,"reason":"ok"}',
    '{"relevant":true,"reason":"ok"}', '[]', 'not json',
    '{"relevant":false,"consistent":true,"reason":"wrong"}',
])
def test_model_evaluation_fails_closed(response):
    model = DirectorLLM("http://unused")
    async def call(*args, **kwargs):
        return response
    model._call = call
    assert run(model.evaluate({}, "", ""))[0] is False


def test_model_generation_does_not_coerce_types():
    model = DirectorLLM("http://unused")
    async def call(*args, **kwargs):
        return '{"name":123,"description":"test"}'
    model._call = call
    assert run(model.gen_scene("", "", "", "")) is None


def test_new_commands_use_transactions(tmp_path):
    s = service(tmp_path)
    run(s.create_character("旅人", "Fighter", "u3", "new player"))
    run(s.admin_give("旅人", "木雕", 2))
    run(s.transfer_item("u3", "木雕", "依思"))
    run(s.transfer_item("u3", "木雕"))
    run(s.admin_inspire("旅人"))
    run(s.admin_xp(100, "旅人"))
    g = restart(tmp_path).game
    assert g.party["旅人"]["level"] == 2
    assert g.has_cond("旅人", "inspired")
    assert ["木雕", 1] in g.inventory["依思"]
    assert ("木雕", 1) in g.world.here.ground_items
    run(s.remove_character("旅人", "u2", "remove confirm"))
    assert "旅人" in s.game.party
    run(s.remove_character("旅人", "u3", "remove confirm"))
    assert "旅人" not in restart(tmp_path).game.party


def test_stale_writer_cannot_overwrite_new_state(tmp_path):
    first = service(tmp_path)
    stale = restart(tmp_path)
    run(first.admin_give("依思", "火把"))
    result = run(stale.admin_give("依思", "木雕"))
    assert "未能儲存" in result
    inventory = restart(tmp_path).game.inventory["依思"]
    assert ["火把", 1] in inventory and ["木雕", 1] not in inventory
