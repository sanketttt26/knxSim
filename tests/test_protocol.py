import unittest

from knxsim import dpt
from knxsim.protocol import (
    GROUP_WRITE, L_DATA_IND, LData, decode_group, frame, ga_from_str, ga_to_str, group_apdu,
    ia_from_str, ia_to_str, parse_frame,
)


class ProtocolTest(unittest.TestCase):
    def test_addresses(self):
        self.assertEqual(ia_from_str("1.1.250"), 0x11FA)
        self.assertEqual(ia_to_str(0x11FA), "1.1.250")
        self.assertEqual(ga_from_str("1/2/3"), 0x0A03)
        self.assertEqual(ga_to_str(0x0A03), "1/2/3")
        self.assertEqual(ga_from_str("1/515"), 0x0A03)
        with self.assertRaises(ValueError):
            ia_from_str("16.0.1")

    def test_frame_roundtrip(self):
        raw = frame(0x0420, b"\x04\x01\x00\x00")
        self.assertEqual(raw[:6], bytes.fromhex("06100420000a"))
        self.assertEqual(parse_frame(raw), (0x0420, b"\x04\x01\x00\x00"))
        self.assertIsNone(parse_frame(b"\x06\x10\x04\x20\x00\xff"))

    def test_ldata_roundtrip(self):
        f = LData(L_DATA_IND, 0x1101, ga_from_str("1/0/1"), True, group_apdu(GROUP_WRITE, 1))
        raw = f.encode()
        self.assertEqual(raw.hex(), "2900bce011010801010081")
        self.assertEqual(LData.decode(raw), f)
        self.assertEqual(decode_group(f.tpdu), (GROUP_WRITE, 1))

    def test_dpt(self):
        for v in (0, 21.5, -30.0, 670760.96, 0.01, -671088.64):
            self.assertAlmostEqual(dpt.decode("9.001", dpt.encode("9.001", v)), v, delta=abs(v) * 0.001 + 0.01)
        self.assertEqual(dpt.encode("9.001", 21.5), bytes.fromhex("0c33"))
        self.assertEqual(dpt.encode("5.001", 100), b"\xff")
        self.assertEqual(dpt.decode("5.001", b"\x80"), 50.2)
        self.assertEqual(dpt.decode("3.007", dpt.encode("3.007", -1)), -1)
        self.assertEqual(dpt.decode("3.007", dpt.encode("3.007", 0)), 0)
        self.assertEqual(dpt.decode("1.001", 1), True)
        self.assertEqual(dpt.decode("14.000", dpt.encode("14.000", 1.5)), 1.5)


if __name__ == "__main__":
    unittest.main()
