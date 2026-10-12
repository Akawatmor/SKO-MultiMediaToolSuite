#!/usr/bin/env python3
# test_ltrt_matrix.py
#
# One-command black-box test:
#   5.1 PCM WAV -> Dolby DEE AC-3 2.0 / 256 kbps / Lt-Rt
#   -> FFmpeg decodes the 2.0 AC-3 back to stereo PCM
#   -> measure channel coefficients + phase -> classify:
#        Lo/Ro
#        Dolby Surround / legacy Pro Logic
#        Dolby Pro Logic II
#
# Requirements in PATH:
#   python, ffmpeg, dee (or dee.exe)
#
# Usage:
#   python test_ltrt_matrix.py
#
# The script will also use wav_encode_to_dd_single_pass_ac3.xml from the
# current directory when present; otherwise it creates an equivalent XML job.

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
    channels = 6
    bits = 16
    bytes_per_sample = bits // 8
    block_align = channels * bytes_per_sample
    byte_rate = SR * block_align
    channel_mask = 0x003F  # Standard 5.1 mask (FL|FR|FC|LFE|BL|BR)

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

    channel_index = {
        "FL": 0,
        "FR": 1,
        "C": 2,
        "LFE": 3,
        "Ls": 4,
        "Rs": 5,
    }

    for frame in range(frames_total):
        samples = [0.0] * channels

        if lead <= frame < lead + len(SEGMENTS) * seg:
            rel = frame - lead
            seg_index = rel // seg
            seg_pos = rel % seg

            _, ch_name, hz = SEGMENTS[seg_index]
            ch = channel_index[ch_name]

            step = (
                phase_step_lfe
                if hz == 60.0
                else phase_step_1k
            )

            samples[ch] = sample_for(seg_pos, hz, step)

        for sample in samples:
            sample = max(-1.0, min(1.0, sample))
            value = int(round(sample * 32767))
            pcm.extend(struct.pack("<h", value))

    subformat_pcm = bytes.fromhex(
        "01000000"
        "0000"
        "1000"
        "8000"
        "00AA00389B71"
    )

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

    fmt_chunk = (
        b"fmt " +
        struct.pack("<I", len(fmt_payload)) +
        fmt_payload
    )

    data_chunk = (
        b"data" +
        struct.pack("<I", len(pcm)) +
        pcm
    )

    riff_size = 4 + len(fmt_chunk) + len(data_chunk)

    riff = (
        b"RIFF" +
        struct.pack("<I", riff_size) +
        b"WAVE" +
        fmt_chunk +
        data_chunk
    )

    path.write_bytes(riff)


def default_xml(input_wav: Path, output_ac3: Path, temp_dir: Path) -> str:
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
      <encode_to_dd_single_pass version="2">
        <threads>4</threads>
        <timecode_frame_rate>not_indicated</timecode_frame_rate>
        <start>first_frame_of_action</start>
        <end>end_of_file</end>
        <time_base>file_position</time_base>
        <loudness>
          <correction_disabled>
            <dialnorm>0</dialnorm>
          </correction_disabled>
        </loudness>
        <bitstream_mode>complete_main</bitstream_mode>
        <acmod>2_0</acmod>
        <data_rate>256</data_rate>
        <lfe_on>false</lfe_on>
        <dolby_surround_mode>yes</dolby_surround_mode>
        <dolby_surround_ex_mode>no</dolby_surround_ex_mode>
        <drc>
          <line_mode_drc_profile>film_light</line_mode_drc_profile>
          <rf_mode_drc_profile>film_light</rf_mode_drc_profile>
        </drc>
        <lfe_lowpass_filter>true</lfe_lowpass_filter>
        <surround_90_degree_phase_shift>true</surround_90_degree_phase_shift>
        <surround_3db_attenuation>true</surround_3db_attenuation>
        <downmix>
          <ltrt_center_mix_level>-3</ltrt_center_mix_level>
          <ltrt_surround_mix_level>-3</ltrt_surround_mix_level>
          <preferred_downmix_mode>ltrt</preferred_downmix_mode>
        </downmix>
      </encode_to_dd_single_pass>
    </audio>
  </filter>
  <output>
    <ac3 version="1">
      <file_name>{output_ac3.name}</file_name>
      <storage><local><path>{output_ac3.parent.as_posix()}</path></local></storage>
    </ac3>
  </output>
  <misc>
    <temp_dir>
      <clean_temp>true</clean_temp>
      <path>{temp_dir.as_posix()}</path>
    </temp_dir>
  </misc>
