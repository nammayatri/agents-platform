"""CLI utilities for the agent orchestration platform."""

import argparse
import asyncio
import sys

import asyncpg

from agents.config.settings import settings


async def promote_user(email: str) -> None:
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        row = await pool.fetchrow("SELECT id, role FROM users WHERE email = $1", email)
        if not row:
            print(f"Error: no user found with email '{email}'")
            sys.exit(1)
        if row["role"] == "admin":
            print(f"User '{email}' is already an admin")
            return
        await pool.execute("UPDATE users SET role = 'admin' WHERE id = $1", row["id"])
        print(f"Promoted '{email}' to admin")
    finally:
        await pool.close()


async def demote_user(email: str) -> None:
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        row = await pool.fetchrow("SELECT id, role FROM users WHERE email = $1", email)
        if not row:
            print(f"Error: no user found with email '{email}'")
            sys.exit(1)
        if row["role"] == "user":
            print(f"User '{email}' is already a regular user")
            return
        await pool.execute("UPDATE users SET role = 'user' WHERE id = $1", row["id"])
        print(f"Demoted '{email}' to regular user")
    finally:
        await pool.close()


async def list_admins() -> None:
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        rows = await pool.fetch(
            "SELECT email, display_name, created_at FROM users "
            "WHERE role = 'admin' ORDER BY created_at"
        )
        if not rows:
            print("No admin users found")
            return
        print(f"{'Email':<30} {'Name':<20} {'Created'}")
        print("-" * 70)
        for r in rows:
            print(f"{r['email']:<30} {r['display_name']:<20} {r['created_at']}")
    finally:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description="Agent platform CLI")
    sub = parser.add_subparsers(dest="command")

    promote = sub.add_parser("promote", help="Promote a user to admin")
    promote.add_argument("email", help="Email of the user to promote")

    demote = sub.add_parser("demote", help="Demote an admin to regular user")
    demote.add_argument("email", help="Email of the user to demote")

    sub.add_parser("list-admins", help="List all admin users")

    args = parser.parse_args()

    if args.command == "promote":
        asyncio.run(promote_user(args.email))
    elif args.command == "demote":
        asyncio.run(demote_user(args.email))
    elif args.command == "list-admins":
        asyncio.run(list_admins())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
