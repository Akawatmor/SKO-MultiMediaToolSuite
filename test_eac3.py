#!/usr/bin/env python3
# test_eac3_matrix.py
#
# One-command black-box test for E-AC-3 (.ec3 / Dolby Digital Plus):
#   5.1 PCM WAV -> Dolby DEE E-AC-3 (DDP) / 448 kbps
#   -> FFmpeg decodes the EC3 back to stereo PCM
#   -> measure channel coefficients + phase -> classify:
#        Lo/Ro
#        Dolby Surround / legacy Pro Logic
#        Dolby Pro Logic II
#
# Usage:
#   python test_eac3_matrix.py [optional_xml_template.xml]

from __future__ import annotations

import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from xml.etree import ElementTree as ET


SR = 48_000
TONE_HZ = 1_000.0
AMP = 0.25
LEAD_S = 0.50
SEG_S = 2.00
TAIL_S = 0.50
FADE_S = 0.10

# Standard 5.1 WAV order: FL, FR, C, LFE, Ls, Rs
SEGMENTS = [
    ("FL",  "FL",  TONE_HZ),
    ("FR",  "FR",  TONE_HZ),
    ("C",   "C",   TONE_HZ),
    ("LFE", "LFE",   60.0),
    ("Ls",  "Ls",  TONE_HZ),
    ("Rs",  "Rs",  TONE_HZ),
]


def die(msg: str, rc: int = 1) -> None:
    print(f"\nERROR: {msg}", file=sys.stderr)
    raise SystemExit(rc)


def find_tool(name: str) -> str:
    p = shutil.which(name)
    if p:
        return p
    if os.name == "nt" and not name.lower().endswith(".exe"):
        p = shutil.which(name + ".exe")
        if p:
            return p
    die(f"'{name}' was not found in PATH.")


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    shown = subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(cmd)
    print("\n>", shown)
    subprocess.run(cmd, cwd=cwd, check=True)


def write_pcm_5_1(path: Path) -> None:
    """Create valid 48 kHz / 16-bit / 6-channel WAVE_FORMAT_EXTENSIBLE WAV."""
    channels = 6
    bits = 16
    bytes_per_sample = bits // 8
    block_align = channels * bytes_per_sample
    byte_rate = SR * block_align
    channel_mask = 0x003F  # Standard 5.1 (FL | FR | FC | LFE | BL | BR)

    lead = int(LEAD_S * SR)
    seg = int(SEG_S * SR)
    tail = int(TAIL_S * SR)
    fade = int(FADE_S * SR)

    frames_total = lead + len(SEGMENTS) * seg + tail

    phase_step_1k = 2.0 * math.pi * TONE_HZ / SR
    phase_step_lfe = 2.0 * math.pi * 60.0 / SR

    pcm = bytearray()

    def sample_for(seg_pos: int, hz: float, step: float) -> float:
        gain = 1.0
        if seg_pos < fade:
            gain *= 0.5 - 0.5 * math.cos(math.pi * seg_pos / fade)
        if seg_pos >= seg - fade:
            x = seg - 1 - seg_pos
            gain *= 0.5 - 0.5 * math.cos(math.pi * x / fade)

        phase = step * seg_pos
        return AMP * gain * math.sin(phase)

    channel_index = {"FL": 0, "FR": 1, "C": 2, "LFE": 3, "Ls": 4, "Rs": 5}

    for frame in range(frames_total):
        samples = [0.0] * channels
        if lead <= frame < lead + len(SEGMENTS) * seg:
            rel = frame - lead
            seg_index = rel // seg
            seg_pos = rel % seg
            _, ch_name, hz = SEGMENTS[seg_index]
            ch = channel_index[ch_name]
            step = phase_step_lfe if hz == 60.0 else phase_step_1k
            samples[ch] = sample_for(seg_pos, hz, step)

        for sample in samples:
            sample = max(-1.0, min(1.0, sample))
            value = int(round(sample * 32767))
            pcm.extend(struct.pack("<h", value))

    subformat_pcm = bytes.fromhex("0100000000001000800000AA00389B71")
    fmt_payload = struct.pack(
        "<HHIIHHHHI16s",
        0xFFFE,
        channels,
        SR,
        byte_rate,
        block_align,
        bits,
        22,
        bits,
        channel_mask,
        subformat_pcm,
    )
    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_payload)) + fmt_payload
    data_chunk = b"data" + struct.pack("<I", len(pcm)) + pcm
    riff_size = 4 + len(fmt_chunk) + len(data_chunk)
    riff = b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + fmt_chunk + data_chunk

    path.write_bytes(riff)


