from httpx import AsyncClient
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from registry_api import memo
from registry_api.models import Identity, Tenant
from tests.conftest import Outbox
from tests.fake_wallet import FakeWallet
from tests.signing import link_wallet, sign
from tests.test_directory import _sign_in


async def test_an_answer_is_kept_until_something_is_written(
    db: AsyncSession, anchor: Tenant
) -> None:
    calls = 0

    async def compute() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await memo.remembered(db, "x", compute) == 1
    assert await memo.remembered(db, "x", compute) == 1
    db.add(Identity(tenant_id=anchor.id, name="Spare"))
    await db.flush()
    assert await memo.remembered(db, "x", compute) == 2
    await db.execute(update(Identity).values(name="Renamed"))
    assert await memo.remembered(db, "x", compute) == 3


async def test_a_page_of_identities_asks_little(
    client: AsyncClient, outbox: Outbox, database: async_sessionmaker[AsyncSession]
) -> None:
    headers, tenant = await _sign_in(client, outbox, "ada@example.org")
    base = f"/api/v1/tenants/{tenant}"
    wallet = FakeWallet()
    await link_wallet(client, headers, wallet)
    for index in range(20):
        made = await client.post(f"{base}/identities", json={"name": f"I{index}"}, headers=headers)
        await sign(client, headers, tenant, made.json()["id"], wallet)

    async with database() as session:
        engine = session.get_bind()
    asked = 0

    def count(*_: object) -> None:
        nonlocal asked
        asked += 1

    event.listen(engine, "before_cursor_execute", count)
    try:
        page = await client.get(f"{base}/identities?limit=20", headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert page.status_code == 200
    assert all(item["signature"] == "signed" for item in page.json()["items"])
    # Twenty rows: what they share is asked once, the rest a few times each.
    assert asked < 120
