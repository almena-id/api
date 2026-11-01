import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from registry_api import wallet
from registry_api.config import Settings, get_settings

FINGERPRINT = ":".join(["AB"] * 32)


async def test_apple_app_site_association_names_the_wallet(client: AsyncClient) -> None:
    response = await client.get("/.well-known/apple-app-site-association")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "applinks": {
            "details": [
                {
                    "appIDs": ["XDUFSW425R.id.almena.wallet"],
                    "components": [{"/": "/w/*"}],
                }
            ]
        }
    }


async def test_assetlinks_is_empty_without_a_fingerprint(client: AsyncClient) -> None:
    response = await client.get("/.well-known/assetlinks.json")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == []


async def test_assetlinks_names_the_package_and_its_fingerprints(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = ":".join(["01"] * 32)
    monkeypatch.setattr(get_settings(), "android_app_fingerprints", [FINGERPRINT, other])
    response = await client.get("/.well-known/assetlinks.json")
    assert response.json() == [
        {
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {
                "namespace": "android_app",
                "package_name": "id.almena.wallet",
                "sha256_cert_fingerprints": [FINGERPRINT, other],
            },
        }
    ]


def test_settings_read_comma_separated_apps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGISTRY_ANDROID_APP_FINGERPRINTS", f"{FINGERPRINT}, {FINGERPRINT}")
    monkeypatch.setenv("REGISTRY_APPLE_APP_IDS", "XDUFSW425R.id.almena.wallet,ABCDE12345.a.b")
    settings = Settings()
    assert settings.android_app_fingerprints == [FINGERPRINT, FINGERPRINT]
    assert settings.apple_app_ids == ["XDUFSW425R.id.almena.wallet", "ABCDE12345.a.b"]


@pytest.mark.parametrize(
    "fingerprint",
    [
        FINGERPRINT.lower(),
        FINGERPRINT.replace(":", ""),
        ":".join(["AB"] * 31),
        ":".join(["AB"] * 33),
        FINGERPRINT[:-2] + "ZZ",
    ],
)
def test_settings_refuse_a_malformed_fingerprint(fingerprint: str) -> None:
    with pytest.raises(ValidationError):
        Settings(android_app_fingerprints=[fingerprint])


@pytest.mark.parametrize("app_id", ["id.almena.wallet", "xdufsw425r.id.almena.wallet", "X.y"])
def test_settings_refuse_a_malformed_apple_app_id(app_id: str) -> None:
    with pytest.raises(ValidationError):
        Settings(apple_app_ids=[app_id])


@pytest.mark.parametrize("package", ["wallet", "id..wallet", "1d.almena", "id.almena-wallet"])
def test_settings_refuse_a_malformed_package(package: str) -> None:
    with pytest.raises(ValidationError):
        Settings(android_package=package)


def test_links_are_https_on_the_identity_domain() -> None:
    uri = "https://almena.id/api/v1/auth/wallet/requests/abc"
    links = wallet.links(uri)
    assert links["scheme_link"] == (
        "almena://auth?request_uri=https%3A%2F%2Falmena.id%2Fapi%2Fv1%2Fauth%2Fwallet%2Frequests%2Fabc"
    )
    assert links["deep_link"] == (
        "https://almena.id/w/auth?request_uri=https%3A%2F%2Falmena.id%2Fapi%2Fv1%2Fauth%2Fwallet%2Frequests%2Fabc"
    )


async def test_the_page_offers_the_same_link_in_the_wallet(client: AsyncClient) -> None:
    query = "request_uri=https%3A%2F%2Falmena.id%2Fapi%2Fv1%2Fverifications%2Fx%2Frequest"
    response = await client.get(f"/w/auth?{query}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    assert "Open this in the <span>Almena wallet</span>" in html
    assert f'href="almena://auth?{query}"' in html
    assert 'href="https://docs.almena.id/use/wallet/"' in html
    assert "--brand:#e0413a" in html
    assert "<script" not in html


async def test_the_page_keeps_the_path(client: AsyncClient) -> None:
    response = await client.get("/w/invite?_oob=eyJhIjoiYiJ9")
    assert 'href="almena://invite?_oob=eyJhIjoiYiJ9"' in response.text


async def test_the_page_is_locked_down(client: AsyncClient) -> None:
    response = await client.get("/w/auth?request_uri=x")
    policy = response.headers["content-security-policy"]
    assert policy.startswith("default-src 'none'; style-src 'sha256-")
    assert "script-src" not in policy and "unsafe-inline" not in policy
    assert "frame-ancestors 'none'" in policy and "form-action 'none'" in policy
    assert f"font-src {get_settings().public_url}" in policy
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


async def test_the_page_never_reflects_markup(client: AsyncClient) -> None:
    response = await client.get(
        '/w/"><script>alert(1)</script>?a="><img src=x onerror=alert(1)>&b=<i>'
    )
    assert response.status_code == 200
    html = response.text
    assert "<script>" not in html and "<img" not in html and "<i>" not in html
    assert '"><' not in html.split('class="button" href="', 1)[1].split('">', 1)[0]


async def test_the_page_refuses_an_endless_link(client: AsyncClient) -> None:
    response = await client.get("/w/invite?_oob=" + "x" * (17 * 1024))
    assert response.status_code == 414


async def test_fonts_load_from_the_identity_domain(client: AsyncClient) -> None:
    response = await client.get("/fonts/inter.woff2", headers={"Origin": "https://almena.id"})
    assert response.headers["access-control-allow-origin"] == "*"
