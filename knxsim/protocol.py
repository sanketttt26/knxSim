"""KNXnet/IP and cEMI framing (KNX Standard vol. 3/8/2, 3/8/4, 3/6/3)."""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass, replace

KNX_PORT = 3671
MULTICAST_GROUP = "224.0.23.12"

# KNXnet/IP service types
SEARCH_REQUEST = 0x0201
SEARCH_RESPONSE = 0x0202
DESCRIPTION_REQUEST = 0x0203
DESCRIPTION_RESPONSE = 0x0204
CONNECT_REQUEST = 0x0205
CONNECT_RESPONSE = 0x0206
CONNECTIONSTATE_REQUEST = 0x0207
CONNECTIONSTATE_RESPONSE = 0x0208
DISCONNECT_REQUEST = 0x0209
DISCONNECT_RESPONSE = 0x020A
DEVICE_CONFIGURATION_REQUEST = 0x0310
DEVICE_CONFIGURATION_ACK = 0x0311
TUNNELLING_REQUEST = 0x0420
TUNNELLING_ACK = 0x0421

# KNXnet/IP status codes
E_NO_ERROR = 0x00
E_HOST_PROTOCOL_TYPE = 0x01
E_CONNECTION_ID = 0x21
E_CONNECTION_TYPE = 0x22
E_NO_MORE_CONNECTIONS = 0x24
E_TUNNELLING_LAYER = 0x29

DEVICE_MGMT_CONNECTION = 0x03
TUNNEL_CONNECTION = 0x04
TUNNEL_LINKLAYER = 0x02

# cEMI message codes
L_DATA_REQ = 0x11
L_DATA_CON = 0x2E
L_DATA_IND = 0x29
M_PROPREAD_REQ = 0xFC
M_PROPREAD_CON = 0xFB
M_PROPWRITE_REQ = 0xF6
M_PROPWRITE_CON = 0xF5

# Transport layer control bytes (TPCI)
T_CONNECT = 0x80
T_DISCONNECT = 0x81

# Application layer services (10-bit APCI)
GROUP_READ = 0x000
GROUP_RESPONSE = 0x040
GROUP_WRITE = 0x080
IA_WRITE = 0x0C0
IA_READ = 0x100
IA_RESPONSE = 0x140
MEMORY_READ = 0x200
MEMORY_RESPONSE = 0x240
MEMORY_WRITE = 0x280
DEVICE_DESCRIPTOR_READ = 0x300
DEVICE_DESCRIPTOR_RESPONSE = 0x340
RESTART = 0x380
AUTHORIZE_REQUEST = 0x3D1
AUTHORIZE_RESPONSE = 0x3D2
PROPERTY_READ = 0x3D5
PROPERTY_RESPONSE = 0x3D6
PROPERTY_WRITE = 0x3D7
IA_SERIAL_READ = 0x3DC
IA_SERIAL_RESPONSE = 0x3DD
IA_SERIAL_WRITE = 0x3DE


def frame(service: int, body: bytes) -> bytes:
    return struct.pack(">BBHH", 6, 0x10, service, 6 + len(body)) + body


def parse_frame(data: bytes) -> tuple[int, bytes] | None:
    if len(data) < 6 or data[0] != 6 or data[1] != 0x10:
        return None
    service, total = struct.unpack_from(">HH", data, 2)
    if total > len(data):
        return None
    return service, data[6:total]


def hpai(ip: str, port: int) -> bytes:
    return struct.pack(">BB4sH", 8, 1, socket.inet_aton(ip), port)


def parse_hpai(data: bytes, offset: int = 0) -> tuple[int, str, int]:
    """Return (host protocol, ip, port) of the HPAI at offset."""
    proto = data[offset + 1]
    ip = socket.inet_ntoa(data[offset + 2:offset + 6])
    (port,) = struct.unpack_from(">H", data, offset + 6)
    return proto, ip, port


def endpoint(hp: tuple[int, str, int], source: tuple[str, int]) -> tuple[str, int]:
    """Resolve an HPAI against the datagram source address (NAT / route-back mode)."""
    _, ip, port = hp
    return source if ip == "0.0.0.0" or port == 0 else (ip, port)


# --- addresses ---------------------------------------------------------------

def ia_from_str(text: str) -> int:
    area, line, dev = (int(x) for x in text.split("."))
    if not (0 <= area <= 15 and 0 <= line <= 15 and 0 <= dev <= 255):
        raise ValueError(f"invalid individual address {text!r}")
    return area << 12 | line << 8 | dev


def ia_to_str(value: int) -> str:
    return f"{value >> 12}.{value >> 8 & 0xF}.{value & 0xFF}"


