import asyncio
import base64

import pytest

from arp_harvester.proxy_relay import AuthProxyRelay


async def _upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    first = await reader.readline()
    headers = b""
    auth = b""
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        headers += line
        if line.lower().startswith(b"proxy-authorization:"):
            auth = line
    if b"Basic" not in auth:
        writer.write(b"HTTP/1.1 407 Proxy Auth Required\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    if first.upper().startswith(b"CONNECT"):
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        data = await reader.read(16)
        writer.write(data)
        await writer.drain()
    else:
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
    writer.close()


@pytest.mark.asyncio
async def test_relay_injects_proxy_authorization_on_connect() -> None:
    server = await asyncio.start_server(_upstream, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    token = base64.b64encode(b"user:secret").decode()
    relay = await AuthProxyRelay.start(f"http://user:secret@127.0.0.1:{port}")
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", int(relay.local_url.rsplit(":", 1)[1]))
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        await writer.drain()
        status = await reader.readline()
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        writer.write(b"ping-from-client")
        await writer.drain()
        echoed = await reader.read(16)
        writer.close()
        await writer.wait_closed()
    finally:
        await relay.close()
        server.close()
        await server.wait_closed()

    assert b"200" in status
    assert echoed == b"ping-from-client"
    assert "secret" not in relay.local_url
    assert token  # used by dummy via relay


@pytest.mark.asyncio
async def test_relay_rewrites_connect_like_curl_cffi() -> None:
    captured: list[bytes] = []

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = await reader.read(1024)
            if not chunk:
                break
            buf.extend(chunk)
        captured.append(bytes(buf))
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    relay = await AuthProxyRelay.start(f"http://user:secret@127.0.0.1:{port}")
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", int(relay.local_url.rsplit(":", 1)[1])
        )
        writer.write(
            b"CONNECT example.com:443 HTTP/1.1\r\n"
            b"Host: example.com:443\r\n"
            b"Proxy-Connection: keep-alive\r\n"
            b"User-Agent: Mozilla/5.0 Chrome/148.0.0.0\r\n\r\n"
        )
        await writer.drain()
        await reader.readline()
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        writer.close()
        await writer.wait_closed()
    finally:
        await relay.close()
        server.close()
        await server.wait_closed()

    sent = captured[0].decode("latin1")
    assert sent.startswith("CONNECT example.com:443 HTTP/1.1\r\n")
    assert "Host: example.com:443\r\n" in sent
    assert "Proxy-Authorization: Basic " in sent
    assert "Proxy-Connection: Keep-Alive\r\n" in sent
    assert "User-Agent" not in sent
    assert "secret" not in relay.local_url
