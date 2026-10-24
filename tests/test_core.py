#!/usr/bin/env python3
"""Юнит-тесты чистой логики ultactl вдоль векторов из RE проекта."""
import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
import ultactl as u


class ChecksumTests(unittest.TestCase):
    def test_reply_checksum_live_vector(self):
        # живой ответ донгла на read EEPROM(0) из packet.rs
        read0 = bytes([0x09, 0x08, 0x00, 0x00, 0x00, 0x0A, 0x01, 0x54, 0x06,
                       0x4F, 0x01, 0x54, 0x00, 0x55, 0x00, 0x55, 0x91])
        self.assertEqual((0x4C - sum(read0[1:16])) & 0xFF, read0[16])

    def test_write_checksum_recipe(self):
        # RE-формула из packet.rs: 'U' − Σчётных − Σнечётных по байтам 0..16
        p = u.build_write(0x00A0, [7, 0xFF, 0, 0xFF, 0x96, 0x96, 0x24])
        odd = even = 0
        for i in range(0, 16, 2):
            odd = (odd + p[i]) & 0xFF
            even = (even + p[i + 1]) & 0xFF
        self.assertEqual(p[16], (0x55 - even - odd) & 0xFF)

    def test_record_appends_its_own_checksum(self):
        r = u.record([0x01, 0x02])
        self.assertEqual(r[-1], (0x55 - 3) & 0xFF)
        self.assertEqual(len(r), 3)


class PacketLayoutTests(unittest.TestCase):
    def test_read_layout(self):
        self.assertEqual(list(u.build_read(0x0123, 10)[:6]),
                         [0x08, 0x08, 0x00, 0x01, 0x23, 0x0A])

    def test_write_layout(self):
        p = u.build_write(0x0004, [1, 0x54])
        self.assertEqual(list(p[:8]),
                         [0x08, 0x07, 0x00, 0x00, 0x04, 0x02, 0x01, 0x54])

    def test_battery_opcode(self):
        self.assertEqual(u.build_battery(), u.ckpack([0x08, 0x04]))

    def test_paint_all_packets_17_bytes(self):
        for pkt in u.SESSION:
            self.assertEqual(len(pkt), 17)
            self.assertEqual(pkt[0], 0x08)

    def test_ckpack_pads_and_self_checksums(self):
        pkt = u.ckpack([0x08, 0x03])
        self.assertEqual(len(pkt), 17)
        self.assertEqual(pkt[16], (0x55 - sum(pkt[:16])) & 0xFF)


class ColorTests(unittest.TestCase):
    def test_named(self):
        self.assertEqual(u.parse_color("red"), (255, 0, 0))
        self.assertEqual(u.parse_color("orange"), (255, 127, 0))

    def test_hex_and_hash_and_rgb(self):
        self.assertEqual(u.parse_color("ff8800"), (0xFF, 0x88, 0x00))
        self.assertEqual(u.parse_color("#FF00FF"), (255, 0, 255))
        self.assertEqual(u.parse_color("255,128,0"), (255, 128, 0))

    def test_invalid_raises(self):
        for bad in ("zzz", "12345", "1000,1000,1000"):
            with self.assertRaises(ValueError):
                u.parse_color(bad)


class DpiTests(unittest.TestCase):
    def test_encode_decode_roundtrip_low(self):
        code, mul = u.dpi_encode(1600)
        self.assertEqual((code, mul), (31, 0))
        self.assertEqual(u.dpi_decode(code, mul), 1600)

    def test_encode_decode_roundtrip_high(self):
        code, mul = u.dpi_encode(12800)
        self.assertEqual((code, mul), (127, 2))
        self.assertEqual(u.dpi_decode(code, mul), 12800)

    def test_low_region_step50(self):
        self.assertEqual(u.dpi_snap(1300), 1300)
        self.assertEqual(u.dpi_snap(1302), 1300)
        self.assertEqual(u.dpi_snap(400), 400)

    def test_high_region_step100(self):
        self.assertEqual(u.dpi_snap(12010), 12000)
        self.assertEqual(u.dpi_snap(12050), 12100)
        self.assertEqual(u.dpi_snap(12049), 12000)

    def test_caps(self):
        self.assertEqual(u.dpi_encode(20000), u.dpi_encode(19000))
        self.assertEqual(u.dpi_encode(10), u.dpi_encode(50))

    def test_slot_record_shape(self):
        code, mul = u.dpi_encode(1600)
        r = u.record([code, code, (mul & 0x0F) | (mul << 4)])
        self.assertEqual(len(r), 4)
        self.assertEqual(r[3], (0x55 - r[0] - r[1] - r[2]) & 0xFF)


class LevelTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(u.level_of_pct(0), 1)
        self.assertEqual(u.level_of_pct(1), 1)
        self.assertEqual(u.level_of_pct(100), 10)
        self.assertEqual(u.level_of_pct(35), 4)
        self.assertEqual(u.level_of_pct(50), 5)

    def test_speed_tables_known_values(self):
        self.assertEqual(u.tables_for("steady")[0][0], 0xFF)
        self.assertEqual(u.tables_for("stream")[0][0], 0x0A)


class DescriptorTests(unittest.TestCase):
    def test_feature_report_detection(self):
        # фрагмент report descriptor конфиг-интерфейса: Usage Page FF02,
        # Report ID 0x08, затем Feature item 0xB1
        desc = bytes([0x06, 0x02, 0xFF, 0x09, 0x02, 0xA1, 0x01, 0x85, 0x08,
                      0x09, 0x02, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08,
                      0x95, 0x10, 0xB1, 0x02, 0xC0])
        self.assertTrue(u.has_feature_report(desc, 0x08))
        self.assertFalse(u.has_feature_report(desc, 0x09))


class MotionLedTests(unittest.TestCase):
    def test_on_writes_zero(self):
        m = u.Mouse.__new__(u.Mouse)

        class Fake:
            pass
        saved = {}

        def wb(addr, data, label=""):
            saved["addr"], saved["data"] = addr, data
        m.write_block = wb
        u.motion_led_set(m, True)
        self.assertEqual(saved["addr"], u.A_LED_OFF_MOVING)
        self.assertEqual(saved["data"], [0, 0x55])

    def test_off_writes_one(self):
        m = u.Mouse.__new__(u.Mouse)
        saved = {}

        def wb(addr, data, label=""):
            saved["data"] = data
        m.write_block = wb
        u.motion_led_set(m, False)
        self.assertEqual(saved["data"], [1, 0x54])


if __name__ == "__main__":
    unittest.main()
