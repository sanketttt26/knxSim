"""ETS-style device management: application download, memory and properties."""
import struct
import unittest

from knxsim.devices import LOAD_STATE_LOADED, LOAD_STATE_LOADING, LOAD_STATE_UNLOADED, Device
from knxsim.protocol import (
    DEVICE_DESCRIPTOR_READ, GROUP_READ, GROUP_RESPONSE, GROUP_WRITE, L_DATA_IND, MEMORY_READ,
    MEMORY_RESPONSE, MEMORY_WRITE, PROPERTY_READ, PROPERTY_RESPONSE, PROPERTY_WRITE, T_CONNECT,
    LData, ga_from_str, group_apdu, ia_from_str,
)


class FakeInstance:
    def __init__(self):
        self.frames = []

    def transmit(self, frame, origin=None):
        self.frames.append(frame)

    def changed(self):
        pass


def apci_of(tpdu: bytes) -> int:
    return (tpdu[0] & 0x03) << 8 | tpdu[1]


class DownloadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.inst = FakeInstance()
        self.dev = Device(self.inst, {
            "ia": "1.1.10", "manufacturer_id": 0x0083, "application_id": 0x00A0,
            "application_version": 0x20, "mask_version": 0x0705,
            "order_number": "5WG1 123-4AB1", "load_state": LOAD_STATE_UNLOADED,
            "channels": [{"type": "object", "number": 1, "dpt": "1.001",
                          "flags": "rwt", "ga": {"value": ["1/0/1"]}}],
        }, 0, b"\x00\x00\x00\x01\x00\x01")
        self.src = ia_from_str("1.1.250")
        self.seq = 0
        self.dev.receive(LData(L_DATA_IND, self.src, self.dev.ia, False, bytes([T_CONNECT])))
        self.inst.frames.clear()

    async def asyncTearDown(self):
        self.dev.stop()

    def send(self, apci: int, data: bytes = b"") -> list[LData]:
        tpdu = bytes([0x40 | self.seq << 2 | (apci >> 8), apci & 0xFF]) + data
        self.dev.receive(LData(L_DATA_IND, self.src, self.dev.ia, False, tpdu))
        self.assertTrue(self.inst.frames, "device must at least acknowledge the telegram")
        self.assertEqual(self.inst.frames[0].tpdu[0] & 0xC0, 0xC0)  # T_ACK
        self.seq = (self.seq + 1) & 0xF
        replies, self.inst.frames = self.inst.frames[1:], []
        return replies

    async def test_device_descriptor_uses_project_mask(self):
        replies = self.send(DEVICE_DESCRIPTOR_READ)
        self.assertEqual(apci_of(replies[0].tpdu), 0x340)
        self.assertEqual(replies[0].tpdu[2:], struct.pack(">H", 0x0705))

    async def test_memory_read_manufacturer_block(self):
        replies = self.send(MEMORY_READ | 3, struct.pack(">H", 0x0100))  # 4 bytes
        response = replies[0]
        apci = apci_of(response.tpdu)
        self.assertEqual(apci & 0x3C0, MEMORY_RESPONSE)
        self.assertEqual((apci & 0x3F) + 1, 4)
        self.assertEqual(response.tpdu[2:], struct.pack(">HH", 0x0083, 0x00A0))

    async def test_memory_write_then_read(self):
        replies = self.send(MEMORY_WRITE | 1, struct.pack(">H", 0x0200) + b"\xde\xad")
        self.assertEqual(replies, [])
        self.assertEqual(self.dev.load_state, LOAD_STATE_LOADED)

        replies = self.send(MEMORY_READ | 1, struct.pack(">H", 0x0200))
        self.assertEqual(replies[0].tpdu[2:], b"\xde\xad")

    async def test_property_read_identity(self):
        replies = self.send(PROPERTY_READ, bytes([0, 12, 0x10, 0x01]))  # NoE 1, start index 1
        self.assertEqual(apci_of(replies[0].tpdu), PROPERTY_RESPONSE)
        self.assertEqual(replies[0].tpdu[2:], bytes([0, 12, 0x10, 0x01]) + struct.pack(">H", 0x0083))

        replies = self.send(PROPERTY_READ, bytes([3, 13, 0x10, 0x01]))  # PID_PROG_VERSION
        self.assertEqual(replies[0].tpdu[6:], struct.pack(">HHB", 0x0083, 0x00A0, 0x20))

        replies = self.send(PROPERTY_READ, bytes([0, 12, 0x10, 0x00]))  # index 0 = element count
        self.assertEqual(replies[0].tpdu[6:], b"\x00\x01")

        replies = self.send(PROPERTY_READ, bytes([0, 99, 0x10, 0x01]))  # unknown -> NoE 0
        self.assertEqual(replies[0].tpdu[2:], bytes([0, 99, 0x00, 0x01]))

    async def test_load_state_machine(self):
        def event(oi, code):
            replies = self.send(PROPERTY_WRITE, bytes([oi, 5, 0x10, 0x01, code]) + bytes(9))
            return replies[0].tpdu[6:]

        self.assertEqual(event(1, 4), bytes([LOAD_STATE_UNLOADED]))   # unload
        self.assertEqual(event(1, 1), bytes([LOAD_STATE_LOADING]))    # start loading
        self.assertEqual(event(1, 3), bytes([LOAD_STATE_LOADING]))    # segment control: no change
        self.assertEqual(event(1, 2), bytes([LOAD_STATE_LOADED]))     # load completed
        replies = self.send(PROPERTY_READ, bytes([3, 5, 0x10, 0x01]))
        self.assertEqual(replies[0].tpdu[6:], bytes([LOAD_STATE_LOADED]))

    async def test_written_property_reads_back(self):
        self.send(PROPERTY_WRITE, bytes([3, 27, 0x10, 0x01]) + b"\x12\x34")
        replies = self.send(PROPERTY_READ, bytes([3, 27, 0x10, 0x01]))
        self.assertEqual(replies[0].tpdu[6:], b"\x12\x34")

    async def test_imported_object_responds_to_group_communication(self):
        self.dev.receive(LData(L_DATA_IND, self.src, ga_from_str("1/0/1"), True,
                               group_apdu(GROUP_WRITE, 1)))
        self.assertTrue(self.dev.channels[0].value)

        self.inst.frames.clear()
        self.dev.receive(LData(L_DATA_IND, self.src, ga_from_str("1/0/1"), True,
                               group_apdu(GROUP_READ, 0)))
        self.assertTrue(self.inst.frames)
        self.assertEqual(self.inst.frames[-1].tpdu, group_apdu(GROUP_RESPONSE, 1))

    async def test_raw_dpt_object(self):
        dev = Device(self.inst, {"ia": "1.1.11", "channels": [
            {"type": "object", "dpt": "10.001", "flags": "rw", "ga": {"value": ["1/0/2"]}}]}, 1, bytes(6))
        self.assertIsNone(dev.channels[0].value)
        dev.receive(LData(L_DATA_IND, self.src, ga_from_str("1/0/2"), True,
                          group_apdu(GROUP_WRITE, b"\x0c\x1e\x00")))
        self.assertEqual(dev.channels[0].value, "0c1e00")
        self.inst.frames.clear()
        dev.receive(LData(L_DATA_IND, self.src, ga_from_str("1/0/2"), True, group_apdu(GROUP_READ, 0)))
        self.assertEqual(self.inst.frames[-1].tpdu, group_apdu(GROUP_RESPONSE, b"\x0c\x1e\x00"))


if __name__ == "__main__":
    unittest.main()
