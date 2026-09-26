"""Local HTTP proxy that injects upstream Proxy-Authorization.

fingerprint-chromium 148 rejects ``--proxy-server=http://user:pass@host``
(``ERR_NO_SUPPORTED_PROXIES``), and Playwright's split username/password never
puts ``Proxy-Authorization`` on CONNECT for this binary. The browser talks to
127.0.0.1 with no secrets; we rewrite CONNECT to the same four lines curl_cffi
sends (Host + Basic auth + Proxy-Connection). ARP bundles keep the original
``proxy_url``.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from urllib.parse import unquote

from arp_harvester.proxies import parse_proxy

logger = logging.getLogger("arp_harvester")
_HEADER_END = (b"\r\n", b"\n", b"")


class AuthProxyRelay:
    def __init__(self, proxy_url: str) -> None:
        self.proxy_url = proxy_url
        self._server: asyncio.AbstractServer | None = None
        self._host = "127.0.0.1"
        self._port = 0
        record = parse_proxy(proxy_url, default_scheme="http")
        if record.scheme not in {"http", "https"}:
            raise ValueError("auth relay only supports HTTP proxies")
        self._upstream_host = record.host
        self._upstream_port = record.port
        user = unquote(record.username)
        password = unquote(record.password)
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = f"Proxy-Authorization: Basic {token}\r\n".encode()

    @property
    def local_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    @classmethod
    async def start(cls, proxy_url: str) -> AuthProxyRelay:
        relay = cls(proxy_url)
        relay._server = await asyncio.start_server(relay._client, relay._host, 0)
        sockets = relay._server.sockets
        if not sockets:
            raise RuntimeError("proxy relay failed to bind")
        relay._port = int(sockets[0].getsockname()[1])
        return relay

    def playwright_proxy(self) -> dict[str, str]:
        return {"server": self.local_url}

    async def close(self) -> None:
        server = self._server
        self._server = None
        if server is None:
            return
        server.close()
        await server.wait_closed()

    def _connect_request(self, target: bytes) -> bytes:
        return (
            b"CONNECT "
            + target
            + b" HTTP/1.1\r\nHost: "
            + target
            + b"\r\n"
            + self._auth
            + b"Proxy-Connection: Keep-Alive\r\n\r\n"
        )

    async def _client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        upstream = None
        try:
            first = await reader.readline()
            if not first:
                return
            while True:
                line = await reader.readline()
                if line in _HEADER_END:
                    break
            parts = first.split()
            if len(parts) < 2:
                return
            method = parts[0].upper()
            target = parts[1]
            upstream = await asyncio.wait_for(
                asyncio.open_connection(self._upstream_host, self._upstream_port),
                timeout=15,
            )
            up_reader, up_writer = upstream
            if method == b"CONNECT":
                up_writer.write(self._connect_request(target))
                await up_writer.drain()
                status = await up_reader.readline()
                writer.write(status)
                while True:
                    line = await up_reader.readline()
                    writer.write(line)
                    if line in _HEADER_END:
                        break
                await writer.drain()
                if b" 200 " not in status:
                    return
                await asyncio.gather(
                    _pipe(reader, up_writer),
                    _pipe(up_reader, writer),
                    return_exceptions=True,
                )
                return
            up_writer.write(first)
            up_writer.write(self._auth)
            up_writer.write(b"Proxy-Connection: Keep-Alive\r\n\r\n")
            await up_writer.drain()
            await asyncio.gather(
                _pipe(reader, up_writer),
                _pipe(up_reader, writer),
                return_exceptions=True,
            )
        except Exception:
            logger.warning("proxy relay client dropped")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            if upstream is not None:
                _up_reader, up_writer = upstream
                up_writer.close()
                try:
                    await up_writer.wait_closed()
                except Exception:
                    pass


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except Exception:
        return
    finally:
        try:
            writer.close()
        except Exception:
            pass
