"""Process-level host: shared discovery socket + any number of instances.

Why many instances fit on one IP:
  * Port 3671 is used only for discovery (SEARCH_REQUEST on 224.0.23.12). That
    socket is opened with SO_REUSEADDR/SO_REUSEPORT, so several simulator
    processes - and several instances inside one process - can all listen.
  * Every instance answers a search with its *own* control endpoint port
    (3672, 3673, ... by default) plus a unique serial number and MAC, so ETS
    lists them as separate interfaces and connects to the right one.
"""
from __future__ import annotations

import asyncio
import logging
import socket

from .instance import Instance
from .protocol import KNX_PORT, MULTICAST_GROUP, SEARCH_REQUEST, endpoint, parse_frame, parse_hpai

log = logging.getLogger(__name__)


class _Discovery(asyncio.DatagramProtocol):
    def __init__(self, host: Host):
        self.host = host
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        parsed = parse_frame(data)
        if not parsed or parsed[0] != SEARCH_REQUEST:
            return
        try:
            target = endpoint(parse_hpai(parsed[1]), addr)
        except (IndexError, OSError):
            return
        for inst in list(self.host.instances.values()):
            self.transport.sendto(inst.search_response(target), target)

    def error_received(self, exc):
        log.debug("discovery: %s", exc)


class Host:
    def __init__(self, ip: str | None = None, base_port: int = KNX_PORT + 1, bind: str = "0.0.0.0"):
        self.ip, self.base_port, self.bind = ip, base_port, bind
        self.instances: dict[int, Instance] = {}
        self.listeners: set[asyncio.Queue] = set()
        self.discovery_active = False
        self._next_id = 1
        self._ip_cache: dict[str, str] = {}
        self._discovery = None
        self._reaper: asyncio.Task | None = None

    async def start(self, discovery: bool = True) -> None:
        if discovery:
            await self._start_discovery()
        self._reaper = asyncio.get_running_loop().create_task(self._expire_loop())

    async def _start_discovery(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):  # Linux/macOS need this to share the port between processes
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            sock.bind(("", KNX_PORT))
        except OSError as exc:
            sock.close()
            log.warning("discovery disabled: cannot bind UDP %d (%s). Is another KNX tool holding it "
                        "exclusively? Clients can still connect to IP:port directly.", KNX_PORT, exc)
            return
        joined = []
        for ifip in self._interfaces():
            try:
                mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton(ifip)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                joined.append(ifip)
            except OSError:
                pass  # interface without multicast, or already joined via an alias
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        self._discovery, _ = await loop.create_datagram_endpoint(lambda: _Discovery(self), sock=sock)
        self.discovery_active = True
        log.info("discovery listening on UDP %d, %s joined on %s", KNX_PORT, MULTICAST_GROUP,
                 ", ".join(joined) or "no interface")

    def _interfaces(self) -> list[str]:
        ips = {self.ip} if self.ip else set()
        if not self.ip:
            ips.add(self.local_ip_for(MULTICAST_GROUP))
            try:
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                    ips.add(info[4][0])
            except OSError:
                pass
        ips = {ip for ip in ips if not ip.startswith("127.")}
        return sorted(ips) or ["0.0.0.0"]

    def local_ip_for(self, remote_ip: str) -> str:
        """The local address a client at remote_ip can reach us on (goes into HPAIs)."""
        if self.ip:
            return self.ip
        if remote_ip not in self._ip_cache:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect((remote_ip, KNX_PORT))  # no packet is sent for UDP connect
                self._ip_cache[remote_ip] = probe.getsockname()[0]
            except OSError:
                self._ip_cache[remote_ip] = "127.0.0.1"
            finally:
                probe.close()
        return self._ip_cache[remote_ip]

    def advertised_ip(self) -> str:
        return self.local_ip_for(MULTICAST_GROUP)

    async def add_instance(self, cfg: dict | None = None) -> Instance:
        cfg = dict(cfg or {})
        if cfg.get("port"):
            ports = [int(cfg["port"])]
        else:
            ports = range(self.base_port, self.base_port + 500)
        used = {i.port for i in self.instances.values()}
        for port in ports:
            if port == KNX_PORT and self.discovery_active:
                raise ValueError(f"port {KNX_PORT} is reserved for discovery; use another port")
            if port in used:
                if cfg.get("port"):
                    raise ValueError(f"port {port} is already used by another instance")
                continue
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind((self.bind, port))
            except OSError:
                sock.close()
                if cfg.get("port"):
                    raise
                continue  # taken by another process (e.g. a second simulator) -> next port
            try:
                inst = Instance(self, self._next_id, cfg, port)
            except Exception:
                sock.close()
                raise
            sock.setblocking(False)
            await asyncio.get_running_loop().create_datagram_endpoint(lambda: inst, sock=sock)
            self._next_id += 1
            self.instances[inst.id] = inst
            self.emit({"type": "instances"})
            log.info("instance %d %r listening on UDP %d", inst.id, inst.name, port)
            return inst
        raise RuntimeError("no free UDP port found for a new instance")

    def remove_instance(self, uid: int) -> None:
        inst = self.instances.pop(uid)
        inst.stop()
        self.emit({"type": "instances"})

    def emit(self, event: dict) -> None:
        for q in list(self.listeners):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow UI client; it will resync from /api/state

    async def _expire_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            for inst in list(self.instances.values()):
                inst.expire()

    def stop(self) -> None:
        for uid in list(self.instances):
            self.remove_instance(uid)
        if self._discovery:
            self._discovery.close()
        if self._reaper:
            self._reaper.cancel()

    def snapshot(self) -> dict:
        return {"ip": self.advertised_ip(), "discovery": self.discovery_active,
                "instances": [i.snapshot() for i in self.instances.values()]}
