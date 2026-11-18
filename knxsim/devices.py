"""Simulated KNX devices.

A Device has an individual address and one or more channels (functional blocks
such as a light output or a push-button). Each channel exposes group objects
that are linked to group addresses in the installation config.

Object flags: r = answers GroupValueRead, w = accepts GroupValueWrite,
u = updates from GroupValueResponse.
"""
from __future__ import annotations

import asyncio
import logging
import struct

from . import dpt
from .protocol import (
    AUTHORIZE_REQUEST, AUTHORIZE_RESPONSE, DEVICE_DESCRIPTOR_READ, DEVICE_DESCRIPTOR_RESPONSE,
    GROUP_READ, GROUP_RESPONSE, GROUP_WRITE, IA_READ, IA_RESPONSE, IA_SERIAL_READ,
    IA_SERIAL_RESPONSE, IA_SERIAL_WRITE, IA_WRITE, L_DATA_IND, MEMORY_READ, MEMORY_RESPONSE,
    MEMORY_WRITE, PROPERTY_READ, PROPERTY_RESPONSE, PROPERTY_WRITE, RESTART, T_CONNECT,
    T_DISCONNECT, LData, apdu, decode_group, ga_from_str, ga_to_str, group_apdu, ia_from_str,
    ia_to_str,
)

log = logging.getLogger(__name__)

MASK_VERSION = 0x07B0       # device descriptor type 0 reported to ETS (TP1 system B)
TRANSPORT_TIMEOUT = 6.0     # seconds, KNX connection-oriented idle timeout

# Synthetic manufacturer-data block. ETS reads it when it identifies a device,
# and the whole 64 KiB array is the download target for A_MemoryWrite.
MEMORY_SIZE = 0x10000
DEVICE_INFO_ADDRESS = 0x0100
APPLICATION_ADDRESS = 0x0110  # first byte of the "application" area

# Load state values reported to ETS (property objects 0 and 5).
LOAD_STATE_UNLOADED = 0
LOAD_STATE_LOADED = 1
LOAD_STATE_LOADING = 2
LOAD_STATE_ERROR = 3


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(v)))


class Channel:
    kind = ""
    objects: dict[str, tuple[str, str]] = {}

    def __init__(self, device: Device, cfg: dict, index: int):
        self.device = device
        self.index = index
        self.name = cfg.get("name") or f"{self.kind} {index + 1}"
        self.objects = self.object_spec(cfg)
        self.ga: dict[str, list[int]] = {}
        for key, val in (cfg.get("ga") or {}).items():
            if key not in self.objects:
                raise ValueError(f"{self.kind} channel {self.name!r}: unknown object {key!r} "
                                 f"(valid: {', '.join(self.objects)})")
            vals = val if isinstance(val, list) else [val]
            self.ga[key] = [ga_from_str(v) for v in vals if v not in (None, "")]
        self._sent: dict[str, object] = {}
        self._task: asyncio.Task | None = None
        self.setup(cfg)

    def object_spec(self, cfg: dict) -> dict[str, tuple[str, str]]:
        return dict(type(self).objects)

    def setup(self, cfg: dict) -> None:
        pass

    # bus side
    def send(self, key: str, value) -> None:
        gas = self.ga.get(key)
        if gas:
            payload = dpt.encode(self.objects[key][0], value)
            self.device.send(gas[0], True, group_apdu(GROUP_WRITE, payload))

    def publish(self, key: str, value) -> None:
        """Send a status object only when its value changed (also prevents bus loops)."""
        if self._sent.get(key, object()) != value:
            self._sent[key] = value
            self.send(key, value)

    def write(self, key: str, value) -> None:
        pass

    def read(self, key: str):
        return None

    # UI side
    def action(self, name: str, value) -> None:
        raise ValueError(f"{self.kind} has no action {name!r}")

    def state(self) -> dict:
        return {}

    def changed(self) -> None:
        self.device.instance.changed()

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    def snapshot(self) -> dict:
        return {"index": self.index, "kind": self.kind, "name": self.name,
                "ga": {k: [ga_to_str(g) for g in v] for k, v in self.ga.items()},
                "dpt": {k: v[0] for k, v in self.objects.items()}, "state": self.state()}


