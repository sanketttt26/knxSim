"""Tiny dependency-free HTTP server for the browser UI (JSON API + Server-Sent Events)."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}


class WebUI:
    def __init__(self, host, bind: str = "127.0.0.1", port: int = 8080):
        self.host, self.bind, self.port = host, bind, port
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        for port in range(self.port, self.port + 50):  # next free port if a 2nd process runs
            try:
                self.server = await asyncio.start_server(self._client, self.bind, port)
                self.port = port
                return
            except OSError:
                continue
        raise RuntimeError(f"no free TCP port for the web UI from {self.port}")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.strip().lower(): v.strip() for k, v in
                       (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
            body = await reader.readexactly(int(headers.get("content-length", 0)))
            path = target.split("?", 1)[0]
            if path == "/api/events":
                await self._events(writer)
                return
            status, ctype, payload = await self._route(method, path, body)
            writer.write(f"HTTP/1.1 {status} {REASONS.get(status, '')}\r\nContent-Type: {ctype}\r\n"
                         f"Content-Length: {len(payload)}\r\nCache-Control: no-store\r\n"
                         f"Connection: close\r\n\r\n".encode() + payload)
            await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()

    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        def js(obj, status=200):
            return status, "application/json", json.dumps(obj).encode()

        try:
            if path in ("/", "/index.html"):
                return 200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes()
            if path == "/api/state" and method == "GET":
                return js(self.host.snapshot())
            parts = path.strip("/").split("/")
            if parts[:2] == ["api", "instances"]:
                if len(parts) == 2 and method == "POST":
                    inst = await self.host.add_instance(json.loads(body or b"{}"))
                    return js({"id": inst.id})
                if len(parts) == 3 and method == "DELETE":
                    self.host.remove_instance(int(parts[2]))
                    return js({"ok": True})
                if len(parts) == 4 and parts[3] == "monitor" and method == "GET":
                    return js(list(self.host.instances[int(parts[2])].monitor))
                if len(parts) == 4 and parts[3] == "action" and method == "POST":
                    req = json.loads(body)
                    self.host.instances[int(parts[2])].action(
                        int(req["device"]), req.get("channel"), req["action"], req.get("value"))
                    return js({"ok": True})
            return js({"error": "not found"}, 404)
        except (KeyError, IndexError, ValueError, TypeError, RuntimeError, OSError) as exc:
            return js({"error": f"{type(exc).__name__}: {exc}"}, 400)

    async def _events(self, writer: asyncio.StreamWriter) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.host.listeners.add(queue)
        try:
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Cache-Control: no-store\r\nConnection: keep-alive\r\n\r\n")
            await writer.drain()
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), 15)
                    writer.write(f"data: {json.dumps(event)}\n\n".encode())
                except asyncio.TimeoutError:  # not the builtin before Python 3.11
                    writer.write(b": keepalive\n\n")
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self.host.listeners.discard(queue)

    def close(self) -> None:
        if self.server:
            self.server.close()
