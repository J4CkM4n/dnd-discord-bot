"""Campaign lifecycle, seed migration, and Discord permission boundaries."""

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict

import pytest

from adapters.discord_bot import DiscordBot, HELP_TEXT
from core.cli import build_demo_game
from core.intent import Intent
from core.memory import build_memory
from core.operators import progress_input
from core.persistence import CampaignRepository
from core.plot import new_state
from core.service import V4Service
from core.state import encode_game
from test_campaign import run, service, restart
from test_campaign_integration import interaction


def archives(s):
    with s.repository.connect() as conn:
        return [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM campaign_archives WHERE channel_id=? ORDER BY id", (s.channel_id,))]


def test_fresh_campaign_has_no_demo_party_and_can_create_player(tmp_path):
    s = V4Service(str(tmp_path), channel_id="fresh")
    assert s.game.party == s.game.inventory == {}
    assert s.game.world.current == "dock"
    assert s.game.world.here.npcs[0]["knows"]
    # Game setup and scene inspection work without a model service.
    assert "新遊戲已開始" in run(s.new_game("admin", admin=True))
    assert "BANANA🍌" in run(s.create_character("BANANA🍌", "Fighter", "u1", "Banana"))
    restored = V4Service(str(tmp_path), channel_id="fresh")
    assert list(restored.game.party) == ["BANANA🍌"]
    assert restored.game.party["BANANA🍌"]["owner_id"] == "u1"
    assert "依思" not in HELP_TEXT and "大力蕉" not in HELP_TEXT


@pytest.mark.parametrize("legacy_json", [False, True])
def test_unowned_seed_migration_preserves_real_players_world_and_history(tmp_path, legacy_json):
    g = build_demo_game(seed=7)
    g.party["GonJK"] = deepcopy(g.party["依思"])
    g.party["GonJK"]["owner_id"] = "real-user"
    g.inventory["GonJK"] = [["木雕", 2]]
    g.party["EC"] = deepcopy(g.party["大力蕉"])
    g.party["EC"]["owner_id"] = "another-user"
    g.inventory["EC"] = []
    g.world.here.fire = True
    g.ledger.add("依思", "heal", "依思 恢復生命", scene="dock")
    g.ledger.add("大力蕉", "search", "大力蕉 搜索碼頭", scene="dock")
    g.ledger.add("GonJK", "item", "GonJK 拾起木雕", scene="dock")
    snapshot = encode_game(g)
    history = [asdict(e) for e in g.ledger.entries]
    if legacy_json:
        (tmp_path / "v4_state_t.json").write_text(json.dumps(
            {**snapshot, "entries": history}, ensure_ascii=False))
    else:
        repo = CampaignRepository(str(tmp_path / "campaigns.db"))
        repo.commit("t", 0, snapshot, history, None)
    s = restart(tmp_path)
    assert set(s.game.party) == {"GonJK", "EC"}
    assert s.game.inventory == {"GonJK": [["木雕", 2]], "EC": []}
    assert s.game.world.here.fire
    assert [asdict(e) for e in s.game.ledger.entries] == history
    assert set(s.game.retired_demo_names) == {"依思", "大力蕉"}
    data, _ = progress_input(s.game, {}, "")
    assert "依思" not in json.dumps(data, ensure_ascii=False)
    assert "大力蕉" not in json.dumps(data, ensure_ascii=False)
    assert "GonJK" in build_memory(s.game).render()
    again = restart(tmp_path)
    assert again._revision == s._revision  # migration is idempotent
    assert again.game.retired_demo_names == s.game.retired_demo_names


@pytest.mark.parametrize("ownership", [{"owner_id": "u1"}, {"owner": "legacy-player"}])
def test_migration_preserves_owned_characters_even_with_demo_names(tmp_path, ownership):
    g = build_demo_game()
    for entry in g.party.values():
        entry.update(ownership)
    repo = CampaignRepository(str(tmp_path / "campaigns.db"))
    repo.commit("t", 0, encode_game(g), [], None)
    s = restart(tmp_path)
    assert set(s.game.party) == {"依思", "大力蕉"}
    assert s.game.retired_demo_names == []


def test_migration_cleans_pending_checks_and_combat_references(tmp_path):
    g = build_demo_game()
    g._pending_check = {"actor": "依思"}
    g._aid = {"依思": True, "大力蕉": True}
    g.combat.order = [{"name": "依思", "npc": False}, {"name": "大力蕉", "npc": False}]
    g.combat.acted = ["依思"]
    g.combat.idx = 1
    blob = encode_game(g)
    blob.update(pending=asdict(Intent("move", actor="大力蕉", destination="tavern")),
                pending_user_id="old-user", pending_admin=True)
    repo = CampaignRepository(str(tmp_path / "campaigns.db"))
    repo.commit("t", 0, blob, [], {"text": "過時的敘事"})
    s = restart(tmp_path)
    assert not s.game.party and not s.game.inventory and not s.game.combat.active
    assert not s.game.combat.acted and s.game.combat.idx == 0
    assert s.game._pending_check is None and s.game._aid == {}
    assert s.pending is None and s.pending_user_id == "" and s.pending_admin is False
    assert not s.has_unfinished()


def test_scene_is_read_only_and_does_not_disclose_secrets(tmp_path):
    s = service(tmp_path)
    s.game.world.here.hidden_items = [("秘密寶物", 1)]
    s.game.world.here.ground_items = [("火把", 2)]
    s.game.world.here.npcs[0]["knows"] = ["秘密路線"]
    s._save()
    before = s.repository.load("t")
    result = s.scene()
    assert all(word in result for word in ("霧錨鎮碼頭", "老船長", "破舊酒館", "火把 ×2"))
    assert "秘密寶物" not in result and "秘密路線" not in result and "哥布林" not in result
    assert s.repository.load("t") == before


def test_new_game_never_overwrites_an_existing_game(tmp_path):
    s = service(tmp_path)
    before = s.repository.load("t")
    assert "已有遊戲" in run(s.new_game("admin", admin=True))
    assert s.repository.load("t") == before
    fresh = V4Service(str(tmp_path), channel_id="empty")
    assert "新遊戲已開始" in run(fresh.new_game("admin", admin=True))
    before = fresh.repository.load("empty")
    assert "已有遊戲" in run(fresh.new_game("admin", admin=True))
    assert fresh.repository.load("empty") == before


def test_lifecycle_requires_admin_and_reset_requires_exact_confirmation(tmp_path):
    s = service(tmp_path)
    before = s.repository.load("t")
    assert "🚫" in run(s.new_game("player"))
    assert "🚫" in run(s.reset_game("player", "reset confirm"))
    for value in ("", "confirm", "reset", "yes"):
        assert "確認" in run(s.reset_game("admin", value, admin=True))
    assert s.repository.load("t") == before and archives(s) == []


def test_reset_archives_all_state_and_isolates_other_channels(tmp_path):
    s = service(tmp_path)
    other = service(tmp_path, "other")
    other_before = other.repository.load("other")
    s.game.world.current = "tavern"
    s.game.start_encounter(seed=7)
    s.game.director_scene_count = 9
    s.game.world.here.fire = True
    s.game._pending_check = {"actor": "依思", "event_id": 1}
    s.pending = Intent("move", actor="依思", destination="dock")
    s.pending_user_id, s.pending_admin = "u1", True
    s.plot_state["fired"] = ["old_beat"]
    s._operator_state["accepted_turns"] = 99
    s._operator_state["pending_triggers"] = ["stalled"]
    s._unfinished = {"text": "舊敘事"}
    s._recent_narrations = ["舊敘事"]
    s.game.ledger.add("依思", "item", "依思 拾起舊寶物")
    s._decisions = [{"accepted": True}]
    s._save()
    with s.repository.connect() as conn:
        conn.execute("INSERT INTO operator_reports"
            "(channel_id,revision,source_revision,role,status,payload) VALUES (?,?,?,?,?,?)",
            ("t", s._revision, s._revision, "main", "ok", '{"old":"report"}'))
    before = s.repository.load("t")
    assert "已封存" in run(s.reset_game("admin", "reset confirm", admin=True))
    assert not s.game.party and not s.game.inventory and not s.game.enemies
    assert not s.game.combat.active and not s.game.combat.acted
    assert s.game.world.current == "dock" and not s.game.world.here.fire
    assert s.game.director_scene_count == 0 and s.game.ledger.turn == 0
    assert s.pending is None and s.pending_user_id == "" and s.pending_admin is False
    assert not s.has_unfinished() and s._recent_narrations == []
    assert s.plot_state == new_state(s.plot)
    assert s._operator_state == {"accepted_turns": 0, "pending_triggers": [], "last_reviewed_event": 0}
    restored = restart(tmp_path)
    assert restored._revision == before["revision"] + 1
    assert not restored.game.party and not restored.has_unfinished()
    assert restored.game.ledger.entries[0].event_id == 1
    assert "舊寶物" not in build_memory(restored.game).render()
    archive = archives(s)[0]
    assert json.loads(archive["channels"][0]["snapshot"]) == before["snapshot"]
    assert [json.loads(e["payload"]) for e in archive["events"]] == before["entries"]
    assert json.loads(archive["narration"][0]["payload"]) == before["unfinished"]
    assert archive["director_decisions"] and archive["operator_reports"] and archive["operator_state"]
    assert other.repository.load("other") == other_before
    run(restored.create_character("New Hero", "Rogue", "new-user", "Player"))
    assert list(restart(tmp_path).game.party) == ["New Hero"]
    with s.repository.connect() as conn:
        assert conn.execute("SELECT count(*) FROM operator_reports WHERE channel_id='t'").fetchone()[0] == 0


def test_reset_database_failure_rolls_back_archive_and_live_state(tmp_path):
    s = service(tmp_path)
    before = s.repository.load("t")
    game_before = encode_game(s.game)
    # Fail AFTER archive creation and deletion, when the replacement is written.
    with s.repository.connect() as conn:
        conn.execute("CREATE TRIGGER reject_reset BEFORE UPDATE OF snapshot ON channels "
                     "BEGIN SELECT RAISE(ABORT, 'disk failure'); END")
    assert "未能儲存" in run(s.reset_game("admin", "reset confirm", admin=True))
    assert s.repository.load("t") == before
    assert encode_game(s.game) == game_before and archives(s) == []


def test_stale_service_cannot_reset_or_write_over_restarted_game(tmp_path):
    s = service(tmp_path)
    stale = restart(tmp_path)
    run(s.reset_game("admin", "reset confirm", admin=True))
    before = s.repository.load("t")
    assert "未能儲存" in run(stale.reset_game("admin", "reset confirm", admin=True))
    assert "未能儲存" in run(stale.admin_give("依思", "火把"))
    assert s.repository.load("t") == before and len(archives(s)) == 1


def test_same_channel_game_start_is_serialized(tmp_path):
    s = V4Service(str(tmp_path), channel_id="t")

    async def scenario():
        return await asyncio.gather(s.new_game("a", admin=True), s.new_game("b", admin=True))

    results = run(scenario())
    assert sum("已有遊戲" in text for text in results) == 1
    assert len(s.game.ledger.entries) == 1


def test_discord_lifecycle_checks_permissions_before_loading_campaign(tmp_path):
    bot = DiscordBot()
    it = interaction()
    for name in ("new_game", "reset_game"):
        command = bot.tree.get_command(name)
        assert command.default_permissions is None and command.guild_only
        run(command.callback(it))
    assert not bot._v4_games
    assert it.response.send_message.await_count == 2
    s = V4Service(str(tmp_path), channel_id="123")
    bot._v4_games["123"] = s
    it.user.guild_permissions.manage_guild = True
    run(bot.tree.get_command("new_game").callback(it))
    assert s.game.ledger.entries[0].data["user_id"] == "1"
    before = s.repository.load("123")
    run(bot.tree.get_command("scene").callback(it))
    assert s.repository.load("123") == before
    assert "霧錨鎮碼頭" in it.response.send_message.await_args.args[0]
    run(bot.tree.get_command("reset_game").callback(it, "reset confirm"))
    assert len(archives(s)) == 1


def test_reset_does_not_reimport_preserved_legacy_save(tmp_path):
    s = service(tmp_path)
    path = tmp_path / "v4_state_t.json"
    path.write_text(json.dumps({**encode_game(s.game), "entries": []}, ensure_ascii=False))
    original = path.read_bytes()
    run(s.reset_game("admin", "reset confirm", admin=True))
    assert not restart(tmp_path).game.party
    assert path.read_bytes() == original
