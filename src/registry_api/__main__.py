"""Entry point: ``registry-api`` or ``python -m registry_api``.

With no command it runs the API; ``registry-api init-root --admin <email>``
creates the root tenant (Almena Trust Anchor, with Almena's catalogue) once,
at install.
"""

import argparse
import asyncio
import sys

import uvicorn

from registry_api.config import get_settings
from registry_api.db import get_engine, get_sessionmaker
from registry_api.root import RootExists, create_root
from registry_api.trust_anchor import ANCHOR_NAME


def serve() -> None:
    settings = get_settings()
    uvicorn.run(
        "registry_api.main:app",
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_level=settings.log_level.lower(),
        # Replaced by registry_api.logs' own, which has the request's context.
        access_log=False,
    )


async def init_root(name: str, admin: str) -> int:
    try:
        async with get_sessionmaker()() as db:
            tenant = await create_root(db, name, admin)
    except RootExists:
        print("init-root: there is a root tenant already", file=sys.stderr)
        return 1
    finally:
        await get_engine().dispose()
    print(f"Root tenant {tenant.id} ({name}): its DID is pending signature.")
    print(f"{admin} runs it once they sign in to the portal with that address; an admin")
    print("with a linked Almena wallet signs its identity, and then it resolves.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="registry-api")
    commands = parser.add_subparsers(dest="command")
    root = commands.add_parser("init-root", help="create the root tenant (the trust anchor), once")
    root.add_argument("--admin", required=True, help="email of its first admin")
    root.add_argument("--name", default=ANCHOR_NAME, help=f"its name (default: {ANCHOR_NAME})")
    args = parser.parse_args()
    if args.command == "init-root":
        sys.exit(asyncio.run(init_root(args.name, args.admin)))
    serve()


if __name__ == "__main__":
    main()
