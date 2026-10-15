"""v5 offline test suite — zero LLM, zero Discord, fast regressions.

    python -m pytest tests/ -q
"""

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cli import build_demo_game, handle                    # noqa: E402
from core.intent import Intent, parse_command                   # noqa: E402
from core.plot import load_plot, new_state, tick as plot_tick    # noqa: E402
from core.rules_core import (_one_death_save as _death_save,    # noqa: E402
                           resolve,                           # noqa: E402
                           resolve_pending_check as rpc)
from core.service import V4Service, load_gamerules, world_block  # noqa: E402
from core.world import Enemy, Scene                             # noqa: E402


def pin(g):
    for n, e in g.party.items():
        if int(e.get("hp_now", 1)) > 0:
            e["hp_now"] = e["hp_max"]


def end_combat(g):
    g.combat.order = []
    g.combat.idx = 0
    g.encounters[g.world.current] = []


# ---------- director (0a) ----------

def test_materialize_take():
    g = build_demo_game(seed=1)
    L = handle(g, "依思 執 木雕")
    assert any("拾起 木雕" in x and "一直都在那裡" in x for x in L)
    assert any(n == "木雕" for n, _ in g.inventory["依思"])
    assert any(e.kind == "materialize" for e in g.ledger.entries)


def test_blocked_take_stays_snark():
    g = build_demo_game(seed=2)
    L = handle(g, "依思 撿 機關槍")
    assert any("🚫" in x for x in L) and any("🎭" in x for x in L)


# ---------- quiet engine (0b) ----------

def test_passive_auto_success():
    g = build_demo_game(seed=3)
    handle(g, "大力蕉 go 破舊酒館")
    end_combat(g)
    L = handle(g, "大力蕉 搜索")
    assert any("無需擲骰" in x for x in L)
    assert getattr(g, "_pending_check", None) is None


def test_uncertain_dc_still_cards():
    g = build_demo_game(seed=4)
    handle(g, "依思 go 破舊酒館")
    end_combat(g)
    g.world.here.search_dc = 16
    handle(g, "依思 搜索")
    assert g._pending_check is not None


# ---------- rest (v4 regression) ----------

def test_pact_slots_short_rest():
    g = build_demo_game(seed=5)
    g.party["依思"]["slots"] = {"1": 0}
    handle(g, "依思 休息")
    assert g.party["依思"]["slots"]["1"] == 2  # Warlock pact magic


def test_full_caster_needs_long_rest():
    g = build_demo_game(seed=6)
    g.party["大力蕉"]["slots"] = {"1": 0}
    it = Intent(action="rest", actor="大力蕉", raw="休息一下")
    resolve(g, it)  # short
    assert g.party["大力蕉"]["slots"]["1"] == 0
    it2 = Intent(action="rest", actor="大力蕉", raw="我哋長休")
    resolve(g, it2)  # sniffed long
    assert g.party["大力蕉"]["slots"]["1"] == 3


# ---------- survival (seed 3) ----------

def test_death_save_nat20_revives():
    g = build_demo_game(seed=7)
    g.party["依思"]["hp_now"] = 0
    g.d20 = lambda: 20
    lines = _death_save(g, "依思", g.party["依思"])
    assert any("撐了起來" in x for x in lines)
    assert g.party["依思"]["hp_now"] == 1


def test_three_fails_permadead():
    g = build_demo_game(seed=8)
    g.party["依思"]["hp_now"] = 0
    g.d20 = lambda: 5
    for _ in range(3):
        _death_save(g, "依思", g.party["依思"])
    assert g.party["依思"]["death"]["dead"]
    assert not g.alive("依思")


def test_inspiration_consumed_with_advantage():
    g = build_demo_game(seed=9)
    handle(g, "依思 go 破舊酒館")
    end_combat(g)
    g.add_cond("依思", "inspired", None)
    calls = []
    g.d20 = lambda: (calls.append(1), 20 if len(calls) == 1 else 2)[1]
    r = resolve(g, Intent(action="attack", actor="依思", target="哥布林①"))
    assert any("靈感優勢" in x for x in r.lines)
    assert len(calls) == 2 and not g.has_cond("依思", "inspired")


# ---------- checks (seed 4) ----------

def test_help_pending_takes_high():
    g = build_demo_game(seed=10)
    g._help_check = {"依思": True}
    handle(g, "依思 檢定 STR") if False else resolve(
        g, Intent(action="check", actor="依思", ability="STR",
                  raw="檢定"))
    assert g._pending_check.get("helped") is True
    g.d20 = lambda: 20  # the engine's aid die
    ok, line = rpc(g, 3)  # player rolled 3 -> max(3,20)
    assert "援助優勢" in line and "d20(20)" in line


