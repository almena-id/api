"""Entry point: ``registry-api`` or ``python -m registry_api``.

With no command it runs the API; ``registry-api init-root --admin <email>``
creates the root tenant (Almena) once, at install.
"""

import argparse
import asyncio
import sys

import uvicorn

from registry_api.config import get_settings
from registry_api.db import get_engine, get_sessionmaker
from registry_api.mediators import MediatorError
from registry_api.root import ROOT_MEDIATOR_URL, RootExists, create_root


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


async def init_root(name: str, admin: str, mediator_url: str) -> int:
    try:
        async with get_sessionmaker()() as db:
            tenant, mediator = await create_root(db, name, admin, mediator_url)
    except RootExists:
        print("init-root: there is a root tenant already", file=sys.stderr)
        return 1
    except MediatorError as error:
        print(f"init-root: --mediator-url: {error.code}", file=sys.stderr)
        return 2
    finally:
        await get_engine().dispose()
    print(f"Root tenant {tenant.id} ({name}) and its mediator {mediator.name}")
    print(f"at {mediator.url}: their DIDs are pending signature.")
    print(f"{admin} runs it once they sign in to the portal with that address; an admin")
    print("with a linked Almena wallet signs the identities, and then they resolve.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="registry-api")
    commands = parser.add_subparsers(dest="command")
    root = commands.add_parser("init-root", help="create the root tenant (Almena), once")
    root.add_argument("--admin", required=True, help="email of its first admin")
    root.add_argument("--name", default="Almena", help="its name (default: Almena)")
    root.add_argument(
        "--mediator-url",
        default=ROOT_MEDIATOR_URL,
        help=f"where its mediator listens (default: {ROOT_MEDIATOR_URL})",
    )
    args = parser.parse_args()
    if args.command == "init-root":
        sys.exit(asyncio.run(init_root(args.name, args.admin, args.mediator_url)))
    serve()


if __name__ == "__main__":
    main()
