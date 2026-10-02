import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient

from registry_api.config import get_settings


@pytest.fixture
def well_known(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(get_settings(), "well_known_dir", str(tmp_path))
    return tmp_path


async def test_serves_the_did_configuration(client: AsyncClient, well_known: Path) -> None:
    configuration = {
        "@context": "https://identity.foundation/.well-known/did-configuration/v1",
        "linked_dids": ["eyJ..."],
    }
    (well_known / "did-configuration.json").write_text(json.dumps(configuration))
    response = await client.get("/.well-known/did-configuration.json")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == configuration


async def test_a_missing_file_is_not_found(client: AsyncClient, well_known: Path) -> None:
    assert (await client.get("/.well-known/did-configuration.json")).status_code == 404


async def test_nothing_is_served_without_a_directory(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "well_known_dir", "")
    assert (await client.get("/.well-known/did-configuration.json")).status_code == 404


async def test_serves_security_txt(client: AsyncClient) -> None:
    response = await client.get("/.well-known/security.txt")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    fields = dict(line.split(": ", 1) for line in response.text.splitlines())
    assert fields["Contact"] == "https://github.com/almena-network/api/security/advisories/new"
    expires = datetime.fromisoformat(fields["Expires"])
    assert timedelta(0) < expires - datetime.now(UTC) < timedelta(days=365)
