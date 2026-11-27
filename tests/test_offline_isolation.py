import asyncio
import errno
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_conftest_overrides_inherited_live_configuration():
    # R7-01: a developer shell must not turn the offline suite into a live client.
    env = dict(os.environ)
    env.update({
        "BOT_TOKEN": "inherited-token-not-real",
        "GEMINI_API_KEY": "inherited-key-not-real",
        "UPSTASH_REDIS_REST_URL": "https://inherited.invalid",
        "UPSTASH_REDIS_REST_TOKEN": "inherited-token-not-real",
        "USE_UPSTASH": "1",
        "MUNITO_API_KEY": "inherited-key-not-real",
        "CLOUDFLARE_API_TOKEN": "inherited-token-not-real",
        "TAVILY_API_KEY": "inherited-key-not-real",
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    script = """
import runpy
runpy.run_path("tests/conftest.py")
import bot
assert bot.USE_UPSTASH is False
assert bot.UPSTASH_REDIS_REST_URL == ""
assert bot.UPSTASH_REDIS_REST_TOKEN == ""
assert bot.BOT_TOKEN == "test-token-not-real"
assert bot.GEMINI_API_KEY == "test-key-not-real"
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_offline_guard_blocks_external_connects():
    with socket.socket() as sock:
        with pytest.raises(OSError, match="External network access"):
            sock.connect(("93.184.216.34", 443))
        assert sock.connect_ex(("93.184.216.34", 443)) == errno.EACCES


def test_offline_guard_allows_loopback_connect():
    with socket.socket() as server, socket.socket() as client:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client.connect(server.getsockname())
        connection, _ = server.accept()
        with connection:
            assert connection.family == socket.AF_INET


def test_offline_guard_blocks_async_external_connect():
    async def scenario():
        loop = asyncio.get_running_loop()
        with socket.socket() as sock:
            with pytest.raises(OSError, match="External network access"):
                await loop.sock_connect(sock, ("93.184.216.34", 443))

    asyncio.run(scenario())


def test_offline_guard_allows_async_loopback_connect():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)

        async def scenario():
            loop = asyncio.get_running_loop()
            with socket.socket() as client:
                await loop.sock_connect(client, server.getsockname())

        asyncio.run(scenario())
        connection, _ = server.accept()
        with connection:
            assert connection.family == socket.AF_INET


def test_offline_guard_allows_unix_connect(tmp_path):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("Unix sockets are unavailable")
    address = str(tmp_path / "test.sock")
    with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
        server.bind(address)
        server.listen(1)
        client.connect(address)
        connection, _ = server.accept()
        with connection:
            assert connection.family == socket.AF_UNIX


def test_offline_guard_blocks_external_dns():
    with pytest.raises(socket.gaierror, match="External DNS"):
        socket.getaddrinfo("external.invalid", 443)


@pytest.mark.parametrize("resolver_name,args", [
    ("gethostbyname", ("localhost",)),
    ("gethostbyname_ex", ("localhost",)),
    ("gethostbyaddr", ("127.0.0.1",)),
    ("getnameinfo", (("93.184.216.34", 443), 0)),
])
def test_offline_guard_blocks_other_dns_entrypoints(resolver_name, args):
    with pytest.raises(socket.gaierror, match="External DNS"):
        getattr(socket, resolver_name)(*args)


def test_offline_guard_allows_test_dns_override(monkeypatch):
    calls = []

    def fake_dns(host, port, *args, **kwargs):
        calls.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)
    assert socket.getaddrinfo("media.example", 443)[0][4][1] == 443
    assert calls == [("media.example", 443)]
