"""One simulated KNX installation exposed through its own KNXnet/IP tunnelling server.

Every instance owns a dedicated UDP port, its own virtual bus, devices, serial
number and MAC address, so many instances can share a single IP address.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import struct
import time
from collections import deque
from dataclasses import replace
from pathlib import Path

from . import dpt
from .devices import Device
from .protocol import (
    CONNECT_REQUEST, CONNECT_RESPONSE, CONNECTIONSTATE_REQUEST, CONNECTIONSTATE_RESPONSE,
    DESCRIPTION_REQUEST, DESCRIPTION_RESPONSE, DEVICE_CONFIGURATION_ACK,
    DEVICE_CONFIGURATION_REQUEST, DEVICE_MGMT_CONNECTION, DISCONNECT_REQUEST,
    DISCONNECT_RESPONSE, E_CONNECTION_ID, E_CONNECTION_TYPE, E_HOST_PROTOCOL_TYPE, E_NO_ERROR,
    E_NO_MORE_CONNECTIONS, E_TUNNELLING_LAYER, GROUP_READ, L_DATA_IND, L_DATA_REQ,
    M_PROPREAD_CON, M_PROPREAD_REQ, M_PROPWRITE_CON, M_PROPWRITE_REQ, MULTICAST_GROUP,
    SEARCH_REQUEST, SEARCH_RESPONSE, TUNNEL_CONNECTION, TUNNEL_LINKLAYER, TUNNELLING_ACK,
    TUNNELLING_REQUEST, LData, decode_group, describe, endpoint, frame, ga_to_str, hpai,
    ia_from_str, ia_to_str, parse_frame, parse_hpai,
)

log = logging.getLogger(__name__)

DEFAULT_DEVICES = json.loads((Path(__file__).parent / "default_installation.json").read_text(encoding="utf-8"))["devices"]
HEARTBEAT_TIMEOUT = 120.0  # KNXnet/IP: server drops a connection without heartbeat after 120 s


class Connection:
    """A tunnelling or device-management connection from a KNXnet/IP client."""

    def __init__(self, inst: Instance, channel: int, kind: int, ia: int | None,
                 ctrl: tuple[str, int], data: tuple[str, int]):
        self.inst, self.channel, self.kind, self.ia = inst, channel, kind, ia
        self.ctrl, self.data = ctrl, data
        tunnel = kind == TUNNEL_CONNECTION
        self.service = TUNNELLING_REQUEST if tunnel else DEVICE_CONFIGURATION_REQUEST
        self.rx = self.tx = 0
        self.last_seen = time.monotonic()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._ack: asyncio.Future | None = None
        self._task = asyncio.get_running_loop().create_task(self._sender())

    def send_cemi(self, cemi: bytes) -> None:
        self._queue.put_nowait(cemi)

    async def _sender(self) -> None:
        """Server -> client requests, one at a time: wait 1 s for the ACK, repeat once."""
        loop = asyncio.get_running_loop()
        while True:
            cemi = await self._queue.get()
            for _ in range(2):
                self._ack = loop.create_future()
                self.inst.sendto(frame(self.service, bytes([4, self.channel, self.tx, 0]) + cemi), self.data)
                try:
                    await asyncio.wait_for(self._ack, 1.0)
                    break
                except asyncio.TimeoutError:  # not the builtin before Python 3.11
                    continue
            else:
                log.info("%s: channel %d not acknowledging, disconnecting", self.inst.name, self.channel)
                self.inst.drop(self, notify=True)
                return
            self.tx = (self.tx + 1) & 0xFF

    def on_ack(self, seq: int) -> None:
        if self._ack and not self._ack.done() and seq == self.tx:
            self._ack.set_result(None)

    def close(self) -> None:
        self._task.cancel()

    def snapshot(self) -> dict:
        return {"channel": self.channel, "peer": f"{self.ctrl[0]}:{self.ctrl[1]}",
                "kind": "tunnel" if self.kind == TUNNEL_CONNECTION else "device-management",
                "ia": ia_to_str(self.ia) if self.ia is not None else None}


class Instance(asyncio.DatagramProtocol):
    def __init__(self, host, uid: int, cfg: dict, port: int):
        self.host, self.id, self.port = host, uid, port
        self.name = cfg.get("name") or f"KNX Sim {uid}"
        self.ia = ia_from_str(cfg.get("interface_ia", "1.1.250"))
        tunnels = int(cfg.get("tunnels", 4))
        if not 1 <= tunnels or (self.ia & 0xFF) + tunnels > 255:
            raise ValueError(f"{self.name}: {tunnels} tunnel addresses after {ia_to_str(self.ia)} do not fit")
        self.tunnel_ias = [self.ia + 1 + i for i in range(tunnels)]
        port_bytes = struct.pack(">H", port)  # ports are unique per machine -> unique ids
        self.serial = b"\x00\x00KS" + port_bytes
        self.mac = b"\x02KS\x00" + port_bytes
        self.devices = [Device(self, d, i, b"\x00\x00" + port_bytes + struct.pack(">H", i + 1))
                        for i, d in enumerate(cfg.get("devices") or DEFAULT_DEVICES)]
        self.connections: dict[int, Connection] = {}
        self.monitor: deque[dict] = deque(maxlen=300)
        self.transport: asyncio.DatagramTransport | None = None
        self._bus: deque = deque()
        self._delivering = False
        self._next_channel = 1
        self._handlers = {
            SEARCH_REQUEST: self._search, DESCRIPTION_REQUEST: self._description,
            CONNECT_REQUEST: self._connect, CONNECTIONSTATE_REQUEST: self._connectionstate,
            DISCONNECT_REQUEST: self._disconnect, TUNNELLING_REQUEST: self._request,
            DEVICE_CONFIGURATION_REQUEST: self._request,
            TUNNELLING_ACK: self._ack, DEVICE_CONFIGURATION_ACK: self._ack,
        }

    # --- UDP plumbing ------------------------------------------------------
    def connection_made(self, transport):
        self.transport = transport

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        if self.transport:
            self.transport.sendto(data, addr)

    def datagram_received(self, data: bytes, addr) -> None:
        parsed = parse_frame(data)
        if not parsed:
            return
        service, body = parsed
        handler = self._handlers.get(service)
        if handler is None:
            return
        try:
            handler(service, body, addr)
        except (IndexError, ValueError, struct.error, OSError) as exc:
            log.debug("%s: bad 0x%04X frame from %s: %s", self.name, service, addr, exc)

    def error_received(self, exc):  # e.g. ICMP port unreachable from a vanished client
        log.debug("%s: %s", self.name, exc)

    def local_hpai(self, remote_ip: str) -> bytes:
        return hpai(self.host.local_ip_for(remote_ip), self.port)

    # --- discovery / description ------------------------------------------
    def dibs(self) -> bytes:
        name = self.name.encode("latin-1", "replace")[:29]
        device = struct.pack(">BBBBHH6s4s6s30s", 0x36, 0x01, 0x02, 0x00, self.ia, 0, self.serial,
                             socket.inet_aton(MULTICAST_GROUP), self.mac, name)
        families = bytes([0x08, 0x02, 0x02, 0x01, 0x03, 0x01, 0x04, 0x01])  # core, devmgmt, tunnelling v1
        return device + families

    def search_response(self, target: tuple[str, int]) -> bytes:
        return frame(SEARCH_RESPONSE, self.local_hpai(target[0]) + self.dibs())

    def _search(self, _service, body, addr):
        target = endpoint(parse_hpai(body), addr)
        self.sendto(self.search_response(target), target)

    def _description(self, _service, body, addr):
        self.sendto(frame(DESCRIPTION_RESPONSE, self.dibs()), endpoint(parse_hpai(body), addr))

    # --- connection management --------------------------------------------
    def _connect(self, _service, body, addr):
        ctrl_hp, data_hp = parse_hpai(body, 0), parse_hpai(body, 8)
        ctrl = endpoint(ctrl_hp, addr)

        def fail(status):
            log.info("%s: refusing connection from %s (status 0x%02X)", self.name, ctrl, status)
            self.sendto(frame(CONNECT_RESPONSE, bytes([0, status])), ctrl)

        if ctrl_hp[0] != 1 or data_hp[0] != 1:
            return fail(E_HOST_PROTOCOL_TYPE)
        cri = body[16:]
        kind = cri[1]
        if kind == TUNNEL_CONNECTION:
            if cri[2] != TUNNEL_LINKLAYER:
                return fail(E_TUNNELLING_LAYER)
            used = {c.ia for c in self.connections.values()}
            free = [ia for ia in self.tunnel_ias if ia not in used]
            if not free:
                return fail(E_NO_MORE_CONNECTIONS)
            ia = free[0]
            crd = bytes([4, TUNNEL_CONNECTION]) + struct.pack(">H", ia)
        elif kind == DEVICE_MGMT_CONNECTION:
            ia, crd = None, bytes([2, DEVICE_MGMT_CONNECTION])
        else:
            return fail(E_CONNECTION_TYPE)
        channel = self._allocate_channel()
        if channel is None:
            return fail(E_NO_MORE_CONNECTIONS)
        conn = Connection(self, channel, kind, ia, ctrl, endpoint(data_hp, addr))
        self.connections[channel] = conn
        self.sendto(frame(CONNECT_RESPONSE, bytes([channel, E_NO_ERROR]) + self.local_hpai(addr[0]) + crd), ctrl)
        log.info("%s: %s connected on channel %d%s", self.name, ctrl, channel,
                 f" as {ia_to_str(ia)}" if ia is not None else " (device management)")
        self.changed()

    def _allocate_channel(self) -> int | None:
        for i in range(255):
            ch = (self._next_channel + i - 1) % 255 + 1
            if ch not in self.connections:
                self._next_channel = ch % 255 + 1
                return ch
        return None

    def _connectionstate(self, _service, body, addr):
        conn = self.connections.get(body[0])
        if conn:
            conn.last_seen = time.monotonic()
        status = E_NO_ERROR if conn else E_CONNECTION_ID
        self.sendto(frame(CONNECTIONSTATE_RESPONSE, bytes([body[0], status])), endpoint(parse_hpai(body, 2), addr))

    def _disconnect(self, _service, body, addr):
        conn = self.connections.get(body[0])
        status = E_NO_ERROR if conn else E_CONNECTION_ID
        self.sendto(frame(DISCONNECT_RESPONSE, bytes([body[0], status])), endpoint(parse_hpai(body, 2), addr))
        if conn:
            self.drop(conn, notify=False)

    def drop(self, conn: Connection, notify: bool) -> None:
        if self.connections.pop(conn.channel, None) is None:
            return
        conn.close()
        if notify:
            self.sendto(frame(DISCONNECT_REQUEST, bytes([conn.channel, 0]) + self.local_hpai(conn.ctrl[0])),
                        conn.ctrl)
        log.info("%s: channel %d closed", self.name, conn.channel)
        self.changed()

    def expire(self) -> None:
        now = time.monotonic()
        for conn in list(self.connections.values()):
            if now - conn.last_seen > HEARTBEAT_TIMEOUT:
                self.drop(conn, notify=True)

    # --- tunnelling / device configuration --------------------------------
    def _request(self, service, body, addr):
        channel, seq = body[1], body[2]
        conn = self.connections.get(channel)
        if conn is None or conn.service != service:
            return
        conn.last_seen = time.monotonic()
        ack = TUNNELLING_ACK if service == TUNNELLING_REQUEST else DEVICE_CONFIGURATION_ACK
        if seq == (conn.rx - 1) & 0xFF:  # repeated request: ack again, don't execute twice
            self.sendto(frame(ack, bytes([4, channel, seq, E_NO_ERROR])), conn.data)
            return
        if seq != conn.rx:
            return
        self.sendto(frame(ack, bytes([4, channel, seq, E_NO_ERROR])), conn.data)
        conn.rx = (conn.rx + 1) & 0xFF
        cemi = body[body[0]:]
        if service == TUNNELLING_REQUEST:
            self._from_client(conn, cemi)
        else:
            self._device_config(conn, cemi)

    def _ack(self, _service, body, addr):
        conn = self.connections.get(body[1])
        if conn:
            conn.on_ack(body[2])

    def _from_client(self, conn: Connection, cemi: bytes) -> None:
        if not cemi or cemi[0] != L_DATA_REQ:
            return
        f = LData.decode(cemi)
        if f.src == 0:
            f = replace(f, src=conn.ia)
        conn.send_cemi(f.confirm(self._reachable(f, conn)).encode())
        self.transmit(replace(f, code=L_DATA_IND), origin=conn)

    def _reachable(self, f: LData, origin) -> bool:
        """Emulates the TP link-layer ACK: individual frames need an existing receiver."""
        if f.group:
            return True
        return (any(d.ia == f.dst for d in self.devices)
                or any(c.ia == f.dst and c is not origin for c in self.connections.values()))

    def _device_config(self, conn: Connection, cemi: bytes) -> None:
        if len(cemi) < 7 or cemi[0] not in (M_PROPREAD_REQ, M_PROPWRITE_REQ):
            return
        iot, pid, b1, b2 = struct.unpack(">H", cemi[1:3])[0], cemi[4], cemi[5], cemi[6]
        noe, six = b1 >> 4, (b1 & 0xF) << 8 | b2
        head, negative = cemi[1:7], cemi[1:5] + bytes([b1 & 0x0F, b2])
        if cemi[0] == M_PROPREAD_REQ:
            props = {(0, 1): b"\x00\x00", (0, 11): self.serial, (0, 12): self.serial[:2],
                     (0, 56): b"\x00\x0F", (8, 1): b"\x00\x08", (8, 51): b"\x00\x02",
                     (8, 52): b"\x00", (8, 53): b"\x00\x02"}
            value = props.get((iot, pid))
            if value is not None and noe == 1 and six <= 1:
                conn.send_cemi(bytes([M_PROPREAD_CON]) + head + (b"\x00\x01" if six == 0 else value))
            else:
                conn.send_cemi(bytes([M_PROPREAD_CON]) + negative + b"\x07")  # void DP
        elif (iot, pid) == (8, 52) and cemi[7:8] == b"\x00":  # comm mode: data link layer
            conn.send_cemi(bytes([M_PROPWRITE_CON]) + head)
        else:
            conn.send_cemi(bytes([M_PROPWRITE_CON]) + negative + b"\x05")  # read only

    # --- the virtual bus ---------------------------------------------------
    def transmit(self, f: LData, origin=None) -> None:
        """Put a frame on the bus. Frames are delivered in FIFO order, never recursively."""
        self._bus.append((f, origin))
        if self._delivering:
            return
        self._delivering = True
        try:
            while self._bus:
                self._deliver(*self._bus.popleft())
        finally:
            self._delivering = False

    def _deliver(self, f: LData, origin) -> None:
        self._record(f, origin)
        raw = f.encode()  # always an L_Data.ind here
        for conn in list(self.connections.values()):
            if conn is not origin and conn.kind == TUNNEL_CONNECTION and (f.group or f.dst == conn.ia):
                conn.send_cemi(raw)
        for dev in self.devices:
            if dev is not origin:
                try:
                    dev.receive(f)
                except Exception:
                    log.exception("%s: device %s failed on %s", self.name, dev.name, describe(f))

    def _record(self, f: LData, origin) -> None:
        value, kind = self._monitor_value(f)
        entry = {"time": time.time(), "src": ia_to_str(f.src),
                 "dst": ga_to_str(f.dst) if f.group else ia_to_str(f.dst), "service": describe(f),
                 "data": f.tpdu.hex(" "), "value": value, "dpt": kind,
                 "origin": "client" if isinstance(origin, Connection) else "device"}
        self.monitor.append(entry)
        self.host.emit({"type": "telegram", "instance": self.id, **entry})

    def _monitor_value(self, f: LData) -> tuple[str | None, str | None]:
        """Decode a group value using the DPT of the first object linked to the address."""
        if not f.group or f.dst == 0 or len(f.tpdu) < 2 or decode_group(f.tpdu)[0] == GROUP_READ:
            return None, None
        payload = decode_group(f.tpdu)[1]
        for dev in self.devices:
            for ch in dev.channels:
                for key, gas in ch.ga.items():
                    if f.dst in gas:
                        kind = ch.objects[key][0]
                        try:
                            return str(dpt.decode(kind, payload)), kind
                        except (ValueError, IndexError, struct.error):
                            return None, kind
        return None, None

    # --- UI / lifecycle ------------------------------------------------------
    def changed(self) -> None:
        self.host.emit({"type": "state", "instance": self.id})

    def action(self, device: int, channel: int | None, name: str, value) -> None:
        dev = self.devices[device]
        if channel is None:
            dev.action(name, value)
        else:
            dev.channels[channel].action(name, value)

    def stop(self) -> None:
        for conn in list(self.connections.values()):
            self.drop(conn, notify=True)
        for dev in self.devices:
            dev.stop()
        if self.transport:
            self.transport.close()

    def snapshot(self) -> dict:
        return {"id": self.id, "name": self.name, "port": self.port,
                "ip": self.host.advertised_ip(), "ia": ia_to_str(self.ia),
                "tunnel_ias": [ia_to_str(i) for i in self.tunnel_ias],
                "connections": [c.snapshot() for c in self.connections.values()],
                "devices": [d.snapshot() for d in self.devices]}