def ga_from_str(text: str) -> int:
    parts = [int(x) for x in str(text).split("/")]
    if len(parts) == 3 and parts[0] <= 31 and parts[1] <= 7 and parts[2] <= 255:
        return parts[0] << 11 | parts[1] << 8 | parts[2]
    if len(parts) == 2 and parts[0] <= 31 and parts[1] <= 2047:
        return parts[0] << 11 | parts[1]
    if len(parts) == 1 and 0 <= parts[0] <= 0xFFFF:
        return parts[0]
    raise ValueError(f"invalid group address {text!r}")


def ga_to_str(value: int) -> str:
    return f"{value >> 11}/{value >> 8 & 0x7}/{value & 0xFF}"


# --- cEMI --------------------------------------------------------------------

@dataclass(frozen=True)
class LData:
    """A cEMI L_Data frame. tpdu = TPCI/APCI byte(s) + data."""
    code: int
    src: int
    dst: int
    group: bool
    tpdu: bytes
    ctrl1: int = 0xBC  # standard frame, no repeat, broadcast, low priority
    hops: int = 6

    @classmethod
    def decode(cls, data: bytes) -> LData:
        code = data[0]
        p = 2 + data[1]  # skip additional info
        ctrl1, ctrl2 = data[p], data[p + 1]
        src, dst = struct.unpack_from(">HH", data, p + 2)
        n = data[p + 6]
        tpdu = bytes(data[p + 7:p + 8 + n])
        if len(tpdu) != n + 1:
            raise ValueError("truncated cEMI frame")
        return cls(code, src, dst, bool(ctrl2 & 0x80), tpdu, ctrl1, ctrl2 >> 4 & 7)

    def encode(self) -> bytes:
        ctrl2 = (0x80 if self.group else 0) | self.hops << 4
        return (bytes([self.code, 0, self.ctrl1, ctrl2]) + struct.pack(">HH", self.src, self.dst)
                + bytes([len(self.tpdu) - 1]) + self.tpdu)

    def confirm(self, ok: bool) -> LData:
        return replace(self, code=L_DATA_CON, ctrl1=(self.ctrl1 & ~1) | (0 if ok else 1))

    @property
    def apci(self) -> int:
        return (self.tpdu[0] & 0x03) << 8 | (self.tpdu[1] if len(self.tpdu) > 1 else 0)


def apdu(service: int, data: bytes = b"") -> bytes:
    """Build an APDU (without transport bits) for a 10-bit service code."""
    return bytes([service >> 8 & 0x03, service & 0xFF]) + data


def group_apdu(service: int, payload: int | bytes) -> bytes:
    """Group value APDU. An int payload is a small (<= 6 bit) value packed into the APCI."""
    if isinstance(payload, int):
        return bytes([0x00, service | (payload & 0x3F)])
    return bytes([0x00, service]) + payload


def decode_group(tpdu: bytes) -> tuple[int, int | bytes]:
    service = ((tpdu[0] & 0x03) << 8 | tpdu[1]) & 0x3C0
    return service, (tpdu[1] & 0x3F if len(tpdu) == 2 else tpdu[2:])


def describe(f: LData) -> str:
    """Human readable service name, used by the monitor."""
    t = f.tpdu[0]
    if t == T_CONNECT:
        return "T_Connect"
    if t == T_DISCONNECT:
        return "T_Disconnect"
    if t & 0xC3 == 0xC2:
        return "T_Ack"
    if t & 0xC3 == 0xC3:
        return "T_Nak"
    a = f.apci
    if f.group and f.dst != 0 and a & 0x3C0 in (GROUP_READ, GROUP_RESPONSE, GROUP_WRITE):
        return {GROUP_READ: "GroupValueRead", GROUP_RESPONSE: "GroupValueResponse",
                GROUP_WRITE: "GroupValueWrite"}[a & 0x3C0]
    four_bit = {IA_WRITE: "IndividualAddressWrite", IA_READ: "IndividualAddressRead",
                IA_RESPONSE: "IndividualAddressResponse", MEMORY_READ: "MemoryRead",
                MEMORY_RESPONSE: "MemoryResponse", MEMORY_WRITE: "MemoryWrite",
                DEVICE_DESCRIPTOR_READ: "DeviceDescriptorRead",
                DEVICE_DESCRIPTOR_RESPONSE: "DeviceDescriptorResponse", RESTART: "Restart"}
    ten_bit = {AUTHORIZE_REQUEST: "AuthorizeRequest", AUTHORIZE_RESPONSE: "AuthorizeResponse",
               PROPERTY_READ: "PropertyValueRead", PROPERTY_RESPONSE: "PropertyValueResponse",
               PROPERTY_WRITE: "PropertyValueWrite", IA_SERIAL_READ: "IndividualAddressSerialNumberRead",
               IA_SERIAL_RESPONSE: "IndividualAddressSerialNumberResponse",
               IA_SERIAL_WRITE: "IndividualAddressSerialNumberWrite"}
    if a & 0x3C0 == 0x3C0:
        return ten_bit.get(a, f"APCI 0x{a:03X}")
    return four_bit.get(a & 0x3C0, f"APCI 0x{a:03X}")