class Switch(Channel):
    kind = "switch"
    objects = {"switch": ("1.001", "w"), "status": ("1.001", "r")}

    def setup(self, cfg):
        self.on = bool(cfg.get("value", False))

    def set(self, on: bool):
        self.on = bool(on)
        self.publish("status", self.on)
        self.changed()

    def write(self, key, value):
        self.set(value)

    def read(self, key):
        return self.on

    def action(self, name, value):
        if name == "toggle":
            self.set(not self.on)
        elif name == "set":
            self.set(bool(value))
        else:
            super().action(name, value)

    def state(self):
        return {"on": self.on}


class Dimmer(Channel):
    kind = "dimmer"
    objects = {"switch": ("1.001", "w"), "dim": ("3.007", "w"), "value": ("5.001", "w"),
               "switch_status": ("1.001", "r"), "value_status": ("5.001", "r")}

    def setup(self, cfg):
        self.level = _clamp(cfg.get("value", 0))
        self.last_on = self.level or 100.0
        self.ramp_seconds = float(cfg.get("ramp_seconds", 4))

    def _apply(self, level):
        self.level = _clamp(level)
        if self.level > 0:
            self.last_on = self.level

    def _report(self):
        self.publish("switch_status", self.level > 0)
        self.publish("value_status", round(self.level))
        self.changed()

    def set(self, level):
        self.stop()
        self._apply(level)
        self._report()

    def dim(self, step: int):
        self.stop()
        if step == 0:  # break telegram
            self._report()
            return
        span = 100 / (1 << (abs(step) - 1))
        target = _clamp(self.level + span if step > 0 else self.level - span)
        self._task = asyncio.get_running_loop().create_task(self._ramp(target))

    async def _ramp(self, target):
        delta = 100 / self.ramp_seconds * 0.05
        while abs(self.level - target) > 1e-6:
            await asyncio.sleep(0.05)
            nxt = self.level + delta if target > self.level else self.level - delta
            self._apply(min(nxt, target) if target > self.level else max(nxt, target))
            self.changed()
        self._task = None
        self._report()

    def write(self, key, value):
        if key == "switch":
            self.set(self.last_on if value else 0)
        elif key == "value":
            self.set(value)
        elif key == "dim":
            self.dim(value)

    def read(self, key):
        return self.level > 0 if key == "switch_status" else round(self.level)

    def action(self, name, value):
        if name == "toggle":
            self.set(0 if self.level > 0 else self.last_on)
        elif name == "set":
            self.set(float(value))
        else:
            super().action(name, value)

    def state(self):
        return {"level": round(self.level, 1), "dimming": self._task is not None}


class Blind(Channel):
    kind = "blind"
    objects = {"move": ("1.008", "w"), "stop": ("1.007", "w"), "position": ("5.001", "w"),
               "position_status": ("5.001", "r")}

    def setup(self, cfg):
        self.position = _clamp(cfg.get("value", 0))  # 0 = fully up/open, 100 = down/closed
        self.travel_seconds = float(cfg.get("travel_seconds", 20))
        self.direction = 0

    def go(self, target):
        self.halt(report=False)
        target = _clamp(target)
        if abs(target - self.position) < 1e-6:
            self._report()
            return
        self.direction = 1 if target > self.position else -1
        self._task = asyncio.get_running_loop().create_task(self._run(target))
        self.changed()

    async def _run(self, target):
        delta = 100 / self.travel_seconds * 0.1
        while abs(self.position - target) > 1e-6:
            await asyncio.sleep(0.1)
            if self.direction > 0:
                self.position = min(target, self.position + delta)
            else:
                self.position = max(target, self.position - delta)
            self.changed()
        self._task = None
        self.direction = 0
        self._report()

    def halt(self, report=True):
        self.stop()
        self.direction = 0
        if report:
            self._report()

    def _report(self):
        self.publish("position_status", round(self.position))
        self.changed()

    def write(self, key, value):
        if key == "move":
            self.go(100 if value else 0)
        elif key == "stop":
            if self._task:
                self.halt()
            else:
                self.go(self.position + (5 if value else -5))
        elif key == "position":
            self.go(value)

    def read(self, key):
        return round(self.position)

    def action(self, name, value):
        actions = {"up": lambda: self.go(0), "down": lambda: self.go(100), "stop": self.halt,
                   "set": lambda: self.go(float(value))}
        if name not in actions:
            super().action(name, value)
        actions[name]()

    def state(self):
        return {"position": round(self.position, 1), "direction": self.direction}


