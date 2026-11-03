#!/usr/bin/env python3
"""Grava a captura UAC processada da Placa A para inspeção ou DAW.

Este arquivo não gera a fonte oficial do dataset do Smart Noise Gate: para isso
use ``recorder demo/recorder_demo.py``, que captura o ADC cru da Placa B pelo
protocolo AUD1/AUDE.

Dependências: ``pip install sounddevice numpy``. Use ``--list`` para descobrir
o nome/índice da entrada UAC no Linux.
"""

import argparse
import csv
import queue
import time


def record(device, output: str, seconds: float, sample_rate: int, blocksize: int) -> int:
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError("instale sounddevice e numpy: python -m pip install sounddevice numpy") from exc

    blocks: queue.Queue = queue.Queue(maxsize=128)
    callback_errors: list[str] = []
    dropped = [0]

    def callback(indata, frames, _time, status):
        if status:
            callback_errors.append(str(status))
        try:
            blocks.put_nowait(indata[:, 0].copy())
        except queue.Full:
            dropped[0] += frames

    total = 0
    deadline = time.monotonic() + seconds
    with open(output, "w", newline="") as target:
        writer = csv.writer(target)
        writer.writerow(["amostra_32bit"])
        with sd.InputStream(device=device, samplerate=sample_rate, channels=1,
                            dtype="float32", blocksize=blocksize,
                            callback=callback):
            while time.monotonic() < deadline:
                remaining = max(0.05, deadline - time.monotonic())
                try:
                    block = blocks.get(timeout=min(0.2, remaining))
                except queue.Empty:
                    continue
                values = np.clip(block, -1.0, 1.0) * 2147483647.0
                values = np.rint(values).astype(np.int32)
                writer.writerows((int(value),) for value in values)
                total += len(values)
        # Stop PortAudio before draining the final callbacks, otherwise a slow
        # writer could keep the queue non-empty forever while the stream runs.
        while not blocks.empty():
            try:
                block = blocks.get_nowait()
            except queue.Empty:
                break
            values = np.clip(block, -1.0, 1.0) * 2147483647.0
            values = np.rint(values).astype(np.int32)
            writer.writerows((int(value),) for value in values)
            total += len(values)

    expected = int(round(seconds * sample_rate))
    print(f"{total} amostras gravadas em {output}; esperado aproximadamente {expected}")
    if dropped[0] or abs(total - expected) > sample_rate // 10:
        print(f"ERRO: captura incompleta (amostras descartadas: {dropped[0]})")
        return 2
    if callback_errors:
        print("Avisos do PortAudio: " + "; ".join(sorted(set(callback_errors))))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", help="CSV, por exemplo classe_1_guitarra.csv")
    parser.add_argument("--device", help="nome ou índice da entrada UAC da Placa A")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--sample-rate", type=int, default=44100)
    parser.add_argument("--blocksize", type=int, default=441)
    parser.add_argument("--list", action="store_true", help="lista dispositivos e sai")
    args = parser.parse_args()

    try:
        import sounddevice as sd
    except ImportError as exc:
        raise RuntimeError("instale sounddevice: python -m pip install sounddevice") from exc
    if args.list:
        print(sd.query_devices())
        return 0
    if not args.output:
        parser.error("informe o CSV de saída ou use --list")
    device = int(args.device) if args.device and args.device.isdigit() else args.device
    return record(device, args.output, args.seconds, args.sample_rate, args.blocksize)


if __name__ == "__main__":
    raise SystemExit(main())