def test_group_stealth_majority():
    g = build_demo_game(seed=11)
    g.d20 = lambda: 20
    r = resolve(g, Intent(action="skill", actor="依思", skill="潛行",
                          raw="我哋匿埋", utterance="我哋匿埋"))
    assert any("團體潛行" in x for x in r.lines)
    assert all(g._stealth.get(n) for n in g.party)


# ---------- affordances (A1) ----------

def test_throw_into_sea_gone():
    g = build_demo_game(seed=12)
    g.give_item("依思", "木雕", 1)
    r = resolve(g, Intent(action="creative", actor="依思", item="木雕",
                          utterance="我掟個木雕落海"))
    assert any("永遠消失" in x for x in r.lines)
    assert not any(n == "木雕" for n, _ in g.inventory["依思"])


def test_ignite_sets_scene_fire_persisted(tmp_path):
    g = build_demo_game(seed=13)
    g.give_item("依思", "火把", 1)
    r = resolve(g, Intent(action="creative", actor="依思", item="火把",
                          utterance="用火把點燃呢度"))
    assert any("陷入火海" in x for x in r.lines) and g.world.here.fire
    svc = _svc(g, tmp_path)
    restored = V4Service(str(tmp_path), "http://unused", channel_id="t")
    assert restored.game.world.scenes["dock"].fire is True


# ---------- plot spine (Pillar B) ----------

def test_plot_clock_and_npc_move():
    g = build_demo_game(seed=14)
    g.world.scenes["altar"] = Scene("altar", "海底祭壇", "")
    g.world.scenes["dock"].npcs.append(
        {"name": "巴鐸", "desc": "", "disposition": "wary"})
    g.enemies["巴鐸的護衛"] = Enemy.make("巴鐸的護衛", 9, 14)
    plot = load_plot()
    st = new_state(plot)
    st["clocks"]["queen_patience"] = 0  # silence that clock
    g.enemies["巴鐸的護衛"].dead = True
    g.ledger.next_turn()
    lines, _ = plot_tick(g, plot, st)
    assert any("巴鐸" in x for x in lines)
    assert any(n["name"] == "巴鐸" for n in g.world.scenes["altar"].npcs)


def test_plot_ending_freezes():
    g = build_demo_game(seed=15)
    plot = load_plot()
    st = new_state(plot)
    st["clocks"]["queen_patience"] = 0
    g.give_item("依思", "海妖女王的項鍊", 1)
    g.ledger.next_turn()
    lines, _ = plot_tick(g, plot, st)
    assert any("劇終" in x for x in lines)
    assert st["ended"]
    again, _ = plot_tick(g, plot, st)
    assert again == []


def test_plot_enemy_dead_ledger_only():
    """Production shape (live crash 'Entry has no attribute target'):
    combat ended long ago — g.enemies empty, the death exists only as a
    ledger entry. The condition must still match via data, not attrs."""
    g = build_demo_game(seed=17)
    plot = load_plot()
    st = new_state(plot)
    st["clocks"]["queen_patience"] = 0  # silence the other beat
    g.ledger.add("大力蕉", "death", "巴鐸的護衛 倒下",
                 target="巴鐸的護衛")
    assert not g.enemies  # the fallback path is the ONLY path
    lines, _ = plot_tick(g, plot, st)
    assert any("巴鐸" in x for x in lines)


# ---------- service-level (seeds 4/5) ----------

class _FakeNarr:
    def __init__(self):
        self.captured = {}

    async def narrate(self, *a, **k):
        self.captured = k
        return "測試敘事。"


def _svc(g, tmp_path):
    s = V4Service(str(tmp_path), "http://unused", channel_id="t")
    s.game = g
    s.digestor = None
    s.narrator = _FakeNarr()
    s.rules = {}
    s.plot = {}
    s.plot_state = {"clocks": {}, "fired": [], "last_turn": 0, "ended": None}
    s._save()
    return s


def test_narration_persisted_to_ledger(tmp_path):
    g = build_demo_game(seed=16)
    svc = _svc(g, tmp_path)
    lines, narr = asyncio.run(svc.handle(
        "依思 go 破舊酒館", "tester", structured=True))
    assert narr == "測試敘事。"
    assert any(e.kind == "narration" and e.actor == "narrator"
               for e in svc.game.ledger.entries)


def test_per_channel_gamerules_override(tmp_path):
    ch = "999"
    over = tmp_path / f"gamerules_{ch}.json"
    over.write_text(json.dumps({"tone": "頻道專屬基調", "world": {
        "canon": ["頻道事實"]}}, ensure_ascii=False), encoding="utf-8")
    rules = load_gamerules(ch, str(tmp_path))
    assert rules["tone"] == "頻道專屬基調"
    assert "頻道事實" in world_block(rules)


def test_parse_help_action():
    it = parse_command("大力蕉 幫助 依思", party_names=["依思", "大力蕉"])
    assert it.action == "help" and it.target == "依思"
