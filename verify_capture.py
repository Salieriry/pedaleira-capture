"""Verifica o arquivo de auditoria AUD1 produzido pelo recorder_demo.py.

O verificador confere CRC e sequência de cada quadro, valida o marcador AUDE,
compara todas as amostras do arquivo bruto com o CSV e recalcula os hashes do
manifesto JSON. Ele não autentica a identidade elétrica da placa; para isso é
necessário secure boot/assinatura ou um elemento seguro no hardware.
"""
from __future__ import annotations

import argparse
import binascii
import csv
import hashlib
import json
import struct
from pathlib import Path

import numpy as np


MAGIC = b"AUD1"
END_MAGIC = b"AUDE"
HEADER = struct.Struct("<4sHI")
END_HEADER = struct.Struct("<4sI")
SAMPLE_FIELD = "amostra_32bit"


def crc16_ccitt(data: bytes) -> int:
    # CRC-16/CCITT-FALSE; the C implementation avoids Python's per-bit loop
    # while preserving the same polynomial and initial value.
    return binascii.crc_hqx(data, 0xFFFF)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(csv_path: Path, archive_path: Path | None = None, metadata_path: Path | None = None) -> dict:
    metadata_path = metadata_path or csv_path.with_suffix(".json")
    archive_path = archive_path or csv_path.with_suffix(".aud1")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    raw_bytes = archive_path.read_bytes()
    if int(metadata.get("raw_archive_bytes", -1)) != len(raw_bytes):
        raise ValueError("tamanho do arquivo AUD1 não confere com o manifesto")
    if hashlib.sha256(raw_bytes).hexdigest() != metadata.get("raw_archive_sha256"):
        raise ValueError("SHA-256 do arquivo AUD1 não confere com o manifesto")

    csv_hash = sha256_file(csv_path)
    if csv_hash != metadata.get("csv_sha256"):
        raise ValueError("SHA-256 do CSV não confere com o manifesto")

    csv_file = csv_path.open("r", newline="", encoding="utf-8")
    csv_reader = csv.DictReader(csv_file)
    if csv_reader.fieldnames is None or SAMPLE_FIELD not in csv_reader.fieldnames:
        raise ValueError(f"CSV sem a coluna {SAMPLE_FIELD!r}")
    # Recorder CSVs currently have one integer column. NumPy's text reader and
    # vector comparison are much faster than converting tens of millions of
    # DictReader rows one at a time. Keep the generic CSV fallback for older
    # or externally produced captures with additional fields.
    csv_values = None
    if csv_reader.fieldnames == [SAMPLE_FIELD]:
        csv_file.close()
        with csv_path.open("r", newline="", encoding="utf-8") as values_file:
            values_file.readline()
            csv_values = np.fromfile(values_file, dtype="<i8", sep="\n")
        csv_iter = None
    else:
        csv_iter = iter(csv_reader)
    csv_samples = 0
    packet_count = 0
    raw_samples = 0
    trailing_sequence_gaps = 0
    first_sequence = None
    previous_sequence = None
    last_sequence = None
    end_sequence = None
    offset = 0
    try:
        while offset < len(raw_bytes):
            if raw_bytes.startswith(MAGIC, offset):
                if len(raw_bytes) - offset < HEADER.size:
                    raise ValueError("AUD1 truncado no cabeçalho")
                _, count, sequence = HEADER.unpack_from(raw_bytes, offset)
                if not 0 < count <= 128:
                    raise ValueError(f"contagem AUD1 inválida no pacote {packet_count}")
                packet_size = HEADER.size + count * 4 + 2
                if len(raw_bytes) - offset < packet_size:
                    raise ValueError("AUD1 truncado no payload")
                frame = raw_bytes[offset:offset + packet_size]
                crc_offset = HEADER.size + count * 4
                received = struct.unpack_from("<H", frame, crc_offset)[0]
                if received != crc16_ccitt(frame[4:crc_offset]):
                    raise ValueError(f"CRC inválido no pacote {packet_count}")
                if previous_sequence is not None and sequence != previous_sequence + 1:
                    if raw_samples < int(metadata.get("samples", 0)):
                        raise ValueError(f"lacuna ou repetição entre sequências {previous_sequence} e {sequence}")
                    trailing_sequence_gaps += (sequence - previous_sequence - 1) & 0xFFFFFFFF
                if first_sequence is None:
                    first_sequence = sequence
                previous_sequence = sequence
                last_sequence = sequence
                raw_samples += count
                values = np.frombuffer(frame, dtype="<i4", count=count, offset=HEADER.size)
                target = int(metadata.get("samples", 0))
                comparable = min(count, max(0, target - csv_samples))
                if comparable:
                    if csv_values is not None:
                        csv_chunk = csv_values[csv_samples:csv_samples + comparable]
                        if len(csv_chunk) != comparable or not np.array_equal(values[:comparable], csv_chunk):
                            raise ValueError(f"CSV diverge do AUD1 na amostra {csv_samples}")
                    else:
                        for value in values[:comparable]:
                            row = next(csv_iter, None)
                            if row is None or int(row[SAMPLE_FIELD]) != int(value):
                                raise ValueError(f"CSV diverge do AUD1 na amostra {csv_samples}")
                    csv_samples += comparable
                packet_count += 1
                offset += packet_size
                continue
            if raw_bytes.startswith(END_MAGIC, offset):
                end_size = END_HEADER.size + 2
                if len(raw_bytes) - offset < end_size:
                    raise ValueError("AUDE truncado")
                frame = raw_bytes[offset:offset + end_size]
                received = struct.unpack_from("<H", frame, END_HEADER.size)[0]
                if received != crc16_ccitt(frame[4:END_HEADER.size]):
                    raise ValueError("CRC inválido no AUDE")
                end_sequence = struct.unpack_from("<I", frame, 4)[0]
                if last_sequence is not None and end_sequence != last_sequence + 1:
                    trailing_sequence_gaps += (end_sequence - last_sequence - 1) & 0xFFFFFFFF
                offset += end_size
                if offset != len(raw_bytes):
                    raise ValueError("bytes depois do AUDE")
                break
            raise ValueError(f"assinatura desconhecida no byte {offset}")
        if csv_values is not None:
            if len(csv_values) != csv_samples:
                raise ValueError(f"CSV possui {len(csv_values)} amostras; AUD1 contém {csv_samples}")
        elif next(csv_iter, None) is not None:
            raise ValueError("CSV possui amostras além do arquivo AUD1")
    finally:
        csv_file.close()

    if end_sequence is None:
        raise ValueError("marcador AUDE ausente")
    expected = int(metadata.get("samples", -1))
    if csv_samples != expected:
        raise ValueError(f"quantidade divergente: CSV/AUD1={csv_samples}, manifesto={expected}")
    checks = {
        "csv": str(csv_path),
        "archive": str(archive_path),
        "samples": csv_samples,
        "raw_samples": raw_samples,
        "trailing_samples": raw_samples - csv_samples,
        "packets": packet_count,
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "end_sequence": end_sequence,
        "trailing_sequence_gaps": trailing_sequence_gaps,
        "status": "OK" if trailing_sequence_gaps == 0 else "OK_CSV_WITH_TRAILING_DRAIN",
    }
    return checks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("archive", nargs="?", type=Path)
    args = parser.parse_args()
    result = verify(args.csv, args.archive)
    print(json.dumps(result, ensure_ascii=False, indent=2))
