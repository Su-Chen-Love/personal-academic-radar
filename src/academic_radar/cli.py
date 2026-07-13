"""Command-line administration for Personal Academic Radar."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from .storage import backup_database, database_status, migrate_state, restore_database, upgrade_database


def _default_backup(db: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return db.expanduser().resolve().with_name(f"{db.stem}.backup-{stamp}{db.suffix}")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="academic-radar")
    groups = root.add_subparsers(dest="group", required=True)

    db = groups.add_parser("db", help="manage the SQLite database")
    db_commands = db.add_subparsers(dest="command", required=True)
    for name in ("status", "upgrade"):
        command = db_commands.add_parser(name)
        command.add_argument("--db", required=True, type=Path)
    backup = db_commands.add_parser("backup")
    backup.add_argument("--db", required=True, type=Path)
    backup.add_argument("--output", type=Path)
    restore = db_commands.add_parser("restore")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--db", required=True, type=Path)
    restore.add_argument("--replace", action="store_true")

    state = groups.add_parser("state", help="manage the external state directory")
    state_commands = state.add_subparsers(dest="command", required=True)
    migrate = state_commands.add_parser("migrate")
    migrate.add_argument("--from", dest="source", required=True, type=Path)
    migrate.add_argument("--to", dest="destination", required=True, type=Path)
    migrate.add_argument("--merge", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.group == "state":
            result = migrate_state(args.source, args.destination, args.merge)
        elif args.command == "status":
            result = database_status(args.db)
        elif args.command == "upgrade":
            result = upgrade_database(args.db)
        elif args.command == "backup":
            result = backup_database(args.db, args.output or _default_backup(args.db))
        else:
            result = restore_database(args.backup, args.db, args.replace)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

