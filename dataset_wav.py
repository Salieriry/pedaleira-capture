"""Converte uma captura CSV da Placa B em WAV float32 mono.

O CSV contém amostras assinadas de 32 bits no campo ``amostra_32bit``. O WAV
gerado usa o formato IEEE float de 32 bits, normalizado por 2**31. A
conversão é feita em fluxo e usa apenas a biblioteca padrão.

Exemplos:

    python dataset_wav.py captura.csv captura.wav
    python dataset_wav.py captura.csv       # cria captura.wav ao lado do CSV
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import struct
from pathlib import Path


SAMPLE_FIELD = "amostra_32bit"
DEFAULT_RATE = 44100
FLOAT32_MAX = 1.0


def read_sample_rate(csv_path: Path, explicit_rate: int | None) -> int:
    if explicit_rate is not None:
        return explicit_rate
    metadata_path = csv_path.with_suffix(".json")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        rate = int(metadata.get("sample_rate", DEFAULT_RATE))
        if rate > 0:
            return rate
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return DEFAULT_RATE


def _write_header(handle, sample_rate: int, data_size: int = 0) -> None:
    # WAVE_FORMAT_IEEE_FLOAT (3), mono, 32-bit little-endian.
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        3,
        1,
        sample_rate,
        sample_rate * 4,
        4,
        32,
        b"data",
        data_size,
    )
    handle.write(header)


def convert(input_csv: str | Path, output_wav: str | Path | None = None, sample_rate: int | None = None) -> Path:
    """Converte ``input_csv`` e retorna o caminho WAV criado."""
    source = Path(input_csv)
    output = source.with_suffix(".wav") if output_wav is None else Path(output_wav)
    if not source.is_file():
        raise FileNotFoundError(source)
    rate = read_sample_rate(source, sample_rate)
    if rate <= 0:
        raise ValueError("A taxa de amostragem deve ser positiva.")

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    data_size = 0
    try:
        with source.open("r", newline="", encoding="utf-8") as csv_file, partial.open("wb+") as wav_file:
            reader = csv.DictReader(csv_file)
            if reader.fieldnames is None or SAMPLE_FIELD not in reader.fieldnames:
                raise ValueError(f"CSV sem a coluna obrigatória {SAMPLE_FIELD!r}: {source}")
            _write_header(wav_file, rate)
            packed = bytearray()
            for line_number, row in enumerate(reader, start=2):
                try:
                    raw = int(row[SAMPLE_FIELD])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Amostra inválida na linha {line_number} de {source}") from exc
                value = max(-1.0, min(FLOAT32_MAX, raw / 2147483648.0))
                packed.extend(struct.pack("<f", value))
                if len(packed) >= 64 * 1024:
                    wav_file.write(packed)
                    data_size += len(packed)
                    packed.clear()
            if packed:
                wav_file.write(packed)
                data_size += len(packed)
            wav_file.seek(0)
            _write_header(wav_file, rate, data_size)
        os.replace(partial, output)
    except Exception:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
        raise
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path)
    parser.add_argument("output_wav", nargs="?", type=Path)
    parser.add_argument("--sample-rate", type=int, default=None, help="sobrescreve a taxa do JSON")
    args = parser.parse_args()
    print(convert(args.input_csv, args.output_wav, args.sample_rate))