def default_ec3_xml(input_wav: Path, output_ec3: Path, temp_dir: Path) -> str:
    """Default XML job config for E-AC-3 (Dolby Digital Plus)."""
    return f"""<?xml version="1.0"?>
<job_config>
  <input>
    <audio>
      <wav version="1">
        <file_name>{input_wav.name}</file_name>
        <timecode_frame_rate>not_indicated</timecode_frame_rate>
        <offset>auto</offset>
        <ffoa>auto</ffoa>
        <storage><local><path>{input_wav.parent.as_posix()}</path></local></storage>
      </wav>
    </audio>
  </input>
  <filter>
    <audio>
      <encode_to_atmos_ddp version="1">
        <loudness>
          <measure_only>
            <metering_mode>1770-4</metering_mode>
            <dialogue_intelligence>true</dialogue_intelligence>
            <speech_threshold>15</speech_threshold>
          </measure_only>
        </loudness>
        <data_rate>448</data_rate>
        <timecode_frame_rate>not_indicated</timecode_frame_rate>
        <start>first_frame_of_action</start>
        <end>end_of_file</end>
        <time_base>file_position</time_base>
        <prepend_silence_duration>0.0</prepend_silence_duration>
        <append_silence_duration>0.0</append_silence_duration>
        <drc>
          <line_mode_drc_profile>film_light</line_mode_drc_profile>
          <rf_mode_drc_profile>film_light</rf_mode_drc_profile>
        </drc>
        <downmix>
          <loro_center_mix_level>-3</loro_center_mix_level>
          <loro_surround_mix_level>-3</loro_surround_mix_level>
          <ltrt_center_mix_level>-3</ltrt_center_mix_level>
          <ltrt_surround_mix_level>-3</ltrt_surround_mix_level>
          <preferred_downmix_mode>ltrt</preferred_downmix_mode>
        </downmix>
        <custom_trims>
          <surround_trim_5_1>auto</surround_trim_5_1>
          <height_trim_5_1>auto</height_trim_5_1>
        </custom_trims>
      </encode_to_atmos_ddp>
    </audio>
  </filter>
  <output>
    <ec3 version="1">
      <file_name>{output_ec3.name}</file_name>
      <storage><local><path>{output_ec3.parent.as_posix()}</path></local></storage>
    </ec3>
  </output>
  <misc>
    <temp_dir>
      <clean_temp>true</clean_temp>
      <path>{temp_dir.as_posix()}</path>
    </temp_dir>
  </misc>
</job_config>
"""


def build_xml_from_template(template_path: Path, input_wav: Path, output_ec3: Path, temp_dir: Path) -> Path:
    xml = ET.parse(template_path)
    root = xml.getroot()

    def set_text(xpath: str, value: str) -> None:
        e = root.find(xpath)
        if e is not None:
            e.text = value

    set_text(".//input//wav/file_name", input_wav.name)
    set_text(".//input//wav//storage/local/path", input_wav.parent.as_posix())
    set_text(".//encode_to_atmos_ddp/downmix/preferred_downmix_mode", "ltrt")
    set_text(".//output//ec3/file_name", output_ec3.name)
    set_text(".//output//ec3//storage/local/path", output_ec3.parent.as_posix())
    set_text(".//misc//temp_dir/path", temp_dir.as_posix())

    out = temp_dir / "dee_job.xml"
    xml.write(out, encoding="utf-8", xml_declaration=True)
    return out


