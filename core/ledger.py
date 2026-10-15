"""v4 ledger: the append-only, deterministic fact log of the game.

Every engine resolution appends facts here; the narrator (P2) renders the
ledger into prose. In degraded mode the ledger lines ARE the narration.
The ledger is an audit trail. Recovery uses a complete state snapshot;
historical entries alone do not encode every state transition.
"""

import json
from dataclasses import dataclass, field, asdict


@dataclass
class Entry:
    turn: int
    actor: str
    kind: str            # attack|damage|heal|item|move|scene|check|rest|
                         # talk|search|combat|death|xp|deny|confirm...
    text: str            # one-line human fact (degraded-mode narration)
    data: dict = field(default_factory=dict)  # machine facts (dice, numbers)
    event_id: int = 0     # stable, monotonically increasing within a channel


class Ledger:
    def __init__(self):
        self.entries: list[Entry] = []
        self.turn = 0

    def next_turn(self) -> int:
        self.turn += 1
        return self.turn

    def add(self, actor: str, kind: str, text: str, **data) -> Entry:
        e = Entry(turn=self.turn, actor=actor, kind=kind, text=text, data=data,
                  event_id=self.entries[-1].event_id + 1 if self.entries else 1)
        self.entries.append(e)
        return e

    def recent(self, n: int = 30) -> list[Entry]:
        return self.entries[-n:]

    def fork(self) -> "Ledger":
        """Stage new events without copying the immutable historical payloads."""
        led = Ledger()
        led.turn = self.turn
        led.entries = self.entries.copy()
        return led

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"turn": self.turn,
                       "entries": [asdict(e) for e in self.entries]},
                      f, ensure_ascii=False, indent=1)

    @classmethod
    def load(cls, path: str) -> "Ledger":
        led = cls()
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
        led.turn = blob["turn"]
        led.entries = [Entry(**dict(e, event_id=e.get("event_id") or i))
                       for i, e in enumerate(blob["entries"], 1)]
        return led
