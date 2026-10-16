"""Back up a campaign DB and prepare a read-only inventory repair report.

No inventory is changed. Review ledger evidence before applying corrections.
Run: python -m tools.audit_inventory --database data/campaigns.db --output-dir data/repair
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def candidates(snapshot, events):
    """Only known parser artifacts with matching materialization evidence.

    Admin grants and unrelated large stacks are deliberately excluded. A
    candidate is a review suggestion, not permission to remove a whole stack.
    """
    result = []
    for actor, stacks in snapshot.get("inventory", {}).items():
        for name, qty in stacks:
            if name not in ("起", "出木塞") or qty <= 0:
                continue
            materialized = [e for e in events if e.get("kind") == "materialize" and e.get("data", {}).get("item") == name]
            admin = [e for e in events if e.get("actor") == "admin" and (e.get("data", {}).get("item") == name or name in e.get("text", ""))]
            pickups = [e for e in events if e.get("kind") == "item" and e.get("actor") == actor
                       and e.get("data", {}).get("item") == name and "拾起" in e.get("text", "")]
            if materialized and pickups and not admin:
                result.append({"actor": actor, "item": name, "current_quantity": qty,
                    "suggested_remove": min(qty, sum(e.get("data", {}).get("qty", 1) for e in pickups)),
                    "evidence_event_ids": [e.get("event_id") for e in materialized + pickups],
                    "reason": "Known pickup/take-out parser artifact; verify provenance and quantity before removal."})
    return result


def audit(database, output_dir, channel=None):
    database, output_dir = Path(database).resolve(), Path(output_dir).resolve()
    if not database.is_file():
        raise ValueError(f"Campaign database does not exist: {database}")
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = output_dir / f"campaigns-{stamp}.db"
    report_path = output_dir / f"inventory-review-{stamp}.json"
    # SQLite's backup API includes committed WAL content; copying the main DB
    # file alone would miss recent turns. Audit the resulting consistent copy.
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(backup_path) as backup:
            source.backup(backup)
    channels = []
    with sqlite3.connect(backup_path) as conn:
        rows = conn.execute("SELECT channel_id, revision, snapshot FROM channels" +
                            (" WHERE channel_id=?" if channel is not None else ""),
                            (str(channel),) if channel is not None else ()).fetchall()
        for channel_id, revision, raw in rows:
            events = [json.loads(row[0]) for row in conn.execute(
                "SELECT payload FROM events WHERE channel_id=? ORDER BY event_id", (channel_id,))]
            channels.append({"channel_id": channel_id, "expected_revision": revision,
                             "candidates": candidates(json.loads(raw), events)})
    report = {"backup": str(backup_path), "database": str(database), "read_only": True,
              "instructions": "Review each candidate against the backup and ledger; no changes have been applied.",
              "channels": channels}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--channel")
    args = parser.parse_args()
    print(audit(args.database, args.output_dir, args.channel))


if __name__ == "__main__":
    main()
