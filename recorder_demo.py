"""Gravador do ADC cru da Placa B com interface gráfica.

Sem argumentos, o programa abre uma interface Tkinter para escolher a porta,
sessão, classe, controles da guitarra, duração e pasta de saída. O modo de
linha de comando continua disponível para automação:

    python recorder_demo.py saida.csv --port /dev/ttyACM0 \
        --session sessao01 --label 1 --conditions "..."

O protocolo AUD1 contém ``magic, count:u16, sequence:u32, int32[count], crc16``.
Ao receber ``S``, a Placa B drena a fila e envia ``AUDE, sequence, crc16``;
esse marcador confirma o fim da tomada e separa uma gravação da próxima.
Arquivos com lacunas ou duração errada são rejeitados e nunca substituem o
CSV final. O áudio processado da Placa A deve ser gravado por
``recorder_usb.py`` para DAW, não para este treinamento.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import queue
import re
import struct
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable, Optional

try:
    import serial
except ImportError:  # a interface pode abrir antes da instalação do pyserial
    serial = None

try:
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - pyserial incompleto
    list_ports = None

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - apenas em ambientes sem Tk
    tk = None
    filedialog = messagebox = ttk = None

MAGIC = b"AUD1"
END_MAGIC = b"AUDE"
HEADER = struct.Struct("<4sHI")
END_HEADER = struct.Struct("<4sI")
RATE = 44100
DATASET_ROOT = Path(os.environ.get(
    "PEDALEIRA_DATASET_DIR", Path.home() / "pedaleira-datasets"
)).expanduser().resolve()


def tools_repo_dir() -> Path:
    override = os.environ.get("PEDALEIRA_TOOLS_DIR")
    tools_dir = (Path(override).expanduser().resolve() if override else
                 Path(__file__).resolve().parent.parent / "pedaleira-tools")
    if not (tools_dir / "profile_collector.py").is_file():
        raise RuntimeError(
            f"ferramentas de profiling ausentes: {tools_dir}; clone pedaleira-tools "
            "como pasta irmã ou defina PEDALEIRA_TOOLS_DIR"
        )
    return tools_dir

GUIDE_SESSIONS = {
    "sessao01": {"position": "1", "tone": "100%"},
    "sessao02": {"position": "2", "tone": "50%"},
    "sessao03": {"position": "3", "tone": "0%"},
    "sessao04": {"position": "4", "tone": "50%"},
    "sessao05": {"position": "5", "tone": "100%"},
}


def _guide_take(code: str, label: int, seconds: int, cable: str, description: str,
                preparation: str, segments: list[tuple[int, int, str]]) -> dict:
    return {
        "code": code, "label": label, "seconds": seconds, "cable": cable,
        "description": description, "preparation": preparation,
        "segments": segments,
    }


GUIDE_TAKES = {
    1: _guide_take(
        "0A", 0, 30, "conectado",
        "Guitarra conectada, cordas paradas e mãos afastadas das cordas e da ponte. Grave somente o ruído natural de repouso.",
        "Conecte a guitarra, pare todas as cordas e afaste as mãos antes de iniciar.",
        [(0, 30, "Mantenha mãos afastadas e deixe apenas o ruído natural de repouso.")],
    ),
    2: _guide_take(
        "0B", 0, 30, "conectado",
        "Guitarra conectada e aterrada pela mão nas cordas ou ponte, sem produzir notas.",
        "Antes de iniciar, encoste a mão nas cordas ou na ponte e abafe todas as cordas.",
        [(0, 30, "Mantenha contato com as cordas ou ponte, sem deixar nenhuma corda soar.")],
    ),
    3: _guide_take(
        "0C", 0, 30, "desconectado",
        "Cabo removido somente do lado da guitarra; plugue livre parado, sem ser tocado.",
        "Desconecte o cabo do lado da guitarra antes de iniciar. Mantenha a outra ponta na entrada do pedal; não grave o ato de desconectar.",
        [(0, 30, "Deixe o plugue livre parado e sem tocar durante toda a captura.")],
    ),
    4: _guide_take(
        "0D", 0, 30, "conectado",
        "Alternância entre mão fora e mão em contato com as cordas/ponte, sem produzir notas.",
        "Reconecte o cabo, espere as cordas pararem e alterne a cada cinco segundos conforme as instruções abaixo.",
        [
            (0, 5, "0–5 s: mão fora das cordas e da ponte."),
            (5, 10, "5–10 s: mão nas cordas ou ponte, toque suave sem produzir nota."),
            (10, 15, "10–15 s: mão fora das cordas e da ponte."),
            (15, 20, "15–20 s: mão nas cordas ou ponte, toque suave sem produzir nota."),
            (20, 25, "20–25 s: mão fora das cordas e da ponte."),
            (25, 30, "25–30 s: mão nas cordas ou ponte, toque suave sem produzir nota."),
        ],
    ),
    5: _guide_take(
        "1A", 1, 60, "conectado",
        "Notas isoladas, escalas, acordes e arpejos, variando intensidade e mantendo notas soando entre ataques.",
        "Comece a tocar antes de iniciar a captura. Evite silêncio nas bordas e pausas deliberadas entre exercícios.",
        [
            (0, 15, "0–15 s: notas isoladas nas seis cordas, graves a agudas, com intensidade média."),
            (15, 30, "15–30 s: escala subindo e descendo, alternando palhetadas fracas e fortes."),
            (30, 45, "30–45 s: acordes abertos e com pestana, alternando batidas leves e fortes."),
            (45, 60, "45–60 s: arpejos com cordas soltas e notas presas; mantenha notas soando entre ataques."),
        ],
    ),
    6: _guide_take(
        "1B", 1, 60, "conectado",
        "Casos difíceis: notas muito fracas, palm mute, sustain em decaimento, bends e vibrato. Alavanca somente com nota soando.",
        "Comece a tocar antes da captura e mantenha o fluxo musical. No campo Alavanca, registre se vai usá-la com nota.",
        [
            (0, 15, "0–15 s: notas isoladas muito fracas, mas distinguíveis do ruído, em cordas graves e agudas."),
            (15, 30, "15–30 s: palm mute leve e moderado; repalhete antes de o som desaparecer."),
            (30, 45, "30–45 s: notas sustentadas, deixando decair e repalhetando enquanto ainda há nota."),
            (45, 60, "45–60 s: bends e vibrato com notas sustentadas; alavanca somente junto de uma nota."),
        ],
    ),
}


def guide_step(session_id: str, take: int) -> dict:
    """Retorna os parâmetros completos de uma etapa do roteiro volume100."""
    if session_id not in GUIDE_SESSIONS:
        raise ValueError(f"Sessão de roteiro inválida: {session_id}")
    if take not in GUIDE_TAKES:
        raise ValueError(f"Take de roteiro inválido: {take}")
    session_number = int(session_id[-2:])
    step = (session_number - 1) * len(GUIDE_TAKES) + take
    step_data = dict(GUIDE_TAKES[take])
    step_data.update(GUIDE_SESSIONS[session_id])
    step_data.update({
        "session_id": session_id, "take": take, "volume": "100%",
        "lever": "parada", "gain": "medio",
        "stage": step,
    })
    return step_data


def guide_instruction(step: dict, audio_seconds: float, finalizing: bool = False) -> str:
    """Seleciona a orientação pelo tempo de áudio efetivamente recebido."""
    if finalizing:
        return "Áudio capturado; aguardando AUDE e finalização dos arquivos…"
    for start, end, instruction in step["segments"]:
        if start <= audio_seconds < end:
            return instruction
    if audio_seconds >= step["seconds"]:
        return "Duração de áudio atingida; aguardando a confirmação AUDE…"
    return step["segments"][0][2]


def crc16_ccitt(data: bytes) -> int:
    """Calcula o mesmo CRC-CCITT usado no transmissor da Placa B."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


