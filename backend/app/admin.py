"""Local operator commands; passwords are read from the terminal, never argv."""

import argparse
import asyncio
import getpass
import os
import sys

from fastapi import HTTPException

from .backup import backup, restore
from .config import settings
from .security import SecurityStore, maintenance_lock
from .services import KnowledgeBase


def password():
    first = getpass.getpass("Nouveau mot de passe (12 caractères minimum) : ")
    if first != getpass.getpass("Confirmer le mot de passe : "):
        raise ValueError("Les mots de passe ne correspondent pas")
    return first


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Administration locale Instruct")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("create-admin", "recover-admin"):
        sub.add_parser(name).add_argument("username")
    sub.add_parser("backup").add_argument("archive")
    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("archive")
    restore_parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    kb = None
    try:
        if args.command in {"create-admin", "recover-admin"}:
            secret = password()
            security = SecurityStore(settings)
            with maintenance_lock(settings, exclusive=True):
                if args.command == "create-admin":
                    security.create_user(args.username, secret, "admin", bootstrap=True)
                else:
                    with security.connect() as db:
                        row = db.execute(
                            "SELECT id FROM users WHERE username=?",
                            (args.username.strip().lower(),),
                        ).fetchone()
                    if not row:
                        raise ValueError("Compte introuvable")
                    security.reset_password(row[0], secret, None, recover=True)
        else:
            kb = KnowledgeBase(config=settings)
            if args.command == "backup":
                backup(settings, kb, args.archive)
            else:
                restore(settings, kb, args.archive, overwrite=args.overwrite)
        print("Opération terminée.")
    except (ValueError, FileExistsError, HTTPException) as exc:
        print(
            exc.detail if isinstance(exc, HTTPException) else str(exc), file=sys.stderr
        )
        raise SystemExit(1) from exc
    finally:
        if kb:
            asyncio.run(kb.close())


if __name__ == "__main__":
    main()
