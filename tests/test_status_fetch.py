"""Fetching another issuer's status list: https to public addresses only, no
redirect elsewhere, a size cap, and inflating no more than asked for."""

import gzip
import zlib
from collections.abc import AsyncIterator

import httpx
import pytest

from registry_api import presentations
from registry_api.presentations import FetchRefused, Problem, fetch_status


def _resolver(addresses: dict[str, list[str]]) -> presentations.Resolve:
    async def resolve(host: str, port: int) -> list[str]:
        if host not in addresses:
            raise OSError("no such host")
        return addresses[host]

    return resolve


PUBLIC = {"lists.example": ["93.184.216.34"], "other.example": ["2606:2800:220:1::1"]}


async def test_a_public_list_is_fetched_from_the_address_resolved() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="token")

    body = await fetch_status(
        "https://lists.example/1",
        resolve=_resolver(PUBLIC),
        transport=httpx.MockTransport(handler),
    )
    assert body == "token"
    # Connected to the address checked, under the name asked for (TLS too).
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "lists.example"
    assert seen[0].extensions["sni_hostname"] == "lists.example"


@pytest.mark.parametrize(
    ("url", "addresses", "code"),
    [
        ("http://lists.example/1", PUBLIC, "not_https"),
        ("https://user:pw@lists.example/1", PUBLIC, "not_https"),
        ("https://unknown.example/1", PUBLIC, "unresolvable"),
        ("https://local.example/1", {"local.example": ["127.0.0.1"]}, "not_public"),
        ("https://lan.example/1", {"lan.example": ["10.0.0.7"]}, "not_public"),
        ("https://meta.example/1", {"meta.example": ["169.254.169.254"]}, "not_public"),
        ("https://v6.example/1", {"v6.example": ["::1"]}, "not_public"),
        ("https://mapped.example/1", {"mapped.example": ["::ffff:192.168.1.1"]}, "not_public"),
        ("https://cgnat.example/1", {"cgnat.example": ["100.64.0.1"]}, "not_public"),
        # One private address among public ones is enough to refuse.
        ("https://mixed.example/1", {"mixed.example": ["93.184.216.34", "10.0.0.1"]}, "not_public"),
    ],
)
async def test_what_is_not_https_to_a_public_address_is_refused(
    url: str, addresses: dict[str, list[str]], code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing is sent")

    with pytest.raises(FetchRefused) as refused:
        await fetch_status(
            url, resolve=_resolver(addresses), transport=httpx.MockTransport(handler)
        )
    assert refused.value.code == code


async def test_redirects_are_checked_like_the_first_address() -> None:
    addresses = {**PUBLIC, "inside.example": ["192.168.0.10"]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "lists.example":
            return httpx.Response(302, headers={"location": "https://other.example/2"})
        if request.url.path == "/2":
            return httpx.Response(302, headers={"location": "https://inside.example/3"})
        raise AssertionError("never reached")

    with pytest.raises(FetchRefused) as refused:
        await fetch_status(
            "https://lists.example/1",
            resolve=_resolver(addresses),
            transport=httpx.MockTransport(handler),
        )
    assert refused.value.code == "not_public"

    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    with pytest.raises(FetchRefused) as refused:
        await fetch_status(
            "https://lists.example/1",
            resolve=_resolver(PUBLIC),
            transport=httpx.MockTransport(loop),
        )
    assert refused.value.code == "too_many_redirects"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(404), "not_found"),
        (
            httpx.Response(200, headers={"content-length": str(10**9)}, content=b"x"),
            "too_large",
        ),
        (httpx.Response(200, content=b"x" * (presentations.FETCH_MOST + 1)), "too_large"),
        (httpx.Response(200, headers={"content-encoding": "gzip"}, content=b"x"), "encoded"),
    ],
)
async def test_too_large_missing_or_encoded_is_refused(response: httpx.Response, code: str) -> None:
    with pytest.raises(FetchRefused) as refused:
        await fetch_status(
            "https://lists.example/1",
            resolve=_resolver(PUBLIC),
            transport=httpx.MockTransport(lambda _: response),
        )
    assert refused.value.code == code


def test_a_list_is_inflated_no_further_than_asked() -> None:
    # A gigabyte of nothing, compressed to about a megabyte.
    bomb = zlib.compress(bytes(64 * 1024 * 1024), 9)
    assert presentations._inflate(bomb, zlib.MAX_WBITS, 4) == bytes(4)
    # Past the most a list is ever read to, or past its end, it is not believed.
    with pytest.raises(Problem):
        presentations._inflate(bomb, zlib.MAX_WBITS, presentations.INFLATE_MOST + 1)
    with pytest.raises(Problem):
        presentations._inflate(zlib.compress(bytes(4)), zlib.MAX_WBITS, 5)
    assert presentations._inflate(gzip.compress(b"\x80"), 16 + zlib.MAX_WBITS, 1) == b"\x80"


async def test_a_body_with_no_length_is_cut_at_the_most() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(presentations.FETCH_MOST // 65536 + 2):
            yield b"x" * 65536

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    with pytest.raises(FetchRefused) as refused:
        await fetch_status(
            "https://lists.example/1",
            resolve=_resolver(PUBLIC),
            transport=httpx.MockTransport(handler),
        )
    assert refused.value.code == "too_large"