class Button(Channel):
    """Wall push-button. Sends on 'switch'; tracks state from 'switch' and 'status'."""
    kind = "button"
    objects = {"switch": ("1.001", "wu"), "status": ("1.001", "wu")}

    def setup(self, cfg):
        self.on = False
        self.mode = cfg.get("mode", "toggle")
        if self.mode not in ("toggle", "on", "off"):
            raise ValueError(f"button mode must be toggle/on/off, not {self.mode!r}")

    def write(self, key, value):
        self.on = bool(value)
        self.changed()

    def action(self, name, value):
        if name != "press":
            super().action(name, value)
        self.on = {"toggle": not self.on, "on": True, "off": False}[self.mode]
        self.send("switch", self.on)
        self.changed()

    def state(self):
        return {"on": self.on, "mode": self.mode}


class Sensor(Channel):
    """Generic sensor (temperature, lux, presence, wind ...). Value is set from the UI."""
    kind = "sensor"

    def object_spec(self, cfg):
        return {"value": (cfg.get("dpt", "9.001"), "r")}

    def setup(self, cfg):
        self.dpt = self.objects["value"][0]
        self.binary = self.dpt.startswith("1.")
        self.value = self._coerce(cfg.get("value", 0))
        self.unit = cfg.get("unit", "")
        self.range = [cfg.get("min", 0), cfg.get("max", 1 if self.binary else 100), cfg.get("step", 1)]
        dpt.encode(self.dpt, self.value)  # validate DPT early

    def _coerce(self, v):
        return bool(v) if self.binary else float(v)

    def read(self, key):
        return self.value

    def action(self, name, value):
        if name != "set":
            super().action(name, value)
        self.value = self._coerce(value)
        self.send("value", self.value)
        self.changed()

    def state(self):
        return {"value": self.value, "unit": self.unit, "range": self.range, "binary": self.binary}


class Thermostat(Channel):
    """Room controller: measured temperature + setpoint -> heating valve output (P-control)."""
    kind = "thermostat"
    objects = {"temperature": ("9.001", "r"), "setpoint": ("9.001", "rw"), "heating": ("5.001", "r")}

    def setup(self, cfg):
        self.temperature = float(cfg.get("temperature", 20.0))
        self.setpoint = float(cfg.get("setpoint", 21.0))
        self.gain = float(cfg.get("gain", 50))  # % valve per Kelvin
        self.heating = self._output()
        self._sent["heating"] = self.heating

    def _output(self):
        return round(_clamp((self.setpoint - self.temperature) * self.gain))

    def _update(self):
        self.heating = self._output()
        self.publish("heating", self.heating)
        self.changed()

    def write(self, key, value):
        if key == "setpoint":
            self.setpoint = float(value)
            self._update()

    def read(self, key):
        return {"temperature": self.temperature, "setpoint": self.setpoint, "heating": self.heating}[key]

    def action(self, name, value):
        if name not in ("temperature", "setpoint"):
            super().action(name, value)
        setattr(self, name, float(value))
        self.send(name, float(value))
        self._update()

    def state(self):
        return {"temperature": self.temperature, "setpoint": self.setpoint, "heating": self.heating}


class Object(Channel):
    """Generic group object imported from an ETS project.

    One channel per communication object. ``flags`` uses the KNX letters
    r (read), w (write), u (update) and t (transmit): the object answers reads,
    accepts writes and, when it can transmit, sends the value on a UI action.
    """
    kind = "object"

    def object_spec(self, cfg):
        return {"value": (cfg.get("dpt", "1.001"), cfg.get("flags", "rw"))}

    def setup(self, cfg):
        self.number = int(cfg.get("number", self.index))
        self.dpt = self.objects["value"][0]
        self.flags = self.objects["value"][1]
        value = cfg.get("value")
        if value is None:
            value = False if self.dpt.split(".")[0] == "1" else 0
        try:
            self.value = dpt.decode(self.dpt, dpt.encode(self.dpt, value))
        except ValueError:
            self.value = None  # raw-hex DPT without a start value: answers reads once written

    def write(self, key, value):
        self.value = value
        self.changed()

    def read(self, key):
        return self.value

    def action(self, name, value):
        if name != "set":
            super().action(name, value)
        self.value = dpt.decode(self.dpt, dpt.encode(self.dpt, value))
        if "t" in self.flags:
            self.send("value", self.value)
        self.changed()

    def state(self):
        return {"value": self.value, "flags": self.flags, "number": self.number}