class RecordingCancelled(Exception):
    """Indica que o usuário interrompeu a tomada antes de validá-la."""


ProgressCallback = Callable[[int, int, float, int], None]


def record(
    port: str,
    output: str,
    seconds: float,
    session_id: str,
    label: Optional[int],
    conditions: str,
    *,
    stop_event: Optional[threading.Event] = None,
    progress_callback: Optional[ProgressCallback] = None,
    metadata_extra: Optional[dict] = None,
    profile: bool = False,
) -> dict:
    """Grava uma tomada, valida a taxa e retorna suas métricas."""
    if serial is None:
        interpreter = sys.executable or "python"
        install_hint = (
            "Instale no mesmo ambiente que abriu o gravador:\n"
            f"  {interpreter} -m pip install -r requirements.txt"
        )
        raise RuntimeError(
            "pyserial não está instalado neste interpretador:\n"
            f"  {interpreter}\n\n"
            f"{install_hint}"
        )
    destination = Path(output)
    partial = destination.with_name(destination.name + ".partial")
    raw_archive = destination.with_suffix(".aud1")
    raw_partial = raw_archive.with_name(raw_archive.name + ".partial")
    stop_event = stop_event or threading.Event()
    samples = 0
    lost_frames = 0
    crc_errors = 0
    duplicate_blocks = 0
    previous_sequence = None
    buffer = bytearray()
    capture_start = None
    start = time.monotonic()
    last_progress = 0.0
    expected = int(round(seconds * RATE))
    # Durante a captura mantenha somente os bytes PCM. Converter cada amostra
    # para uma tupla e chamar csv.writer a cada bloco deixa o leitor USB
    # vulnerável a pausas curtas do escalonador e pode causar perda de quadros.
    # A conversão para CSV ocorre depois que o fluxo foi parado e drenado.
    captured_pcm = bytearray()
    raw_hash = hashlib.sha256()
    raw_packet_count = 0
    first_sequence = None
    last_sequence = None
    end_sequence = None
    trailing_lost_blocks = 0
    raw_capture = None

    def stop_and_drain(timeout: float = 10.0) -> None:
        """Solicita parada e aguarda o marcador AUDE da Placa B.

        O host continua lendo durante a drenagem porque já pode haver quadros
        AUD1 no endpoint quando ``S`` é processado. Quadros excedentes são
        descartados; o AUDE só é aceito quando o CRC do marcador confere.
        """
        nonlocal end_sequence, trailing_lost_blocks
        # O primeiro S para a captura. Repetimos o comando durante a espera:
        # ele é idempotente na Placa B e permite que o transmissor emita o
        # AUDE novamente caso a primeira escrita tenha ficado retida no
        # endpoint CDC ou tenha sido perdida durante a reconexão USB.
        device.write(b"S")
        device.flush()
        deadline = time.monotonic() + timeout
        retry_at = time.monotonic() + 0.75
        end_size = END_HEADER.size + 2
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= retry_at:
                device.write(b"S")
                device.flush()
                retry_at = now + 0.75
            offset = buffer.find(END_MAGIC)
            if offset < 0:
                # Preserve o sufixo que ainda pode ser o início de AUDE.
                del buffer[: max(0, len(buffer) - len(END_MAGIC) + 1)]
            else:
                if offset:
                    del buffer[:offset]
                if len(buffer) >= end_size:
                    crc_offset = END_HEADER.size
                    received = struct.unpack_from("<H", buffer, crc_offset)[0]
                    calculated = crc16_ccitt(buffer[4:crc_offset])
                    if received == calculated:
                        frame = bytes(buffer[:end_size])
                        if raw_capture is not None:
                            raw_capture.write(frame)
                            raw_hash.update(frame)
                        end_sequence = struct.unpack_from("<I", frame, 4)[0]
                        if last_sequence is not None and end_sequence > last_sequence + 1:
                            trailing_lost_blocks = end_sequence - last_sequence - 1
                        del buffer[:end_size]
                        return
                    del buffer[0]
            available = device.in_waiting
            buffer.extend(device.read(min(max(available, 1), 64 * 1024)))
        raise TimeoutError("A Placa B não confirmou o fim da captura (AUDE).")

    def report_progress(force: bool = False) -> None:
        nonlocal last_progress
        if progress_callback is None:
            return
        now = time.monotonic()
        if not force and now - last_progress < 0.15 and len(captured_pcm) // 4 < expected:
            return
        last_progress = now
        elapsed = now - (capture_start or start)
        progress_callback(len(captured_pcm) // 4, expected, elapsed, lost_frames)

    device = None
    profile_session = None
    profile_artifacts = None
    profile_error = None
    profile_query_seconds = None
    captured_elapsed = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        device = serial.Serial(port, 115200, timeout=0.1, write_timeout=1.0)
        # Limpe uma tentativa anterior sem tornar o início dependente de um
        # marcador AUDE (ele só é obrigatório para fechar a tomada atual).
        # Depois de uma captura normal a fila já foi drenada; após uma queda,
        # este intervalo curto descarta qualquer resíduo antes do novo R.
        device.write(b"S")
        device.flush()
        time.sleep(0.15)
        device.reset_input_buffer()
        if profile:
            # Zera os acumuladores imediatamente antes de R. Durante AUD1 e
            # drenagem não enviamos consultas; os snapshots só começam depois
            # que stop_and_drain validou AUDE.
            sys.path.insert(0, str(tools_repo_dir()))
            from profile_collector import BProfileSession, ProfileB, write_outputs
            profile_session = BProfileSession(
                ProfileB(serial_port=device, timeout=1.0), timeout=1.0)
            profile_session.start()
        device.write(b"R")
        device.flush()

        with partial.open("w", newline="", buffering=1024 * 1024) as target, raw_partial.open("wb", buffering=1024 * 1024) as raw_file:
            raw_capture = raw_file
            # O relógio da tomada é o número de amostras, não o momento em
            # que o primeiro pacote chegou. Assim, blocos que ainda estejam
            # na fila CDC podem ser drenados antes do S final.
            while capture_start is None or len(captured_pcm) // 4 < expected:
                if stop_event.is_set():
                    device.write(b"S")
                    raise RecordingCancelled("gravação interrompida pelo usuário")

                now = time.monotonic()
                if capture_start is None and now - start > 5.0:
                    raise TimeoutError("A placa B não iniciou a captura em 5 segundos.")
                if capture_start is not None and now - capture_start > seconds + 2.0:
                    raise TimeoutError("A Placa B não entregou a duração esperada em tempo hábil.")
                # Ler em lotes grandes reduz chamadas de sistema e o tempo em
                # Python por quadro. O limite evita uma alocação excessiva se
                # o kernel acumular muitos dados durante uma pausa curta.
                available = device.in_waiting
                read_size = min(max(available, 1), 64 * 1024)
                buffer.extend(device.read(read_size))
                while True:
                    audio_offset = buffer.find(MAGIC)
                    end_offset = buffer.find(END_MAGIC)
                    # Não descarte AUDE se ele já chegou junto com os últimos
                    # quadros. A drenagem posterior precisa encontrá-lo.
                    if end_offset >= 0 and (audio_offset < 0 or end_offset < audio_offset):
                        # O S enviado antes do R pode ter sido processado pela
                        # placa depois do reset_input_buffer(). Esse AUDE é da
                        # tomada anterior e deve ser consumido antes do primeiro
                        # AUD1 da nova tomada.
                        if capture_start is None:
                            end_size = END_HEADER.size + 2
                            if end_offset:
                                del buffer[:end_offset]
                            if len(buffer) < end_size:
                                break
                            received = struct.unpack_from("<H", buffer, END_HEADER.size)[0]
                            calculated = crc16_ccitt(buffer[4:END_HEADER.size])
                            if received == calculated:
                                del buffer[:end_size]
                            else:
                                del buffer[0]
                            continue
                        break
                    if audio_offset < 0:
                        # Preserve o maior sufixo possível para AUD1/AUDE.
                        keep = max(len(MAGIC), len(END_MAGIC)) - 1
                        del buffer[: max(0, len(buffer) - keep)]
                        break
                    if audio_offset:
                        del buffer[:audio_offset]
                    if len(buffer) < HEADER.size:
                        break
                    _, count, sequence = HEADER.unpack_from(buffer)
                    if not 0 < count <= 128:
                        del buffer[0]
                        continue
                    packet_size = HEADER.size + count * 4 + 2
                    if len(buffer) < packet_size:
                        break
                    crc_offset = HEADER.size + count * 4
                    received_crc = struct.unpack_from("<H", buffer, crc_offset)[0]
                    calculated_crc = crc16_ccitt(buffer[4:crc_offset])
                    if received_crc != calculated_crc:
                        crc_errors += 1
                        # Descartar somente o magic atual permite reencontrar
                        # o próximo pacote mesmo quando houve byte perdido.
                        del buffer[0]
                        continue
                    # Copie o payload antes de remover o pacote do buffer de
                    # recepção. Não faça unpack das amostras aqui: isso só
                    # aumenta o trabalho enquanto o USB precisa ser drenado.
                    payload = bytes(buffer[HEADER.size:crc_offset])
                    frame = bytes(buffer[:packet_size])
                    raw_capture.write(frame)
                    raw_hash.update(frame)
                    raw_packet_count += 1
                    if first_sequence is None:
                        first_sequence = sequence
                    last_sequence = sequence
                    del buffer[:packet_size]
                    if capture_start is None:
                        capture_start = time.monotonic()
                    if previous_sequence is not None:
                        delta = (sequence - previous_sequence) & 0xFFFFFFFF
                        if delta == 0:
                            duplicate_blocks += 1
                            continue
                        if delta >= 0x80000000:
                            raise RuntimeError(
                                "sequência da Placa B foi reiniciada durante a captura; "
                                "a tomada foi descartada"
                            )
                        lost_frames += delta - 1
                    previous_sequence = sequence
                    captured = len(captured_pcm) // 4
                    remaining = expected - captured
                    if remaining <= 0:
                        continue
                    # O pacote já foi validado pelo CRC. Copiar os bytes
                    # diretamente evita unpack/pack desnecessário no trecho
                    # que precisa acompanhar 44,1 kHz.
                    captured_pcm.extend(payload[:remaining * 4])
                    samples = len(captured_pcm) // 4
                    report_progress()

            stop_and_drain()
            captured_elapsed = time.monotonic() - (capture_start or start)
            if profile_session is not None:
                try:
                    profile_query_started = time.monotonic()
                    profile_session.snapshot_after_aude()
                    profile_prefix = destination.with_name(destination.stem + "_profiling")
                    profile_artifacts = write_outputs(profile_session.report(), profile_prefix)
                    profile_query_seconds = time.monotonic() - profile_query_started
                except Exception as exc:
                    profile_error = str(exc)
            # Agora que a quantidade foi atingida e o fluxo foi solicitado a
            # parar, serializamos o CSV fora do caminho crítico USB.
            writer = csv.writer(target)
            writer.writerow(["amostra_32bit"])
            for offset in range(0, len(captured_pcm), 4 * 8192):
                chunk = captured_pcm[offset:offset + 4 * 8192]
                writer.writerows(struct.iter_unpack("<i", chunk))
            samples = len(captured_pcm) // 4
            target.flush()

        elapsed = captured_elapsed if captured_elapsed is not None else time.monotonic() - (capture_start or start)
        measured_rate = samples / elapsed if elapsed > 0 else 0.0
        if (
            lost_frames
            or crc_errors
            or duplicate_blocks
            or abs(samples - expected) > RATE // 10
            or abs(measured_rate - RATE) > RATE * 0.1
        ):
            raise RuntimeError(
                f"captura rejeitada: {samples} amostras, {elapsed:.1f}s, "
                f"{measured_rate:.0f} Hz, {lost_frames} blocos perdidos, "
                f"{crc_errors} CRC inválidos"
            )

        os.replace(partial, destination)
        os.replace(raw_partial, raw_archive)
        audio_duration = samples / RATE
        csv_hash = hashlib.sha256()
        with destination.open("rb") as csv_file:
            for chunk in iter(lambda: csv_file.read(1024 * 1024), b""):
                csv_hash.update(chunk)
        metadata = {
            "session_id": session_id,
            "label": int(label) if label is not None else None,
            "sample_rate": RATE,
            "samples": samples,
            # A duração do áudio vem da contagem de amostras. O tempo de
            # parede inclui latência e drenagem USB e fica separado para
            # diagnosticar a captura sem alterar a duração do dataset.
            "duration_seconds": audio_duration,
            "capture_elapsed_seconds": elapsed,
            "transport_rate_hz": measured_rate,
            "conditions": conditions,
            "source": "placa_b_adc_raw",
            "capture_protocol": "AUD1+AUDE",
            "raw_archive": raw_archive.name,
            "raw_archive_sha256": raw_hash.hexdigest(),
            "raw_archive_bytes": raw_archive.stat().st_size,
            "raw_packet_count": raw_packet_count,
            "first_sequence": first_sequence,
            "last_sequence": last_sequence,
            "end_sequence": end_sequence,
            "drain_lost_blocks": trailing_lost_blocks,
            "csv_sha256": csv_hash.hexdigest(),
            "crc_errors": crc_errors,
            "lost_blocks": lost_frames,
            "duplicate_blocks": duplicate_blocks,
        }
        if profile_session is not None:
            metadata["profiling"] = {
                "session_id": profile_session.session_id,
                "queried_after_aude": True,
                "post_aude_query_seconds": profile_query_seconds,
                "reports": [path.name for path in profile_artifacts] if profile_artifacts else None,
                "error": profile_error,
            }
        if metadata_extra:
            metadata.update(metadata_extra)
        destination.with_suffix(".json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        report_progress(force=True)
        result = {
            "path": str(destination),
            "raw_archive": str(raw_archive),
            "samples": samples,
            "elapsed": elapsed,
            "rate": measured_rate,
            "lost_frames": lost_frames,
            "duplicate_blocks": duplicate_blocks,
            "crc_errors": crc_errors,
            "profile_query_seconds": profile_query_seconds,
            "profiling_reports": [str(path) for path in profile_artifacts] if profile_artifacts else None,
            "profiling_error": profile_error,
        }
        print(f"{samples} amostras gravadas em {destination} ({measured_rate:.0f} Hz)")
        return result
    except Exception:
        if partial.exists():
            partial.unlink()
        if raw_partial.exists():
            raw_partial.unlink()
        raise
    finally:
        # Nunca deixe a Placa B transmitindo depois de timeout, erro de CRC,
        # desconexão ou cancelamento. O firmware limpa a fila ao receber S.
        if device is not None:
            try:
                device.write(b"S")
                device.flush()
            except Exception:
                pass
            try:
                device.close()
            except Exception:
                pass


def slug(value: str, fallback: str = "sem_nome") -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    clean = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").lower()
    return clean or fallback


class RecorderApp:
    """Interface Tkinter; a captura roda fora da thread da janela."""

    def __init__(self, root: "tk.Tk") -> None:
        self.root = root
        self.root.title("Smart Noise Gate — gravador de dataset")
        self.root.minsize(900, 870)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.events: queue.Queue = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.capture_finalizing = False
        self.form_widgets: list = []
        self.guide_locked_widgets: list = []
        self._changing_guide = False
        self._guided_output_initialized = False
        self._guided_output_dir = str(DATASET_ROOT / "dataset_volume100")
        self.custom_values = None
        self.guided_values = None
        self.recording_active = False
        self._last_saved_stage = None

        self.port_var = tk.StringVar(value="/dev/ttyACM0")
        self.mode_var = tk.StringVar(value="Personalizado")
        self.output_dir_var = tk.StringVar(value=str(DATASET_ROOT))
        self.session_var = tk.StringVar(value="sessao01")
        self.take_var = tk.IntVar(value=1)
        self.duration_var = tk.DoubleVar(value=60.0)
        self.label_var = tk.IntVar(value=0)
        self.position_var = tk.StringVar(value="1")
        self.volume_var = tk.StringVar(value="100%")
        self.tone_var = tk.StringVar(value="100%")
        self.lever_var = tk.StringVar(value="parada")
        self.gain_var = tk.StringVar(value="medio")
        self.cable_var = tk.StringVar(value="conectado")
        self.environment_var = tk.StringVar(value="")
        self.notes_var = tk.StringVar(value="")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.status_var = tk.StringVar(value="Pronto para gravar.")
        self.progress_detail_var = tk.StringVar(value="0 / 0 amostras")
        self.guide_stage_var = tk.StringVar(value="")
        self.guide_description_var = tk.StringVar(value="")
        self.guide_preparation_var = tk.StringVar(value="")
        self.guide_instruction_var = tk.StringVar(value="")
        self.guide_saved_var = tk.StringVar(value="")

        self.build_widgets()
        self.custom_values = self.read_form_values()
        self.mode_var.trace_add("write", self.on_mode_changed)
        self.session_var.trace_add("write", self.on_guide_selection_changed)
        self.take_var.trace_add("write", self.on_guide_selection_changed)
        self.output_dir_var.trace_add("write", self.on_output_dir_changed)
        self.apply_mode()
        self.refresh_ports()
        self.update_label_hint()
        self.root.after(100, self.poll_events)

    def add_form(self, widget):
        self.form_widgets.append(widget)
        return widget

    def build_widgets(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(6, weight=1)

        mode_frame = ttk.LabelFrame(root, text="Modo de captura")
        mode_frame.grid(row=0, column=0, padx=10, pady=(10, 5), sticky="ew")
        ttk.Label(mode_frame, text="Roteiro:").grid(row=0, column=0, padx=6, pady=5, sticky="w")
        self.mode_combo = self.add_form(ttk.Combobox(
            mode_frame, textvariable=self.mode_var,
            values=["Personalizado", "Roteiro — volume 100%"],
            state="readonly", width=28,
        ))
        self.mode_combo.grid(row=0, column=1, padx=6, pady=5, sticky="w")
        ttk.Label(mode_frame, text="O modo Roteiro preenche os parâmetros das 30 etapas e identifica os metadados.").grid(
            row=0, column=2, padx=8, pady=5, sticky="w"
        )

        connection = ttk.LabelFrame(root, text="Captura e identificação")
        connection.grid(row=1, column=0, padx=10, pady=5, sticky="ew")
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="Porta da Placa B:").grid(row=0, column=0, padx=6, pady=5, sticky="w")
        self.port_combo = self.add_form(ttk.Combobox(connection, textvariable=self.port_var, width=24))
        self.port_combo.grid(row=0, column=1, padx=6, pady=5, sticky="w")
        self.refresh_button = self.add_form(ttk.Button(connection, text="Atualizar portas", command=self.refresh_ports))
        self.refresh_button.grid(row=0, column=2, padx=6, pady=5)
        ttk.Label(connection, text="Pasta de saída:").grid(row=1, column=0, padx=6, pady=5, sticky="w")
        self.output_entry = self.add_form(ttk.Entry(connection, textvariable=self.output_dir_var))
        self.output_entry.grid(row=1, column=1, padx=6, pady=5, sticky="ew")
        self.browse_button = self.add_form(ttk.Button(connection, text="Escolher...", command=self.choose_output_dir))
        self.browse_button.grid(row=1, column=2, padx=6, pady=5)
        ttk.Label(connection, text="Sessão:").grid(row=2, column=0, padx=6, pady=5, sticky="w")
        self.session_entry = self.add_form(ttk.Entry(connection, textvariable=self.session_var, width=24))
        self.session_entry.grid(row=2, column=1, padx=6, pady=5, sticky="w")
        self.guide_session_combo = self.add_form(ttk.Combobox(
            connection, textvariable=self.session_var, values=list(GUIDE_SESSIONS),
            state="readonly", width=21,
        ))
        self.guide_session_combo.grid(row=2, column=1, padx=6, pady=5, sticky="w")
        self.guide_session_combo.grid_remove()
        ttk.Label(connection, text="Tomada:").grid(row=2, column=2, padx=(6, 0), pady=5, sticky="e")
        self.take_spin = self.add_form(ttk.Spinbox(connection, from_=1, to=999, textvariable=self.take_var, width=6))
        self.take_spin.grid(row=2, column=3, padx=6, pady=5, sticky="w")
        ttk.Label(connection, text="Duração (s):").grid(row=3, column=0, padx=6, pady=5, sticky="w")
        self.duration_spin = self.add_form(ttk.Spinbox(connection, from_=1, to=3600, increment=1, textvariable=self.duration_var, width=8))
        self.duration_spin.grid(row=3, column=1, padx=6, pady=5, sticky="w")
        self.label_hint = ttk.Label(connection, textvariable=self.status_var, foreground="#555")
        self.label_hint.grid(row=3, column=2, columnspan=2, padx=6, pady=5, sticky="e")

        classes = ttk.LabelFrame(root, text="Classe")
        classes.grid(row=2, column=0, padx=10, pady=5, sticky="ew")
        self.class_noise = self.add_form(ttk.Radiobutton(classes, text="0 — ruído / sem corda vibrando", variable=self.label_var, value=0, command=self.update_label_hint))
        self.class_noise.grid(row=0, column=0, padx=10, pady=5, sticky="w")
        self.class_guitar = self.add_form(ttk.Radiobutton(classes, text="1 — presença de guitarra", variable=self.label_var, value=1, command=self.update_label_hint))
        self.class_guitar.grid(row=0, column=1, padx=10, pady=5, sticky="w")
        self.guide_locked_widgets.extend([self.class_noise, self.class_guitar])

        controls = ttk.LabelFrame(root, text="Controles da única guitarra passiva")
        controls.grid(row=3, column=0, padx=10, pady=5, sticky="ew")
        for column in (1, 3, 5):
            controls.columnconfigure(column, weight=1)
        self.position_combo = self.add_combo(controls, "Chave (posição):", self.position_var, ["1", "2", "3", "4", "5"], 0, 0)
        self.volume_combo = self.add_combo(controls, "Volume:", self.volume_var, ["100%", "75%", "50%", "25%"], 0, 2)
        self.tone_combo = self.add_combo(controls, "Tone:", self.tone_var, ["100%", "50%", "0%"], 0, 4)
        self.lever_combo = self.add_combo(controls, "Alavanca:", self.lever_var, ["parada", "manuseio sem nota", "vibrato/bend com nota"], 1, 0, width=23)
        self.add_combo(controls, "Ganho:", self.gain_var, ["baixo", "medio", "alto"], 1, 2)
        self.cable_combo = self.add_combo(controls, "Cabo:", self.cable_var, ["conectado", "desconectado", "movimento"], 1, 4)
        self.guide_locked_widgets.extend([self.position_combo, self.volume_combo, self.tone_combo, self.cable_combo])
        ttk.Label(controls, text="Ambiente:").grid(row=2, column=0, padx=6, pady=5, sticky="w")
        self.environment_entry = self.add_form(ttk.Entry(controls, textvariable=self.environment_var))
        self.environment_entry.grid(row=2, column=1, columnspan=5, padx=6, pady=5, sticky="ew")
        ttk.Label(controls, text="Observações:").grid(row=3, column=0, padx=6, pady=5, sticky="w")
        self.notes_entry = self.add_form(ttk.Entry(controls, textvariable=self.notes_var))
        self.notes_entry.grid(row=3, column=1, columnspan=5, padx=6, pady=5, sticky="ew")

        self.guide_panel = ttk.LabelFrame(root, text="Instruções do roteiro")
        self.guide_panel.grid(row=4, column=0, padx=10, pady=5, sticky="ew")
        self.guide_panel.columnconfigure(0, weight=1)
        ttk.Label(self.guide_panel, textvariable=self.guide_stage_var, font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, columnspan=4, padx=8, pady=(7, 2), sticky="w"
        )
        ttk.Label(self.guide_panel, textvariable=self.guide_description_var, wraplength=980, justify="left").grid(
            row=1, column=0, columnspan=4, padx=8, pady=3, sticky="ew"
        )
        ttk.Label(self.guide_panel, textvariable=self.guide_preparation_var, wraplength=980, justify="left").grid(
            row=2, column=0, columnspan=4, padx=8, pady=3, sticky="ew"
        )
        self.guide_instruction_label = ttk.Label(
            self.guide_panel, textvariable=self.guide_instruction_var,
            wraplength=980, justify="left", foreground="#174f86",
        )
        self.guide_instruction_label.grid(row=3, column=0, columnspan=4, padx=8, pady=4, sticky="ew")
        ttk.Label(self.guide_panel, textvariable=self.guide_saved_var, foreground="#286b32").grid(
            row=4, column=0, padx=8, pady=(0, 4), sticky="w"
        )
        self.previous_button = ttk.Button(self.guide_panel, text="Anterior", command=lambda: self.move_guide(-1))
        self.previous_button.grid(row=4, column=1, padx=5, pady=4, sticky="e")
        self.next_button = ttk.Button(self.guide_panel, text="Próximo take", command=lambda: self.move_guide(1))
        self.next_button.grid(row=4, column=2, padx=5, pady=4, sticky="e")
        self.stage_combo = self.add_form(ttk.Combobox(self.guide_panel, state="readonly", width=27))
        self.stage_combo.grid(row=4, column=3, padx=8, pady=4, sticky="e")
        self.stage_combo.bind("<<ComboboxSelected>>", self.on_stage_selected)
        self.stage_options = [
            f"{i:02d} {sid} take{take:02d} • {guide_step(sid, take)['code']}"
            for i, (sid, take) in enumerate(
                ((session, n) for session in GUIDE_SESSIONS for n in GUIDE_TAKES), start=1
            )
        ]
        self.stage_combo.configure(values=self.stage_options)

        action = ttk.Frame(root)
        action.grid(row=5, column=0, padx=10, pady=5, sticky="ew")
        self.start_button = ttk.Button(action, text="Iniciar gravação", command=self.start_recording)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(action, text="Parar e descartar", command=self.stop_recording, state="disabled")
        self.stop_button.pack(side="left")
        ttk.Label(action, textvariable=self.progress_detail_var).pack(side="right")

        progress = ttk.Frame(root)
        progress.grid(row=6, column=0, padx=10, pady=5, sticky="nsew")
        progress.columnconfigure(0, weight=1)
        progress.rowconfigure(1, weight=1)
        self.progress_bar = ttk.Progressbar(progress, variable=self.progress_var, maximum=100)
        self.progress_bar.grid(row=0, column=0, sticky="ew")
        self.log = tk.Text(progress, height=8, state="disabled", wrap="word")
        self.log.grid(row=1, column=0, pady=(6, 0), sticky="nsew")
        self.append_log("Interface pronta. Selecione a porta da Placa B e confira os controles antes de iniciar.")

    def add_combo(self, parent, label, variable, values, row, column, width=12):
        ttk.Label(parent, text=label).grid(row=row, column=column, padx=6, pady=5, sticky="w")
        combo = self.add_form(ttk.Combobox(parent, textvariable=variable, values=values, width=width, state="readonly"))
        combo.grid(row=row, column=column + 1, padx=6, pady=5, sticky="ew")
        return combo

    def append_log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def refresh_ports(self) -> None:
        values = []
        if list_ports is not None:
            values = [port.device for port in list_ports.comports()]
        current = self.port_var.get()
        if current and current not in values:
            values.insert(0, current)
        self.port_combo["values"] = values
        if values and current not in values:
            self.port_var.set(values[0])
        self.append_log("Portas atualizadas: " + (", ".join(values) if values else "nenhuma detectada"))

    def choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir_var.get())
        if selected:
            self.output_dir_var.set(selected)

    def read_form_values(self) -> dict:
        return {
            "output_dir": self.output_dir_var.get(),
            "session": self.session_var.get(),
            "take": self.take_var.get(),
            "duration": self.duration_var.get(),
            "label": self.label_var.get(),
            "position": self.position_var.get(),
            "volume": self.volume_var.get(),
            "tone": self.tone_var.get(),
            "lever": self.lever_var.get(),
            "gain": self.gain_var.get(),
            "cable": self.cable_var.get(),
            "environment": self.environment_var.get(),
            "notes": self.notes_var.get(),
        }

    def restore_form_values(self, values: dict) -> None:
        self._changing_guide = True
        try:
            self.output_dir_var.set(values["output_dir"])
            self.session_var.set(values["session"])
            self.take_var.set(values["take"])
            self.duration_var.set(values["duration"])
            self.label_var.set(values["label"])
            self.position_var.set(values["position"])
            self.volume_var.set(values["volume"])
            self.tone_var.set(values["tone"])
            self.lever_var.set(values["lever"])
            self.gain_var.set(values["gain"])
            self.cable_var.set(values["cable"])
            self.environment_var.set(values["environment"])
            self.notes_var.set(values["notes"])
        finally:
            self._changing_guide = False

    def on_mode_changed(self, *_args) -> None:
        if getattr(self, "_changing_guide", False):
            return
        if self.mode_var.get() == "Roteiro — volume 100%":
            self.custom_values = self.read_form_values()
            if not getattr(self, "_guided_output_initialized", False):
                self._guided_output_dir = str(DATASET_ROOT / "dataset_volume100")
                self._guided_output_initialized = True
                self.guided_values = {
                    **self.read_form_values(), "session": "sessao01", "take": 1,
                    "output_dir": self._guided_output_dir,
                }
            if self.guided_values:
                self.restore_form_values(self.guided_values)
            self._guide_selection_initialized = True
            self._changing_guide = True
            try:
                self.session_var.set(self.guided_values["session"])
                self.take_var.set(self.guided_values["take"])
                self.output_dir_var.set(self.guided_values["output_dir"])
            finally:
                self._changing_guide = False
            self.apply_guide_step()
        else:
            if getattr(self, "_guide_selection_initialized", False):
                self._guided_output_dir = self.output_dir_var.get()
                self.guided_values = self.read_form_values()
            if self.custom_values:
                self.restore_form_values(self.custom_values)
        self.apply_mode()

    def apply_mode(self) -> None:
        guided = self.mode_var.get() == "Roteiro — volume 100%"
        busy = self.recording_active
        if not busy:
            for widget in self.form_widgets:
                try:
                    if isinstance(widget, ttk.Combobox):
                        widget.configure(state="readonly")
                    else:
                        widget.configure(state="normal")
                except tk.TclError:
                    pass
        if guided:
            self.session_entry.grid_remove()
            self.guide_session_combo.grid()
            self.guide_panel.grid()
            self.take_spin.configure(from_=1, to=6)
            self.duration_spin.configure(state="readonly")
            for widget in self.guide_locked_widgets:
                widget.configure(state="disabled")
            self.position_combo.configure(state="disabled")
            self.volume_combo.configure(state="disabled")
            self.tone_combo.configure(state="disabled")
            self.cable_combo.configure(state="disabled")
            self.class_noise.configure(state="disabled")
            self.class_guitar.configure(state="disabled")
            self.take_spin.configure(state="readonly")
            if self.take_var.get() == 6:
                self.lever_combo.configure(values=["parada", "vibrato/bend com nota"], state="readonly")
            else:
                self.lever_var.set("parada")
                self.lever_combo.configure(state="disabled")
            self.stage_combo.configure(values=self.stage_options)
            self.refresh_guide_view()
            self.start_button.configure(text="Iniciar gravação")
            self.status_var.set("Roteiro: confira a preparação e acompanhe a instrução atual.")
        else:
            self.session_entry.grid()
            self.guide_session_combo.grid_remove()
            self.guide_panel.grid_remove()
            self.take_spin.configure(from_=1, to=999, state="normal")
            self.duration_spin.configure(state="normal")
            self.lever_combo.configure(values=["parada", "manuseio sem nota", "vibrato/bend com nota"], state="readonly")
            self.start_button.configure(text="Iniciar gravação")
            self.update_label_hint()
        self.mode_combo.configure(state="disabled" if busy else "readonly")
        self.previous_button.configure(state="disabled" if busy or not guided or self.current_stage() <= 1 else "normal")
        self.next_button.configure(state="disabled" if busy or not guided or self.current_stage() >= 30 else "normal")
        self.stage_combo.configure(state="disabled" if busy or not guided else "readonly")
        if busy:
            for widget in self.form_widgets:
                try:
                    widget.configure(state="disabled")
                except tk.TclError:
                    pass
            self.previous_button.configure(state="disabled")
            self.next_button.configure(state="disabled")

    def current_stage(self) -> int:
        if self.session_var.get() not in GUIDE_SESSIONS:
            return 1
        try:
            return guide_step(self.session_var.get(), int(self.take_var.get()))["stage"]
        except (ValueError, TypeError, tk.TclError):
            return 1

    def on_guide_selection_changed(self, *_args) -> None:
        if (self._changing_guide or
                self.mode_var.get() != "Roteiro — volume 100%" or
                not hasattr(self, "guide_panel")):
            return
        if self.session_var.get() not in GUIDE_SESSIONS:
            return
        try:
            take = int(self.take_var.get())
        except (ValueError, TypeError, tk.TclError):
            return
        if take not in GUIDE_TAKES:
            return
        self._guide_selection_initialized = True
        self._guided_output_dir = self.output_dir_var.get()
        self.guided_values = self.read_form_values()
        self.apply_guide_step()
        self.apply_mode()

    def on_output_dir_changed(self, *_args) -> None:
        if (not self._changing_guide and
                self.mode_var.get() == "Roteiro — volume 100%" and
                hasattr(self, "guide_panel")):
            self._guided_output_dir = self.output_dir_var.get()
            self.refresh_guide_view()

    def apply_guide_step(self) -> None:
        step = guide_step(self.session_var.get(), int(self.take_var.get()))
        self._changing_guide = True
        try:
            self.label_var.set(step["label"])
            self.duration_var.set(step["seconds"])
            self.position_var.set(step["position"])
            self.volume_var.set(step["volume"])
            self.tone_var.set(step["tone"])
            self.cable_var.set(step["cable"])
            if step["take"] != 6 or self.lever_var.get() not in {"parada", "vibrato/bend com nota"}:
                self.lever_var.set("parada")
        finally:
            self._changing_guide = False
        self.refresh_guide_view()

    def refresh_guide_view(self, audio_seconds: float = 0.0, finalizing: bool = False) -> None:
        if self.session_var.get() not in GUIDE_SESSIONS:
            return
        try:
            step = guide_step(self.session_var.get(), int(self.take_var.get()))
        except (ValueError, TypeError, tk.TclError):
            return
        self.guide_stage_var.set(
            f"Etapa {step['stage']} de 30 — {step['session_id']}, take {step['take']:02d}, "
            f"código {step['code']} — {step['label']} ({step['seconds']} s)"
        )
        self.guide_description_var.set("Take: " + step["description"])
        self.guide_preparation_var.set("Preparação: " + step["preparation"])
        self.guide_instruction_var.set(guide_instruction(step, audio_seconds, finalizing))
        selected = self.stage_combo.current()
        if selected != step["stage"] - 1:
            self.stage_combo.current(step["stage"] - 1)
        try:
            output = self.output_path()
            self.guide_saved_var.set(f"Arquivo: {output}")
        except (ValueError, tk.TclError):
            self.guide_saved_var.set("")
        if self._last_saved_stage == step["stage"]:
            self.guide_saved_var.set(f"Salvo: {output}")
        elif not finalizing:
            self.guide_saved_var.set(f"Arquivo: {output}")
        busy = self.recording_active
        self.previous_button.configure(state="disabled" if busy or step["stage"] <= 1 else "normal")
        self.next_button.configure(state="disabled" if busy or step["stage"] >= 30 else "normal")

    def move_guide(self, offset: int) -> None:
        if self.mode_var.get() != "Roteiro — volume 100%" or self.worker and self.worker.is_alive():
            return
        stage = min(30, max(1, self.current_stage() + offset))
        session_number, take = divmod(stage - 1, 6)
        self._changing_guide = True
        try:
            self.session_var.set(f"sessao{session_number + 1:02d}")
            self.take_var.set(take + 1)
        finally:
            self._changing_guide = False
        self.apply_guide_step()
        self.apply_mode()

    def on_stage_selected(self, _event=None) -> None:
        if self.mode_var.get() != "Roteiro — volume 100%":
            return
        stage = self.stage_combo.current() + 1
        if not 1 <= stage <= 30:
            return
        session_number, take = divmod(stage - 1, 6)
        self._changing_guide = True
        try:
            self.session_var.set(f"sessao{session_number + 1:02d}")
            self.take_var.set(take + 1)
        finally:
            self._changing_guide = False
        self.apply_guide_step()
        self.apply_mode()

    def update_label_hint(self) -> None:
        if self.mode_var.get() == "Roteiro — volume 100%":
            return
        if self.label_var.get() == 0:
            self.status_var.set("Classe 0: nenhuma corda deve vibrar durante a tomada.")
        else:
            self.status_var.set("Classe 1: inclua ataques, notas fracas e decaimentos.")

    def make_conditions(self, guide: Optional[dict] = None) -> tuple[str, dict]:
        notes = self.notes_var.get().strip()
        controls = {
            "guitarra": "passiva",
            "chave_posicao": self.position_var.get(),
            "volume": self.volume_var.get(),
            "tone": self.tone_var.get(),
            "alavanca": self.lever_var.get(),
            "ganho": self.gain_var.get(),
            "cabo": self.cable_var.get(),
            "ambiente": self.environment_var.get().strip(),
            "observacoes": notes,
        }
        guide_metadata = None
        if guide is not None:
            automatic = f"take_code={guide['code']}; descricao_take={guide['description']}"
            controls["observacoes"] = automatic + (f"; observacoes_livres={notes}" if notes else "")
            guide_metadata = {
                "id": "volume100", "version": 1,
                "stage": guide["stage"], "take_code": guide["code"],
            }
        text = ", ".join(f"{key}={value}" for key, value in controls.items() if value)
        if guide_metadata is not None:
            text += f", roteiro=volume100, take_code={guide['code']}, descricao_take={guide['description']}"
            if notes:
                text += f", observacoes_livres={notes}"
        return text, controls

    def output_path(self) -> Path:
        session = slug(self.session_var.get(), "sessao")
        try:
            take = int(self.take_var.get())
        except (TypeError, ValueError, tk.TclError) as exc:
            raise ValueError("A tomada deve ser um número inteiro.") from exc
        if take < 1:
            raise ValueError("A tomada deve ser maior ou igual a 1.")
        selected = Path(self.output_dir_var.get()).expanduser()
        # A classe determina a pasta final. Se o usuário selecionou class0 ou
        # class1 diretamente e depois mudou o rádio da classe, use a pasta
        # irmã correta em vez de misturar rótulos no mesmo diretório.
        if selected.name.lower() in {"class0", "class1"}:
            root = selected.parent
        else:
            root = selected
        class_dir = root / f"class{self.label_var.get()}"
        return class_dir / f"classe_{self.label_var.get()}_{session}_take{take:02d}.csv"

    def destination_conflicts(self, output: Path) -> list[Path]:
        raw_archive = output.with_suffix(".aud1")
        candidates = [output, output.with_suffix(".json"), raw_archive]
        return [path for path in candidates if path.exists()]

    def start_recording(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        port = self.port_var.get().strip()
        session = self.session_var.get().strip()
        if not port:
            messagebox.showerror("Porta ausente", "Escolha a porta serial da Placa B.")
            return
        if not session:
            messagebox.showerror("Sessão ausente", "Informe um identificador de sessão.")
            return
        try:
            seconds = float(self.duration_var.get())
            if seconds <= 0:
                raise ValueError
            take = int(self.take_var.get())
            guided = self.mode_var.get() == "Roteiro — volume 100%"
            guide = guide_step(session, take) if guided else None
            label = int(self.label_var.get())
            output = self.output_path()
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("Dados inválidos", str(exc) or "Informe uma duração válida.")
            return
        conflicts = self.destination_conflicts(output)
        if conflicts:
            paths = "\n".join(str(path) for path in conflicts)
            messagebox.showerror("Destino já utilizado", f"Nenhum arquivo foi substituído. Escolha outra pasta ou etapa:\n{paths}")
            return

        if guided:
            guide = dict(guide)
            guide["session_id"] = session
            guide["take"] = take
            guide["lever"] = self.lever_var.get()
            if take == 6 and guide["lever"] not in {"parada", "vibrato/bend com nota"}:
                messagebox.showerror("Alavanca inválida", "No take 06, escolha 'parada' ou 'vibrato/bend com nota'.")
                return
        conditions, controls = self.make_conditions(guide)
        metadata_extra = {"take": take, "controls": dict(controls)}
        if guide is not None:
            metadata_extra["capture_guide"] = {
                "id": "volume100", "version": 1,
                "stage": guide["stage"], "take_code": guide["code"],
            }
        # Estes valores são snapshots: a thread da gravação não consulta mais
        # StringVar nem estado mutável da interface.
        session_snapshot = str(session)
        label_snapshot = label
        conditions_snapshot = str(conditions)
        metadata_snapshot = dict(metadata_extra)
        guide_snapshot = dict(guide) if guide is not None else None
        self.stop_event = threading.Event()
        self.capture_finalizing = False
        self.progress_var.set(0)
        self.progress_detail_var.set("aguardando a Placa B...")
        self.status_var.set("Aguardando o início da captura...")
        if guided:
            self._last_saved_stage = None
            self.refresh_guide_view()
        self.append_log(f"Iniciando classe {label_snapshot} — {output.name}")
        self.set_running(True)
        self.worker = threading.Thread(
            target=self.capture_worker,
            args=(port, output, seconds, session_snapshot, label_snapshot,
                  conditions_snapshot, metadata_snapshot, guide_snapshot),
            daemon=True,
        )
        self.worker.start()

    def capture_worker(self, port, output, seconds, session, label, conditions,
                       metadata_extra, guide) -> None:
        finalizing_sent = False

        def progress(samples, expected, elapsed, lost):
            self.events.put(("progress", samples, expected, elapsed, lost))
            nonlocal finalizing_sent
            if guide is not None and samples >= expected and not finalizing_sent:
                finalizing_sent = True
                self.events.put(("finalizing",))

        try:
            result = record(
                port,
                str(output),
                seconds,
                session,
                label,
                conditions,
                stop_event=self.stop_event,
                progress_callback=progress,
                metadata_extra=metadata_extra,
            )
            self.events.put(("done", result, guide["stage"] if guide else None))
        except RecordingCancelled:
            self.events.put(("cancelled",))
        except Exception as exc:  # exibido na thread da UI
            self.events.put(("error", str(exc)))

    def poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    _, samples, expected, elapsed, lost = event
                    percent = 100.0 * samples / expected if expected else 0.0
                    self.progress_var.set(min(100.0, percent))
                    self.progress_detail_var.set(f"{samples:,} / {expected:,} amostras — {elapsed:.1f}s — perdas: {lost}")
                    if not self.capture_finalizing:
                        self.status_var.set("Gravando...")
                    if (self.mode_var.get() == "Roteiro — volume 100%"
                            and not self.capture_finalizing):
                        self.refresh_guide_view(samples / RATE)
                elif kind == "finalizing":
                    if self.mode_var.get() == "Roteiro — volume 100%":
                        self.capture_finalizing = True
                        self.status_var.set("Finalizando captura...")
                        self.refresh_guide_view(finalizing=True)
                elif kind == "done":
                    result, stage = event[1], event[2]
                    self.capture_finalizing = False
                    self.progress_var.set(100.0)
                    self.progress_detail_var.set(f"{result['samples']:,} amostras — {result['rate']:.0f} Hz")
                    if stage is None:
                        self.take_var.set(int(self.take_var.get()) + 1)
                    else:
                        self._last_saved_stage = stage
                        self.refresh_guide_view()
                    self.set_running(False)
                    self.status_var.set("Tomada validada e salva.")
                    self.append_log(f"OK: {result['path']} ({result['rate']:.0f} Hz)")
                elif kind == "cancelled":
                    self.capture_finalizing = False
                    self.set_running(False)
                    self.status_var.set("Captura descartada.")
                    self.append_log("Captura interrompida; o arquivo parcial foi removido.")
                elif kind == "error":
                    self.capture_finalizing = False
                    self.set_running(False)
                    self.status_var.set("Falha na captura.")
                    self.append_log("ERRO: " + event[1])
                    messagebox.showerror("Falha na captura", event[1])
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def set_running(self, running: bool) -> None:
        self.recording_active = running
        if running:
            for widget in self.form_widgets:
                try:
                    widget.configure(state="disabled")
                except tk.TclError:
                    pass
            self.mode_combo.configure(state="disabled")
            self.previous_button.configure(state="disabled")
            self.next_button.configure(state="disabled")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
        else:
            self.apply_mode()
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")

    def stop_recording(self) -> None:
        if self.worker and self.worker.is_alive():
            self.status_var.set("Parando; a captura atual será descartada...")
            self.stop_event.set()
            self.stop_button.configure(state="disabled")

    def close(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.root.after(100, self.close)
            return
        self.root.destroy()


def launch_gui() -> None:
    if tk is None:
        raise SystemExit(
            "Tkinter/Tk não está disponível. No Arch Linux, instale com "
            "'sudo pacman -S tk'; no Debian/Ubuntu, use 'sudo apt install python3-tk'."
        )
    root = tk.Tk()
    RecorderApp(root)
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", help="CSV final; sem esse argumento abre a interface gráfica")
    parser.add_argument("--gui", action="store_true", help="abre a interface gráfica explicitamente")
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--session", help="identificador independente da sessão")
    parser.add_argument("--label", type=int, choices=(0, 1), help="0=ruído, 1=guitarra")
    parser.add_argument(
        "--conditions", default="",
        help="guitarra passiva, posição da chave, volume, tone, alavanca, ganho, cabo e ambiente",
    )
    parser.add_argument("--profile", action="store_true",
        help="mede a captura: reseta antes de R e consulta apenas após AUDE (firmware *-profile)")
    args = parser.parse_args()

    if args.gui or args.output is None:
        launch_gui()
        return
    if args.session is None or args.label is None:
        parser.error("--session e --label são obrigatórios no modo CLI")
    record(args.port, args.output, args.seconds, args.session, args.label, args.conditions,
           profile=args.profile)


if __name__ == "__main__":
    main()
