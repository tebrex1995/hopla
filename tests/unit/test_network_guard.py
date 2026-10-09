"""No test may reach the network (NFR-060): pytest-socket blocks connect() to all but loopback.

These tests fail within seconds (they don't hang or pass) if the guard flags disappear
from the pytest addopts. pytest-socket guards connect() only, not DNS lookups or UDP,
so addresses here are IP literals from TEST-NET-1 (RFC 5737); HTTP in tests goes through respx.
"""

import socket

import httpx
import pytest
from pytest_socket import SocketConnectBlockedError

UNROUTABLE = "192.0.2.1"


def test_raw_socket_to_a_public_address_is_blocked() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        with pytest.raises(SocketConnectBlockedError):
            sock.connect((UNROUTABLE, 80))


def test_httpx_request_to_a_public_address_is_blocked() -> None:
    # httpx doesn't wrap the error into its own ConnectError: it surfaces unchanged.
    with pytest.raises(SocketConnectBlockedError):
        httpx.get(f"http://{UNROUTABLE}/", timeout=1)


async def test_async_httpx_request_to_a_public_address_is_blocked() -> None:
    # The pipeline is async (ADR-0001); this also proves async tests really run.
    # anyio's happy-eyeballs connect runs in a task group, so today the error arrives grouped.
    async with httpx.AsyncClient(timeout=1) as client:
        with pytest.RaisesGroup(SocketConnectBlockedError, allow_unwrapped=True):
            await client.get(f"http://{UNROUTABLE}/")


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_loopback_is_allowed(host: str) -> None:
    # Integration tests need a local Postgres (04 §2): the guard must not over-block.
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.settimeout(1)
        port = server.getsockname()[1]
        with socket.create_connection((host, port), timeout=1):
            conn, _ = server.accept()
            conn.close()