CHANNEL_TYPES = {cls.kind: cls for cls in (Switch, Dimmer, Blind, Button, Sensor, Thermostat, Object)}


class Device:
    def __init__(self, instance, cfg: dict, index: int, serial: bytes):
        self.instance = instance
        self.index = index
        self.ia = ia_from_str(cfg["ia"])
        self.name = cfg.get("name") or f"Device {cfg['ia']}"
        self.serial = serial
        self.prog_mode = False
        self.mask_version = int(cfg.get("mask_version", MASK_VERSION)) & 0xFFFF
        self.manufacturer_id = int(cfg.get("manufacturer_id", 0)) & 0xFFFF
        self.application_id = int(cfg.get("application_id", 0)) & 0xFFFF
        self.application_version = int(cfg.get("application_version", 0)) & 0xFF
        self.order_number = str(cfg.get("order_number", "KNXSIM"))
        self.load_state = int(cfg.get("load_state", LOAD_STATE_LOADED))
        self.properties: dict[tuple[int, int], bytes] = {}
        self.memory = self._initial_memory(int(cfg.get("memory_size", MEMORY_SIZE)))
        self.channels = []
        for i, ch in enumerate(cfg.get("channels", [])):
            kind = ch.get("type")
            if kind not in CHANNEL_TYPES:
                raise ValueError(f"device {cfg['ia']}: unknown channel type {kind!r} "
                                 f"(valid: {', '.join(CHANNEL_TYPES)})")
            self.channels.append(CHANNEL_TYPES[kind](self, ch, i))
        self._peer: int | None = None
        self._rx = self._tx = 0
        self._timer: asyncio.TimerHandle | None = None

    # --- download memory ---------------------------------------------------
    def _initial_memory(self, size: int) -> bytearray:
        """Manufacturer data at 0x0100, then the application download area."""
        memory = bytearray(max(size, APPLICATION_ADDRESS))
        base = DEVICE_INFO_ADDRESS
        memory[base:base + 2] = struct.pack(">H", self.manufacturer_id)
        memory[base + 2:base + 4] = struct.pack(">H", self.application_id)
        memory[base + 4] = self.application_version
        memory[base + 5] = 0
        memory[base + 6:base + 8] = struct.pack(">H", self.mask_version)
        order = self.order_number.encode("ascii", "replace")[:8]
        memory[base + 8:base + 16] = order.ljust(8, b"\x20")
        return memory

    def memory_read(self, address: int, count: int) -> bytes:
        if address < 0:
            return b"\x00" * count
        return bytes(self.memory[address:address + count]).ljust(count, b"\x00")

    def memory_write(self, address: int, data: bytes) -> None:
        if address < 0 or not data:
            return
        end = address + len(data)
        if end > len(self.memory):
            self.memory.extend(b"\x00" * (end - len(self.memory)))
        self.memory[address:end] = data
        if address >= APPLICATION_ADDRESS and self.load_state != LOAD_STATE_LOADING:
            self.load_state = LOAD_STATE_LOADED  # memory-only (BCU1) downloads have no load events

    def send(self, dst: int, group: bool, tpdu: bytes) -> None:
        self.instance.transmit(LData(L_DATA_IND, self.ia, dst, group, tpdu), origin=self)

    def receive(self, f: LData) -> None:
        if len(f.tpdu) < 1:
            return
        if f.group:
            if f.dst == 0:
                self._broadcast(f)
            elif len(f.tpdu) >= 2:
                self._group(f)
        elif f.dst == self.ia:
            self._p2p(f)

    # --- group communication ---------------------------------------------
    def _group(self, f: LData) -> None:
        service, payload = decode_group(f.tpdu)
        answered = False
        for ch in self.channels:
            for key, gas in ch.ga.items():
                if f.dst not in gas:
                    continue
                kind, flags = ch.objects[key]
                if service == GROUP_READ:
                    if "r" in flags and gas[0] == f.dst and not answered:
                        value = ch.read(key)
                        if value is not None:
                            answered = True
                            self.send(f.dst, True, group_apdu(GROUP_RESPONSE, dpt.encode(kind, value)))
                elif (service == GROUP_WRITE and "w" in flags) or (service == GROUP_RESPONSE and "u" in flags):
                    try:
                        value = dpt.decode(kind, payload)
                    except (ValueError, IndexError, struct.error):
                        log.debug("ignoring telegram with wrong size for DPT %s", kind)
                        continue
                    ch.write(key, value)

    # --- broadcast management (programming mode) --------------------------
    def _broadcast(self, f: LData) -> None:
        if len(f.tpdu) < 2:
            return
        a, data = f.apci, f.tpdu[2:]
        if a == IA_READ and self.prog_mode:
            self.send(0, True, apdu(IA_RESPONSE))
        elif a == IA_WRITE and self.prog_mode and len(data) >= 2:
            self._set_ia(struct.unpack(">H", data[:2])[0])
        elif a == IA_SERIAL_READ and data[:6] == self.serial:
            self.send(0, True, apdu(IA_SERIAL_RESPONSE, self.serial + b"\x00\x00"))
        elif a == IA_SERIAL_WRITE and data[:6] == self.serial and len(data) >= 8:
            self._set_ia(struct.unpack(">H", data[6:8])[0])

    def _set_ia(self, ia: int) -> None:
        log.info("%s: individual address %s -> %s", self.name, ia_to_str(self.ia), ia_to_str(ia))
        self.ia = ia
        self.instance.changed()

    # --- point-to-point transport layer -----------------------------------
    def _p2p(self, f: LData) -> None:
        t = f.tpdu[0]
        if t == T_CONNECT:
            if self._peer in (None, f.src):
                self._peer, self._rx, self._tx = f.src, 0, 0
                self._touch()
            else:
                self.send(f.src, False, bytes([T_DISCONNECT]))
            return
        if t == T_DISCONNECT:
            if f.src == self._peer:
                self._close()
            return
        if t & 0xC0 == 0xC0:  # T_ACK / T_NAK: we never need to retransmit
            return
        if t & 0xC0 == 0x40:  # T_Data_Connected
            if f.src != self._peer:
                self.send(f.src, False, bytes([T_DISCONNECT]))
                return
            seq = t >> 2 & 0xF
            self._touch()
            if seq == self._rx:
                self.send(f.src, False, bytes([0xC2 | seq << 2]))
                self._rx = (seq + 1) & 0xF
                if len(f.tpdu) >= 2:
                    self._application(f, connected=True)
            elif seq == (self._rx - 1) & 0xF:  # repeated frame: ack again, don't execute
                self.send(f.src, False, bytes([0xC2 | seq << 2]))
            return
        if len(f.tpdu) >= 2:  # T_Data_Individual (connectionless)
            self._application(f, connected=False)

    def _touch(self) -> None:
        if self._timer:
            self._timer.cancel()
        self._timer = asyncio.get_running_loop().call_later(TRANSPORT_TIMEOUT, self._timeout)

    def _timeout(self) -> None:
        peer = self._peer
        self._close()
        if peer is not None:
            self.send(peer, False, bytes([T_DISCONNECT]))

    def _close(self) -> None:
        self._peer = None
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _reply(self, dst: int, connected: bool, payload: bytes) -> None:
        if connected:
            payload = bytes([0x40 | self._tx << 2 | payload[0]]) + payload[1:]
            self._tx = (self._tx + 1) & 0xF
        self.send(dst, False, payload)

    def _application(self, f: LData, connected: bool) -> None:
        a, data = f.apci, f.tpdu[2:]
        reply = lambda payload: self._reply(f.src, connected, payload)  # noqa: E731
        if a & 0x3C0 == DEVICE_DESCRIPTOR_READ:
            descriptor = a & 0x3F
            if descriptor == 0:  # mask version
                reply(apdu(DEVICE_DESCRIPTOR_RESPONSE, struct.pack(">H", self.mask_version)))
            elif descriptor == 2:  # application descriptor: application id + version
                reply(apdu(DEVICE_DESCRIPTOR_RESPONSE | 2,
                           struct.pack(">H", self.application_id) + bytes([self.application_version])))
            else:
                reply(apdu(DEVICE_DESCRIPTOR_RESPONSE | 0x3F))  # unsupported descriptor type
        elif a & 0x3C0 == RESTART:
            if a & 0x01:  # master reset expects a response
                reply(apdu(0x3A1, b"\x00\x00\x00"))
            self.prog_mode = False
            self._close()
            self.instance.changed()
        elif a & 0x3C0 == MEMORY_READ:
            address = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 0
            count = (a & 0x3F) + 1  # ETS encodes number of bytes minus one
            reply(apdu(MEMORY_RESPONSE | (count - 1), self.memory_read(address, count)))
        elif a & 0x3C0 == MEMORY_WRITE:
            address = struct.unpack(">H", data[:2])[0] if len(data) >= 2 else 0
            count = (a & 0x3F) + 1
            self.memory_write(address, data[2:2 + count])
        elif a == AUTHORIZE_REQUEST:
            reply(apdu(AUTHORIZE_RESPONSE, b"\x00"))  # access level 0 (full)
        elif a == PROPERTY_READ and len(data) >= 4:
            reply(apdu(PROPERTY_RESPONSE, self._property(data)))
        elif a == PROPERTY_WRITE and len(data) >= 4:
            reply(apdu(PROPERTY_RESPONSE, self._property_write(data)))

    def _property_values(self, oi: int) -> dict[int, bytes]:
        if oi == 0:  # device object
            values = {1: b"\x00\x00", 11: self.serial, 12: struct.pack(">H", self.manufacturer_id),
                      54: bytes([int(self.prog_mode)]), 56: b"\x00\x0F",
                      83: struct.pack(">H", self.mask_version)}
        elif 1 <= oi <= 4:  # load state machines: address/association table, application, PEI
            # ponytail: one load state shared by all four; split per object if ETS interleaves them
            values = {1: struct.pack(">H", oi), 5: bytes([self.load_state])}
            if oi == 3:  # PID_PROG_VERSION: manufacturer, application number, version
                values[13] = struct.pack(">HHB", self.manufacturer_id, self.application_id,
                                         self.application_version)
        else:
            values = {}
        values.update({pid: v for (o, pid), v in self.properties.items() if o == oi})
        return values

    def _property(self, req: bytes) -> bytes:
        oi, pid, noe, six = req[0], req[1], req[2] >> 4, (req[2] & 0xF) << 8 | req[3]
        value = self._property_values(oi).get(pid)
        if value is None or noe != 1 or six > 1:
            return bytes([oi, pid, six >> 8 & 0xF, six & 0xFF])  # NoE = 0 -> negative response
        return bytes(req[:4]) + (b"\x00\x01" if six == 0 else value)  # index 0 = element count

    def _property_write(self, req: bytes) -> bytes:
        oi, pid, noe, six = req[0], req[1], req[2] >> 4, (req[2] & 0xF) << 8 | req[3]
        payload = bytes(req[4:])
        if noe != 1 or six != 1 or not payload:
            return bytes([oi, pid, six >> 8 & 0xF, six & 0xFF])
        if pid == 5 and 1 <= oi <= 4:  # load event; 0 = no-op, 3 = segment control (no state change)
            self.load_state = {1: LOAD_STATE_LOADING, 2: LOAD_STATE_LOADED,
                               4: LOAD_STATE_UNLOADED}.get(payload[0], self.load_state)
        elif oi == 0 and pid == 54:
            self.prog_mode = bool(payload[0] & 1)
        else:  # LdCtrlWriteProp and friends: keep the value so ETS can read it back
            self.properties[(oi, pid)] = payload
        self.instance.changed()
        return bytes(req[:4]) + self._property_values(oi)[pid]

    # --- UI --------------------------------------------------------------
    def action(self, name: str, value) -> None:
        if name != "prog":
            raise ValueError(f"device has no action {name!r}")
        self.prog_mode = not self.prog_mode if value is None else bool(value)
        self.instance.changed()

    def stop(self) -> None:
        self._close()
        for ch in self.channels:
            ch.stop()

    def snapshot(self) -> dict:
        return {"index": self.index, "ia": ia_to_str(self.ia), "name": self.name,
                "prog_mode": self.prog_mode, "serial": self.serial.hex(),
                "manufacturer_id": self.manufacturer_id, "application_id": self.application_id,
                "order_number": self.order_number, "mask_version": self.mask_version,
                "load_state": self.load_state,
                "channels": [ch.snapshot() for ch in self.channels]}
