"""Command-line administration for Personal Academic Radar."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

from .storage import backup_database, database_status, migrate_state, restore_database, upgrade_database
from .engagement import (
    confirm_profile, create_profile_draft, feedback_examples, list_feedback, list_profiles, set_feedback
)
from .operations import verify_installation


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

    feedback = groups.add_parser("feedback", help="record and inspect paper feedback")
    feedback_commands = feedback.add_subparsers(dest="command", required=True)
    feedback_set = feedback_commands.add_parser("set")
    feedback_set.add_argument("--db", required=True, type=Path)
    feedback_set.add_argument("--identity", required=True)
    feedback_set.add_argument("--interest", choices=("interested", "not_interested", "none"), default="none")
    feedback_set.add_argument("--reason", default="")
    feedback_set.add_argument("--favorite", action="store_true")
    feedback_set.add_argument("--reading-status", choices=("unread", "read", "read_later"), default="unread")
    for name in ("list", "examples"):
        command = feedback_commands.add_parser(name)
        command.add_argument("--db", required=True, type=Path)

    profile = groups.add_parser("profile", help="manage confirmed research-profile versions")
    profile_commands = profile.add_subparsers(dest="command", required=True)
    profile_list = profile_commands.add_parser("list")
    profile_list.add_argument("--db", required=True, type=Path)
    draft = profile_commands.add_parser("draft")
    draft.add_argument("--db", required=True, type=Path)
    draft.add_argument("--file", required=True, type=Path)
    draft.add_argument("--summary", required=True)
    confirm = profile_commands.add_parser("confirm")
    confirm.add_argument("--db", required=True, type=Path)
    confirm.add_argument("--id", required=True, type=int)
    confirm.add_argument("--profile-file", required=True, type=Path)

    web = groups.add_parser("web", help="run the local web application")
    web.add_argument("--config", required=True, type=Path)
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", default=8765, type=int)
    web.add_argument("--allow-remote", action="store_true")

    verify = groups.add_parser("verify", help="verify a complete daily-use installation")
    verify.add_argument("--config", required=True, type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.group == "verify":
            result=verify_installation(args.config)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return 0 if result["ok"] else 1
        if args.group == "web":
            if args.host not in ("127.0.0.1", "localhost", "::1") and not args.allow_remote:
                raise ValueError("Remote binding requires --allow-remote and an access-control review")
            import uvicorn
            from .web import create_app
            uvicorn.run(create_app(args.config), host=args.host, port=args.port)
            return 0
        if args.group == "state":
            result = migrate_state(args.source, args.destination, args.merge)
        elif args.group == "feedback":
            if args.command == "set":
                result = set_feedback(args.db,args.identity,None if args.interest=="none" else args.interest,
                                      args.reason,args.favorite,args.reading_status)
            elif args.command == "examples":
                result = feedback_examples(args.db)
            else:
                result = list_feedback(args.db)
        elif args.group == "profile":
            if args.command == "draft":
                result = create_profile_draft(args.db,args.file.read_text(encoding="utf-8"),args.summary)
            elif args.command == "confirm":
                result = confirm_profile(args.db,args.id,args.profile_file)
            else:
                result = list_profiles(args.db)
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