</job_config>
"""


def build_xml_from_template(template_path: Path, input_wav: Path, output_ac3: Path, temp_dir: Path) -> Path:
    xml = ET.parse(template_path)
    root = xml.getroot()

    def set_text(xpath: str, value: str) -> None:
        e = root.find(xpath)
        if e is None:
            die(f"Template is missing XML element: {xpath}")
        e.text = value

    set_text(".//input//wav/file_name", input_wav.name)
    set_text(".//input//wav//storage/local/path", input_wav.parent.as_posix())

    manifest = root.find(".//input//manifest")
    if manifest is not None:
        die(
            "The selected template requires a loudness manifest. "
            "Use DEE's wav_encode_to_dd_single_pass_ac3.xml template "
            "for this synthetic WAV-only test."
        )

    set_text(".//encode_to_dd_single_pass/acmod", "3_2")
    set_text(".//encode_to_dd_single_pass/data_rate", "256")
    set_text(".//encode_to_dd_single_pass/downmix/preferred_downmix_mode", "ltrt")
    loudness_parent = root.find(".//encode_to_dd_single_pass")
    if loudness_parent is not None:
        loudness = loudness_parent.find("loudness")
        if loudness is None:
            loudness = ET.SubElement(loudness_parent, "loudness")
        correction_disabled = loudness.find("correction_disabled")
        if correction_disabled is None:
            correction_disabled = ET.SubElement(loudness, "correction_disabled")
        dialnorm = correction_disabled.find("dialnorm")
        if dialnorm is None:
            dialnorm = ET.SubElement(correction_disabled, "dialnorm")
        dialnorm.text = "0"
    set_text(".//output//ac3/file_name", output_ac3.name)
    set_text(".//output//ac3//storage/local/path", output_ac3.parent.as_posix())
    set_text(".//misc//temp_dir/path", temp_dir.as_posix())

    p90 = root.find(".//encode_to_dd_single_pass/surround_90_degree_phase_shift")
    if p90 is not None:
        p90.text = "true"

    out = temp_dir / "dee_job.xml"
    xml.write(out, encoding="utf-8", xml_declaration=True)
    return out


def make_xml(template_hint: Path | None, input_wav: Path, output_ac3: Path, temp_dir: Path) -> Path:
    if template_hint and template_hint.is_file():
        print(f"Using XML template: {template_hint}")
        return build_xml_from_template(template_hint, input_wav, output_ac3, temp_dir)

    for auto in (
        Path.cwd() / "wav_encode_to_dd_single_pass_ac3.xml",
        Path.cwd()
        / "encode_to_dd_single_pass"
        / "wav_encode_to_dd_single_pass_ac3.xml",
    ):
        if auto.is_file():
            print(f"Using XML template: {auto}")
            return build_xml_from_template(auto, input_wav, output_ac3, temp_dir)

    out = temp_dir / "dee_job.xml"
    out.write_text(default_xml(input_wav, output_ac3, temp_dir), encoding="utf-8")
    print("No XML template found; generated an equivalent test XML.")
    return out


def decode_stereo(ffmpeg: str, ac3: Path, out_wav: Path) -> None:
    run([
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", str(ac3),
        "-map", "0:a:0",
        "-ar", str(SR),
        "-ac", "2",
        "-c:a", "pcm_s16le",
        "-y", str(out_wav),
    ])


def read_stereo(path: Path) -> tuple[list[float], list[float]]:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 2 or w.getsampwidth() != 2 or w.getframerate() != SR:
            die(
                f"Decoded WAV format is not 48 kHz / 16-bit / stereo: "
                f"{w.getframerate()} Hz, {w.getsampwidth()*8}-bit, {w.getnchannels()} ch"
            )
        raw = w.readframes(w.getnframes())

    vals = struct.unpack("<" + "h" * (len(raw) // 2), raw)
    left = [v / 32768.0 for v in vals[0::2]]
    right = [v / 32768.0 for v in vals[1::2]]
    return left, right


def dft_tone(x: list[float], start: int, length: int, hz: float) -> complex:
    """Hann-windowed complex projection at one exact tone frequency."""
    start = max(0, start)
    end = min(len(x), start + length)
    n = end - start
    if n < SR // 4:
        return 0j

    re = 0.0
    im = 0.0
    wsum = 0.0

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


def print_results(rows: dict[str, tuple[complex, complex]]) -> None:
    ref = max(abs(rows["FL"][0]), 1e-12)
    ref_phase = phase_deg(rows["FL"][0])

    print("\n" + "=" * 82)
    print("Lt/Rt MATRIX TEST")
    print("=" * 82)
    print(f"Reference: FL -> Lt = 0 dB, common encoder-delay phase = {ref_phase:+.2f} deg")
    print()
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

    print("\nClassification:")
    print(f"  -> {best}")
    print(f"  fit error: {score:.2f} dB RMS")

    print("\nCandidate fit (lower is better):")
    for name, s in sorted(scores.items(), key=lambda kv: kv[1]):
        print(f"  {name:<34} {s:.2f} dB")

    c_l, c_r = rows["C"]
    lfe_l, lfe_r = rows["LFE"]
    ls_l, _ = rows["Ls"]
    _, rs_r = rows["Rs"]

    c_sym = abs(mag_db(c_l, ref) - mag_db(c_r, ref))
    lfe_peak = max(mag_db(lfe_l, ref), mag_db(lfe_r, ref))
    ls_phase = wrap_deg(phase_deg(ls_l) - ref_phase)
    rs_phase = wrap_deg(phase_deg(rs_r) - ref_phase)

    print("\nSanity:")
    print(f"  Center Lt/Rt level mismatch : {c_sym:.2f} dB (near 0 is expected)")
    print(f"  LFE leakage peak            : {lfe_peak:.2f} dB (very low is expected)")
    print(f"  Surround phase vs FL        : Ls(Lt) {ls_phase:+.2f} deg, Rs(Rt) {rs_phase:+.2f} deg")
    print("=" * 82)


def main() -> None:
    ffmpeg = find_tool("ffmpeg")
    dee = find_tool("dee")

    # DEE 5.2.1 does not parse spaces in XML local-storage paths reliably.
    # Keep every generated input/output path under an ASCII, space-free root.
    root = Path("C:/dee_task/ltrt_matrix_test")
    root.mkdir(parents=True, exist_ok=True)

    wav_in = root / "test_5.1_source.wav"
    ac3_out = root / "test_5.1_to_LtRt_256k.ac3"
    wav_dec = root / "test_LtRt_decoded.wav"

    template = None
    if len(sys.argv) > 1:
        template = Path(sys.argv[1]).expanduser().resolve()
    else:
        dee_template = (
            Path(dee).parent
            / "xml_templates"
            / "encode_to_dd_single_pass"
            / "wav_encode_to_dd_single_pass_ac3.xml"
        )
        if dee_template.is_file():
            template = dee_template

    for p in (wav_in, ac3_out, wav_dec):
        if p.exists():
            p.unlink()

    print("Generating 5.1 test WAV...")
    write_pcm_5_1(wav_in)

    with tempfile.TemporaryDirectory(prefix="ltrt_dee_", dir=str(root)) as td:
        temp_dir = Path(td)
        job_xml = make_xml(template, wav_in.resolve(), ac3_out.resolve(), temp_dir)

        print("\nEncoding with Dolby DEE...")
        # XML-based DEE syntax used by older DEE releases.
        run([dee, "--xml", str(job_xml), "--progress", "--stdout"])

    if not ac3_out.exists():
        die(f"DEE finished but AC3 output was not created: {ac3_out}")

    print("\nDecoding AC-3 to 2.0 PCM with FFmpeg...")
    decode_stereo(ffmpeg, ac3_out, wav_dec)

    print("\nAnalyzing decoded Lt/Rt...")
    left, right = read_stereo(wav_dec)

    rows = {
        label: measure(left, right, i, hz)
        for i, (label, _, hz) in enumerate(SEGMENTS)
    }

    print_results(rows)

    print("\nFiles:")
    print(f"  5.1 source : {wav_in}")
    print(f"  AC-3 Lt/Rt : {ac3_out}")
    print(f"  decoded 2.0: {wav_dec}")


if __name__ == "__main__":
    main()