def make_xml(template_hint: Path | None, input_wav: Path, output_ec3: Path, temp_dir: Path) -> Path:
    if template_hint and template_hint.is_file():
        print(f"Using XML template: {template_hint}")
        return build_xml_from_template(template_hint, input_wav, output_ec3, temp_dir)

    auto = Path.cwd() / "wav_encode_to_atmos_ddp_ec3.xml"
    if auto.is_file():
        print(f"Using XML template: {auto}")
        return build_xml_from_template(auto, input_wav, output_ec3, temp_dir)

    out = temp_dir / "dee_job.xml"
    out.write_text(default_ec3_xml(input_wav, output_ec3, temp_dir), encoding="utf-8")
    print("No XML template found; generated equivalent E-AC-3 XML.")
    return out


def decode_stereo(ffmpeg: str, ec3: Path, out_wav: Path, use_dplii_matrix: bool = False) -> None:
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", str(ec3),
    ]
    if use_dplii_matrix:
        cmd.extend(["-af", "aresample=matrix_encoding=dplii"])
    
    cmd.extend([
        "-map", "0:a:0",
        "-ar", str(SR),
        "-ac", "2",
        "-c:a", "pcm_s16le",
        "-y", str(out_wav),
    ])
    run(cmd)


def read_stereo(path: Path) -> tuple[list[float], list[float]]:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 2 or w.getsampwidth() != 2 or w.getframerate() != SR:
            die("Decoded WAV format is not 48 kHz / 16-bit / stereo")
        raw = w.readframes(w.getnframes())

    vals = struct.unpack("<" + "h" * (len(raw) // 2), raw)
    left = [v / 32768.0 for v in vals[0::2]]
    right = [v / 32768.0 for v in vals[1::2]]
    return left, right


def dft_tone(x: list[float], start: int, length: int, hz: float) -> complex:
    start = max(0, start)
    end = min(len(x), start + length)
    n = end - start
    if n < SR // 4:
        return 0j

    re, im, wsum = 0.0, 0.0, 0.0
    for i in range(n):
        win = 0.5 - 0.5 * math.cos(2.0 * math.pi * i / (n - 1))
        a = 2.0 * math.pi * hz * i / SR
        s = x[start + i] * win
        re += s * math.cos(a)
        im -= s * math.sin(a)
        wsum += win

    scale = 2.0 / wsum
    return complex(re * scale, im * scale)


def mag_db(z: complex, ref: float) -> float:
    if ref <= 1e-12 or abs(z) <= 1e-12:
        return -120.0
    return 20.0 * math.log10(abs(z) / ref)


def phase_deg(z: complex) -> float:
    return math.degrees(math.atan2(z.imag, z.real))


def wrap_deg(x: float) -> float:
    while x <= -180.0:
        x += 360.0
    while x > 180.0:
        x -= 360.0
    return x


def segment_start_frames(index: int) -> int:
    return int((LEAD_S + index * SEG_S) * SR)


def measure(left: list[float], right: list[float], index: int, hz: float) -> tuple[complex, complex]:
    start = segment_start_frames(index) + int(0.50 * SR)
    length = int(1.00 * SR)
    return dft_tone(left, start, length, hz), dft_tone(right, start, length, hz)


def classify(rows: dict[str, tuple[complex, complex]]) -> tuple[str, float, dict[str, float]]:
    ref = max(abs(rows["FL"][0]), 1e-12)

    def db(ch: str, side: int) -> float:
        return mag_db(rows[ch][side], ref)

    candidates = {
        "Lo/Ro": [
            (db("Ls", 0), -3.0), (db("Ls", 1), -60.0),
            (db("Rs", 0), -60.0), (db("Rs", 1), -3.0),
            (db("C", 0), -3.0), (db("C", 1), -3.0),
        ],
        "Dolby Surround / legacy Pro Logic": [
            (db("Ls", 0), -3.0), (db("Ls", 1), -3.0),
            (db("Rs", 0), -3.0), (db("Rs", 1), -3.0),
            (db("C", 0), -3.0), (db("C", 1), -3.0),
        ],
        "Dolby Pro Logic II": [
            (db("Ls", 0), -1.2), (db("Ls", 1), -6.2),
            (db("Rs", 0), -6.2), (db("Rs", 1), -1.2),
            (db("C", 0), -3.0), (db("C", 1), -3.0),
        ],
    }

    scores: dict[str, float] = {}
    for name, pairs in candidates.items():
        err = 0.0
        for measured, expected in pairs:
            if expected <= -50:
                err += max(0.0, measured + 50.0) ** 2
            else:
                err += (measured - expected) ** 2
        scores[name] = math.sqrt(err / len(pairs))

    best = min(scores, key=scores.get)
    return best, scores[best], scores


def print_results(rows: dict[str, tuple[complex, complex]], title: str) -> None:
    ref = max(abs(rows["FL"][0]), 1e-12)
    ref_phase = phase_deg(rows["FL"][0])

    print("\n" + "=" * 82)
    print(f"E-AC-3 MATRIX TEST: {title}")
    print("=" * 82)
    print(f"Reference: FL -> Lt = 0 dB, common encoder-delay phase = {ref_phase:+.2f} deg\n")
    print(f"{'Input':<5} {'Lt dB':>9} {'Rt dB':>9} {'Lt phase':>11} {'Rt phase':>11} {'Lt-Rt':>9}")
    print("-" * 82)

    for ch in ("FL", "FR", "C", "LFE", "Ls", "Rs"):
        l, r = rows[ch]
        ldb = mag_db(l, ref)
        rdb = mag_db(r, ref)
        lp = wrap_deg(phase_deg(l) - ref_phase)
        rp = wrap_deg(phase_deg(r) - ref_phase)
        lr = wrap_deg(phase_deg(l) - phase_deg(r)) if abs(l) > 1e-10 and abs(r) > 1e-10 else float("nan")
        lr_s = f"{lr:+.2f}" if math.isfinite(lr) else "n/a"
        print(f"{ch:<5} {ldb:>9.2f} {rdb:>9.2f} {lp:>11.2f} {rp:>11.2f} {lr_s:>9}")

    best, score, scores = classify(rows)
    print(f"\nClassification: -> {best} (fit error: {score:.2f} dB RMS)")


def main() -> None:
    ffmpeg = find_tool("ffmpeg")
    dee = find_tool("dee")

    root = Path("C:/dee_task/eac3_matrix_test")
    root.mkdir(parents=True, exist_ok=True)

    wav_in = root / "test_5.1_source.wav"
    ec3_out = root / "test_5.1_to_LtRt.ec3"
    wav_dec_default = root / "test_decoded_default.wav"
    wav_dec_dplii = root / "test_decoded_dplii.wav"

    template = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else None

    for p in (wav_in, ec3_out, wav_dec_default, wav_dec_dplii):
        if p.exists():
            p.unlink()

    print("Generating 5.1 test WAV...")
    write_pcm_5_1(wav_in)

    with tempfile.TemporaryDirectory(prefix="eac3_dee_", dir=str(root)) as td:
        temp_dir = Path(td)
        job_xml = make_xml(template, wav_in.resolve(), ec3_out.resolve(), temp_dir)

        print("\nEncoding to E-AC-3 (Dolby Digital Plus) with DEE...")
        run([dee, "--xml", str(job_xml), "--progress", "--stdout"])

    if not ec3_out.exists():
        die(f"DEE finished but EC3 output was not created: {ec3_out}")

    # Pass 1: Default FFmpeg decode
    print("\n[Pass 1] Decoding EC3 with Default FFmpeg Downmix...")
    decode_stereo(ffmpeg, ec3_out, wav_dec_default, use_dplii_matrix=False)
    l1, r1 = read_stereo(wav_dec_default)
    rows1 = {label: measure(l1, r1, i, hz) for i, (label, _, hz) in enumerate(SEGMENTS)}
    print_results(rows1, "FFmpeg Default Decoder")

    # Pass 2: Matrix DPLII FFmpeg decode
    print("\n[Pass 2] Decoding EC3 with Matrix DPLII Resampler Filter...")
    decode_stereo(ffmpeg, ec3_out, wav_dec_dplii, use_dplii_matrix=True)
    l2, r2 = read_stereo(wav_dec_dplii)
    rows2 = {label: measure(l2, r2, i, hz) for i, (label, _, hz) in enumerate(SEGMENTS)}
    print_results(rows2, "FFmpeg with DPLII Matrix Filter")


if __name__ == "__main__":
    main()