"""Testes do verificador AUD1, incluindo drenagem após o limite do CSV."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import struct
import tempfile
import unittest
from pathlib import Path

VERIFY_PATH = Path(__file__).with_name("verify_capture.py")
VERIFY_SPEC = importlib.util.spec_from_file_location("capture_verify_tests", VERIFY_PATH)
verify_module = importlib.util.module_from_spec(VERIFY_SPEC)
assert VERIFY_SPEC is not None and VERIFY_SPEC.loader is not None
VERIFY_SPEC.loader.exec_module(verify_module)


def make_capture(directory: Path, include_end: bool = True, corrupt_crc: bool = False,
                 mismatch: bool = False, internal_gap: bool = False):
    csv_path = directory / "capture.csv"
    aud1_path = directory / "capture.aud1"
    json_path = directory / "capture.json"
    csv_samples = [1, -2]
    if mismatch:
        csv_samples[1] = -3
    csv_path.write_text("amostra_32bit\n" + "".join(f"{sample}\n" for sample in csv_samples))

    def packet(sequence: int, values: list[int], corrupt: bool = False) -> bytes:
        header = verify_module.HEADER.pack(verify_module.MAGIC, len(values), sequence)
        payload = struct.pack("<" + "i" * len(values), *values)
        frame = header + payload
        crc = verify_module.crc16_ccitt(frame[4:])
        if corrupt:
            crc ^= 1
        return frame + struct.pack("<H", crc)

    first_sequence = 0
    next_sequence = 2 if internal_gap else 2
    first_values = [1] if internal_gap else [1, -2]
    last_values = [-2, 99] if internal_gap else [99]
    body = packet(first_sequence, first_values) + packet(next_sequence, last_values, corrupt_crc)
    trailing_gaps = next_sequence - first_sequence - 1
    end_sequence = next_sequence + 1
    if include_end:
        end_frame = verify_module.END_HEADER.pack(verify_module.END_MAGIC, end_sequence)
        body += end_frame + struct.pack("<H", verify_module.crc16_ccitt(end_frame[4:]))
    aud1_path.write_bytes(body)
    metadata = {
        "samples": 2,
        "raw_archive_bytes": len(body),
        "raw_archive_sha256": hashlib.sha256(body).hexdigest(),
        "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "first_sequence": 0,
        "last_sequence": next_sequence,
        "end_sequence": end_sequence,
        "raw_packet_count": 2,
        "drain_lost_blocks": trailing_gaps,
    }
    json_path.write_text(json.dumps(metadata))
    return csv_path, aud1_path, json_path


class CaptureVerifyTests(unittest.TestCase):
    def run_case(self, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        paths = make_capture(Path(temporary.name), **kwargs)
        return verify_module.verify(*paths)

    def test_trailing_drain_is_reported_but_csv_samples_are_valid(self):
        result = self.run_case()
        self.assertEqual(result["status"], "OK_CSV_WITH_TRAILING_DRAIN")
        self.assertEqual(result["trailing_samples"], 1)
        self.assertEqual(result["trailing_sequence_gaps"], 1)

    def test_au_de_marker_is_required(self):
        with self.assertRaisesRegex(ValueError, "AUDE ausente"):
            self.run_case(include_end=False)

    def test_packet_crc_is_checked(self):
        with self.assertRaisesRegex(ValueError, "CRC inválido"):
            self.run_case(corrupt_crc=True)

    def test_csv_must_match_aud1_audio(self):
        with self.assertRaisesRegex(ValueError, "CSV diverge do AUD1"):
            self.run_case(mismatch=True)

    def test_sequence_gap_inside_csv_interval_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "lacuna ou repetição"):
            self.run_case(internal_gap=True)


if __name__ == "__main__":
    unittest.main()
