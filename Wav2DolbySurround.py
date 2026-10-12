#!/usr/bin/env python3
# pipeline_to_ac3_ec3_ltrt.py  (v2)
#
# Any audio -> [block-by-block] layout detect -> canonical 5.1 -> Matrix Lt/Rt -> LPF
#           -> Float32 2.0 WAV (เขียนทีละ block) -> Dolby DEE -> AC-3 หรือ E-AC-3 (ec3/eac3)
#
# Layout:  2.0 (bypass) | 3.0 | 4.0q (L R Ls Rs) | 4.0c (L R C S) | 5.0 | 5.1 | 7.1 (-> 5.1 -> 2.0)
# Mode:    dpl1 | dpl2 | dpl2-no90 | dolby-surround
#
# พารามิเตอร์ของ DEE (ตาม manifest) ใส่ได้ด้วย --set ชื่อ=ค่า  เช่น
#   --set dialnorm=-27 --set ltrt_center_mix_level=-3 --fps 24
# หมายเหตุ: ค่า surround/downmix/dolby_surround_* ใน DEE เป็น metadata ที่ถูก "ฝัง" ลง bitstream เท่านั้น
#          การประมวลผล matrix จริงทำในสคริปต์นี้ (Stage 2)

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import numpy as np
    from scipy.signal import butter, oaconvolve, sosfiltfilt, windows
except ImportError:
    print("ERROR: กรุณาติดตั้ง numpy และ scipy: pip install numpy scipy", file=sys.stderr)
    sys.exit(1)

SR = 48000


def db(x: float) -> float:
    return float(10 ** (x / 20.0))


G_CENTER = db(-3.0)
G_SURR = db(-3.0)
G_MAJOR = db(-1.2)
G_MINOR = db(-6.2)
G_BACK_TO_SIDE = db(-3.0)   # 7.1 -> 5.1
G_S_TO_SURR = db(-3.0)      # 4.0c: S -> Ls/Rs

MODES = ("dpl1", "dpl2", "dpl2-no90", "dolby-surround")
LAYOUTS = {
    "2.0":  ["L", "R"],
    "3.0":  ["L", "R", "C"],
    "4.0q": ["L", "R", "Ls", "Rs"],
    "4.0c": ["L", "R", "C", "S"],
    "5.0":  ["L", "R", "C", "Ls", "Rs"],
    "5.1":  ["L", "R", "C", "LFE", "Ls", "Rs"],
    "7.1":  ["L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs"],
}
MASK_TO_LAYOUT = {
    0x003: "2.0", 0x007: "3.0",
    0x033: "4.0q", 0x603: "4.0q", 0x107: "4.0c",
    0x037: "5.0", 0x607: "5.0",
    0x03F: "5.1", 0x60F: "5.1",
    0x63F: "7.1",
}
COUNT_TO_LAYOUT = {2: "2.0", 3: "3.0", 4: "4.0q", 5: "5.0", 6: "5.1", 8: "7.1"}


def die(msg: str, rc: int = 1):
    print(f"\n[ERROR] {msg}", file=sys.stderr)
    sys.exit(rc)


def find_tool(name: str) -> str:
    p = shutil.which(name) or (shutil.which(name + ".exe") if os.name == "nt" else None)
    if not p:
        die(f"ไม่พบเครื่องมือ '{name}' ใน PATH")
    return p


def run_cmd(cmd: list) -> None:
    shown = subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(cmd)
    print("\n>", shown)
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        die(f"คำสั่งล้มเหลว (exit code {e.returncode}): {Path(cmd[0]).name}", e.returncode)


# ====================================================================== WAV streaming I/O
class WavError(Exception):
    pass


class WavStream:
    """อ่าน WAV ทีละ block จากไฟล์หรือ pipe (ffmpeg) รองรับ PCM 8/16/24/32, float 32/64, extensible, RF64"""

    def __init__(self, f):
        self.f, self.proc = f, None
        head = self._readexact(12)
        if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            raise WavError("ไม่ใช่ไฟล์ WAV")
        fmt, ds64 = None, None
        while True:
            hdr = self._readexact(8)
            if len(hdr) < 8:
                raise WavError("ไม่พบ data chunk")
            cid, size = struct.unpack("<4sI", hdr)
            if cid == b"data":
                if fmt is None:
                    raise WavError("data มาก่อน fmt")
                self.remaining = (ds64 if size in (0, 0xFFFFFFFF) else size) or None
                break
            body = self._readexact(size + (size & 1))
            if cid == b"fmt ":
                fmt = body
            elif cid == b"ds64" and len(body) >= 16:
                ds64 = struct.unpack("<Q", body[8:16])[0]
        tag, nch, sr, _, _, bits = struct.unpack("<HHIIHH", fmt[:16])
        self.mask = None
        if tag == 0xFFFE and len(fmt) >= 40:
            self.mask = struct.unpack("<I", fmt[20:24])[0]
            tag = struct.unpack("<H", fmt[24:26])[0]
        ok = (tag == 1 and bits in (8, 16, 24, 32)) or (tag == 3 and bits in (32, 64))
        if not ok:
            raise WavError(f"รูปแบบ WAV ไม่รองรับ (tag={tag}, bits={bits})")
        self.tag, self.bits, self.nch, self.sr = tag, bits, nch, sr
        self.fb = nch * (bits // 8)
        self.total_frames = None if self.remaining is None else self.remaining // self.fb

    def _readexact(self, n: int) -> bytes:
        parts, got = [], 0
        while got < n:
            b = self.f.read(n - got)
            if not b:
                break
            parts.append(b)
            got += len(b)
        return b"".join(parts)

    def read(self, n: int) -> np.ndarray:
        want = n * self.fb
        if self.remaining is not None:
            want = min(want, self.remaining)
        raw = self._readexact(want) if want > 0 else b""
        if self.remaining is not None:
            self.remaining -= len(raw)
        raw = raw[: len(raw) // self.fb * self.fb]
        if not raw:
            return np.zeros((0, self.nch), np.float32)
        if self.tag == 3:
            a = np.frombuffer(raw, "<f4" if self.bits == 32 else "<f8").astype(np.float32)
        elif self.bits == 16:
            a = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
        elif self.bits == 24:
            b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
            v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
            a = (((v << 8) >> 8).astype(np.float32)) / 8388608.0
        elif self.bits == 32:
            a = (np.frombuffer(raw, "<i4").astype(np.float64) / 2147483648.0).astype(np.float32)
        else:
            a = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128.0) / 128.0
        return a.reshape(-1, self.nch)

    def close(self):
        try:
            self.f.close()
        finally:
            if self.proc is not None:
                rc = self.proc.wait()
                if rc != 0:
                    die(f"FFmpeg ล้มเหลว (exit code {rc})", rc)


class WavWriterF32:
    """เขียน Float32 WAV แบบต่อท้ายทีละ block แล้วย้อนมาเติม header ตอนปิดไฟล์"""

    def __init__(self, path: Path, sr: int, nch: int):
        self.f, self.sr, self.nch, self.n = open(path, "wb"), sr, nch, 0
        self.f.write(b"\0" * 44)

    def write(self, a: np.ndarray):
        b = np.ascontiguousarray(a, dtype="<f4").tobytes()
        self.f.write(b)
        self.n += len(b)

    def close(self):
        if self.n > 0xFFFFFFFF - 64:
            die("ไฟล์ WAV Lt/Rt เกิน 4 GB (ยังไม่รองรับ RF64 ตอนเขียน)")
        hdr = (b"RIFF" + struct.pack("<I", 36 + self.n) + b"WAVEfmt " +
               struct.pack("<IHHIIHH", 16, 3, self.nch, self.sr, self.sr * self.nch * 4, self.nch * 4, 32) +
               b"data" + struct.pack("<I", self.n))
        self.f.seek(0)
        self.f.write(hdr)
        self.f.close()


def open_source(input_file: Path, ffmpeg_getter) -> WavStream:
    if input_file.suffix.lower() == ".wav":
        f = open(input_file, "rb")
        try:
            s = WavStream(f)
            if s.sr == SR:
                print(f"[STAGE 1] อ่าน WAV ตรงแบบ stream: {input_file.name} ({s.nch} ch, {s.bits}-bit)")
                return s
            print(f"[STAGE 1] sample rate {s.sr} Hz -> ใช้ FFmpeg resample เป็น 48 kHz")
        except WavError as e:
            print(f"[STAGE 1] อ่านตรงไม่ได้ ({e}) -> ใช้ FFmpeg")
        f.close()
    else:
        print("[STAGE 1] ถอดรหัสด้วย FFmpeg (pipe, Float32 48 kHz, คงจำนวนช่องเดิม)")
    cmd = [ffmpeg_getter(), "-hide_banner", "-loglevel", "error", "-i", str(input_file),
           "-map", "0:a:0", "-ar", str(SR), "-c:a", "pcm_f32le", "-f", "wav", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    try:
        s = WavStream(proc.stdout)
    except WavError as e:
        proc.kill()
        die(f"อ่านเอาต์พุตของ FFmpeg ไม่ได้: {e}")
    s.proc = proc
    return s


def detect_layout(nch: int, mask, override: str) -> str:
    if override != "auto":
        if len(LAYOUTS[override]) != nch:
            die(f"--layout {override} ต้องมี {len(LAYOUTS[override])} ช่อง แต่ไฟล์มี {nch} ช่อง")
        return override
    if mask in MASK_TO_LAYOUT and len(LAYOUTS[MASK_TO_LAYOUT[mask]]) == nch:
        return MASK_TO_LAYOUT[mask]
    if nch in COUNT_TO_LAYOUT:
        if nch == 4:
            print("[WARN] ไฟล์ 4 ช่องไม่มี channel mask -> สมมติ L R Ls Rs (quad) "
                  "ถ้าเป็น L R C S ให้ใช้ --layout 4.0c")
        return COUNT_TO_LAYOUT[nch]
    die(f"ไม่รองรับไฟล์ {nch} ช่อง (mask={mask}) ลองระบุ --layout")


# ====================================================================== DSP (ทำงานต่อ window)
_H = None


def shift90(x: np.ndarray) -> np.ndarray:
    """FIR Hilbert 2047 taps, mode='same' (ไม่มี delay) — ความเพี้ยนที่ขอบ window ถูกกันด้วย context"""
    global _H
    if _H is None:
        taps = 2047
        n = np.arange(-(taps // 2), taps // 2 + 1)
        h = np.zeros(taps)
        odd = (n % 2) != 0
        h[odd] = 2.0 / (np.pi * n[odd])
        _H = (h * windows.kaiser(taps, 8.0)).astype(np.float32)
    return oaconvolve(x, _H, mode="same").astype(np.float32)


def zp(x: np.ndarray, sos) -> np.ndarray:
    return sosfiltfilt(sos, x, axis=0).astype(np.float32)


def to_canonical(data: np.ndarray, layout: str) -> dict:
    ch = {n: np.ascontiguousarray(data[:, i]) for i, n in enumerate(LAYOUTS[layout])}
    if layout == "7.1":
        g = np.float32(G_BACK_TO_SIDE)
        ch["Ls"] = ch["Ls"] + g * ch.pop("Lb")
        ch["Rs"] = ch["Rs"] + g * ch.pop("Rb")
    elif layout == "4.0c":
        s = ch.pop("S") * np.float32(G_S_TO_SURR)
        ch["Ls"], ch["Rs"] = s, s.copy()
    return ch


def matrix_encode(ch: dict, mode: str, bandlimit_s: bool):
    gc, gs = np.float32(G_CENTER), np.float32(G_SURR)
    lt, rt = ch["L"].astype(np.float32).copy(), ch["R"].astype(np.float32).copy()
    if "C" in ch:
        lt += gc * ch["C"]
        rt += gc * ch["C"]
    if "Ls" not in ch:
        return lt, rt
    ls, rs = ch["Ls"], ch["Rs"]
    if mode in ("dpl1", "dolby-surround"):
        s = ls + rs
        if mode == "dpl1" and bandlimit_s:
            s = zp(s, butter(4, [100.0, 7000.0], btype="band", fs=SR, output="sos"))
        s90 = shift90(s)
        lt -= gs * s90
        rt += gs * s90
    else:
        a, b = np.float32(G_MAJOR), np.float32(G_MINOR)
        if mode == "dpl2":
            ls, rs = shift90(ls), shift90(rs)
        lt += -a * ls - b * rs
        rt += b * ls + a * rs
    return lt, rt


class Dsp:
    def __init__(self, layout, args):
        self.layout, self.args = layout, args
        self.matrixed = layout != "2.0"
        self.has_surr = layout in ("4.0q", "4.0c", "5.0", "5.1", "7.1")
        self.sos_lfe = butter(4, 120.0, btype="low", fs=SR, output="sos")
        self.sos_lpf = butter(8, args.lpf, btype="low", fs=SR, output="sos") if args.lpf > 0 else None

    def describe(self):
        a = self.args
        print(f"         Layout: {self.layout} ({' '.join(LAYOUTS[self.layout])})")
        if self.layout == "2.0":
            print("         2.0 -> ไม่ทำ Matrix Encode (bypass)")
        else:
            print(f"         Matrix Encode: {a.mode}")
        if self.layout == "7.1":
            print(f"         7.1 -> 5.1: Ls/Rs = side + back x {G_BACK_TO_SIDE:.4f}")
        if self.layout in ("5.1", "7.1"):
            print(f"         LFE: {'mix ' + format(a.lfe_level, '+.1f') + ' dB (LPF 120 Hz)' if a.lfe == 'mix' else 'drop'}")
        print(f"         LPF: {a.lpf:.0f} Hz zero-phase" if a.lpf > 0 else "         LPF: off")

    def run(self, win: np.ndarray) -> np.ndarray:
        a = self.args
        ch = to_canonical(win, self.layout)
        if self.matrixed:
            lt, rt = matrix_encode(ch, a.mode, not a.no_s_bandlimit)
        else:
            lt, rt = ch["L"].copy(), ch["R"].copy()
        if "LFE" in ch and a.lfe == "mix":
            lfe = zp(ch["LFE"], self.sos_lfe) * np.float32(db(a.lfe_level))
            lt += lfe
            rt += lfe
        st = np.column_stack((lt, rt)).astype(np.float32)
        if self.sos_lpf is not None:
            st = zp(st, self.sos_lpf)
        return st


def stream_downmix(src: WavStream, dsp: Dsp, wav_out: Path, block: int, ctx: int) -> float:
    """อ่านทีละ block (+context ซ้าย/ขวา) -> Lt/Rt -> เขียนต่อท้าย WAV คืนค่า peak"""
    w = WavWriterF32(wav_out, SR, 2)
    buf = np.zeros((ctx, src.nch), np.float32)   # context ซ้ายของ block แรก = ศูนย์
    eof, peak, done, nblk = False, 0.0, 0, 0
    need = ctx + block + ctx
    while True:
        while buf.shape[0] < need and not eof:
            blk = src.read(need - buf.shape[0])
            if len(blk) == 0:
                eof = True
            else:
                buf = np.concatenate((buf, blk))
        n_out = min(block, buf.shape[0] - ctx)
        if n_out <= 0:
            break
        win = buf[: ctx + n_out + ctx]
        if win.shape[0] < ctx + n_out + ctx:
            win = np.concatenate((win, np.zeros((ctx + n_out + ctx - win.shape[0], src.nch), np.float32)))
        out = dsp.run(win)[ctx: ctx + n_out]
        peak = max(peak, float(np.max(np.abs(out))))
        w.write(out)
        buf = buf[n_out:]
        done += n_out
        nblk += 1
        if nblk % 8 == 0 or eof:
            tot = f"/{src.total_frames / SR:.0f}s" if src.total_frames else ""
            print(f"\r         block {nblk}: {done / SR:.0f}s{tot}", end="", flush=True)
    print()
    w.close()
    return peak


def rescale_wav(src_path: Path, dst_path: Path, gain: float, block: int):
    s = WavStream(open(src_path, "rb"))
    w = WavWriterF32(dst_path, SR, 2)
    g = np.float32(gain)
    while True:
        b = s.read(block)
        if len(b) == 0:
            break
        w.write(b * g)
    w.close()
    s.close()


# ====================================================================== DEE parameters (ตาม manifest)
FPS = ["not_indicated", "23.976", "24", "25", "29.97", "30", "48", "50", "59.94", "60"]
MIX_C = ["+3", "+1.5", "0", "-1.5", "-3", "-4.5", "-6", "-inf"]
MIX_S = ["-1.5", "-3", "-4.5", "-6", "-inf"]
DRC_AC3 = ["film_standard", "film_light", "music_standard", "music_light", "speech"]
YNN = ["yes", "no", "not_indicated"]
E = lambda l: ("enum", l)
BOOL = ("bool",)
STR = ("str",)

COMMON_SPEC = {
    "timecode_frame_rate": E(FPS), "offset": STR, "ffoa": STR,
    "start": STR, "end": STR, "time_base": E(["file_position", "embedded_timecode"]),
    "loro_center_mix_level": E(MIX_C), "loro_surround_mix_level": E(MIX_S),
    "ltrt_center_mix_level": E(MIX_C), "ltrt_surround_mix_level": E(MIX_S),
    "line_mode_drc_profile": E(DRC_AC3), "rf_mode_drc_profile": E(DRC_AC3),
}
SPEC = {
    "ac3": {**COMMON_SPEC,
            "threads": ("int", 0, 4),
            "bitstream_mode": E(["complete_main", "music_and_effects", "visually_impaired", "hearing_impaired",
                                 "dialogue", "commentary", "emergency", "voice_over"]),
            "acmod": E(["auto", "1_0", "2_0", "3_2"]),
            "data_rate": E(["auto", "96", "112", "128", "160", "192", "224", "256", "320", "384", "448", "512",
                            "576", "640"]),
            "lfe_on": BOOL, "dolby_surround_mode": E(YNN), "dolby_surround_ex_mode": E(YNN),
            "lfe_lowpass_filter": BOOL, "surround_90_degree_phase_shift": BOOL, "surround_3db_attenuation": BOOL,
            "preferred_downmix_mode": E(["loro", "ltrt", "not_indicated"]),
            # loudness
            "dialnorm": ("int", -31, -1),
            "preset": E(["manual", "atsc_a85", "atsc_a85_agile", "ebu_r128", "freetv_op59", "arib_b32"]),
            "correction_mode": E(["pcm_scale", "md_update"]),
            "loudness_target": ("int", -31, -8), "peak_limit_db": ("float", -12.0, -0.1),
            "peak_reference": E(["true_peak", "sample_peak"]), "measure_corrected": BOOL},
    "ec3": {**COMMON_SPEC,
            "line_mode_drc_profile": E(DRC_AC3 + ["none"]), "rf_mode_drc_profile": E(DRC_AC3 + ["none"]),
            "data_rate": ("multi", ["384", "448", "576", "640", "768", "1024"]),
            "prepend_silence_duration": STR, "append_silence_duration": STR,
            "preferred_downmix_mode": E(["loro", "ltrt", "ltrt-pl2", "not_indicated"]),
            "surround_trim_5_1": E(["0", "-3", "-6", "-9", "auto"]),
            "height_trim_5_1": E(["-3", "-6", "-9", "-12", "auto"]),
            "metering_mode": E(["1770-4", "1770-3", "1770-2", "1770-1", "LeqA"]),
            "dialogue_intelligence": BOOL, "speech_threshold": ("int", 0, 100)},
}
LOUD_KEYS = {
    "disabled": {"dialnorm"},
    "measure_and_correct": {"preset", "correction_mode", "loudness_target", "peak_limit_db",
                            "peak_reference", "measure_corrected"},
    "measure_only": {"metering_mode", "dialogue_intelligence", "speech_threshold"},
}
FMT_LOUD = {"ac3": ("disabled", "measure_and_correct"), "ec3": ("measure_only",)}
MANUAL_ONLY = {"correction_mode", "loudness_target", "peak_limit_db", "peak_reference"}


def canon_enum(val: str, options: list):
    if val in options:
        return val
    try:
        f = float(val)
        for o in options:
            try:
                if float(o) == f:
                    return o
            except ValueError:
                pass
    except ValueError:
        pass
    return None


def validate(fmt: str, key: str, val: str) -> str:
    spec = SPEC[fmt].get(key)
    if spec is None:
        die(f"พารามิเตอร์ '{key}' ใช้ไม่ได้กับ {fmt}\nที่ใช้ได้: {', '.join(sorted(SPEC[fmt]))}")
    kind = spec[0]
    if kind == "enum":
        r = canon_enum(val, spec[1])
        if r is None:
            die(f"{key}={val} ไม่ถูกต้อง ค่าที่ใช้ได้: {', '.join(spec[1])}")
        return r
    if kind == "multi":
        out = []
        for v in val.split(","):
            r = canon_enum(v.strip(), spec[1])
            if r is None:
                die(f"{key}={v} ไม่ถูกต้อง ค่าที่ใช้ได้: {', '.join(spec[1])}")
            out.append(r)
        return ",".join(out)
    if kind in ("int", "float"):
        try:
            x = int(val) if kind == "int" else float(val)
        except ValueError:
            die(f"{key}={val} ต้องเป็น{'จำนวนเต็ม' if kind == 'int' else 'ตัวเลข'}")
        if not spec[1] <= x <= spec[2]:
            die(f"{key}={val} อยู่นอกช่วง {spec[1]} ถึง {spec[2]}")
        return str(x)
    if kind == "bool":
        if val.lower() not in ("true", "false"):
            die(f"{key}={val} ต้องเป็น true หรือ false")
        return val.lower()
    return val


def default_params(fmt: str, dsp: Dsp, mode: str) -> dict:
    ltrt = dsp.matrixed
    d = dict(timecode_frame_rate="not_indicated", offset="auto", ffoa="auto",
             start="first_frame_of_action", end="end_of_file", time_base="file_position",
             loro_center_mix_level="-3", loro_surround_mix_level="-3",
             ltrt_center_mix_level="-3", ltrt_surround_mix_level="-3",
             line_mode_drc_profile="film_light", rf_mode_drc_profile="film_light")
    if fmt == "ac3":
        d.update(threads="4", bitstream_mode="complete_main", acmod="2_0", data_rate="192", lfe_on="false",
                 dolby_surround_mode="yes" if (dsp.matrixed and dsp.has_surr) else "not_indicated",
                 dolby_surround_ex_mode="no", lfe_lowpass_filter="true",
                 surround_90_degree_phase_shift="true" if (dsp.matrixed and dsp.has_surr and mode != "dpl2-no90") else "false",
                 surround_3db_attenuation="false",
                 preferred_downmix_mode="ltrt" if ltrt else "not_indicated",
                 dialnorm="-27", preset="atsc_a85", correction_mode="pcm_scale", loudness_target="-24",
                 peak_limit_db="-2", peak_reference="true_peak")
    else:
        d.update(data_rate="448", prepend_silence_duration="0.0", append_silence_duration="0.0",
                 preferred_downmix_mode=("ltrt-pl2" if mode.startswith("dpl2") else "ltrt") if ltrt else "not_indicated",
                 surround_trim_5_1="auto", height_trim_5_1="auto",
                 metering_mode="1770-4", dialogue_intelligence="true", speech_threshold="15")
    return d


def resolve_loudness(fmt: str, user: dict, requested):
    """เลือกโหมด loudness (อนุมานจากคีย์ที่ผู้ใช้ตั้ง หากไม่ระบุ --loudness)"""
    inferred = {m for m, ks in LOUD_KEYS.items() if ks & set(user)}
    if requested:
        mode = requested
    elif len(inferred) > 1:
        die(f"ตั้งค่า loudness ปะปนหลายโหมด: {sorted(inferred)}")
    else:
        mode = next(iter(inferred)) if inferred else FMT_LOUD[fmt][0]
    if mode not in FMT_LOUD[fmt]:
        die(f"--loudness {mode} ใช้ไม่ได้กับ {fmt} (ใช้ได้: {', '.join(FMT_LOUD[fmt])})")
    for k in user:
        for m, ks in LOUD_KEYS.items():
            if k in ks and m != mode:
                die(f"'{k}' เป็นของ loudness โหมด {m} แต่โหมดที่ใช้คือ {mode}")
    return mode


def build_job_xml(fmt, P, loud_mode, user, wav: Path, out_file: Path, dee_tmp: Path) -> ET.ElementTree:
    def sub(p, tag, text=None, **at):
        e = ET.SubElement(p, tag, at)
        if text is not None:
            e.text = str(text)
        return e

    root = ET.Element("job_config")
    w = sub(sub(sub(root, "input"), "audio"), "wav", version="1")
    sub(w, "file_name", wav.name)
    sub(w, "timecode_frame_rate", P["timecode_frame_rate"])
    sub(w, "offset", P["offset"])
    sub(w, "ffoa", P["ffoa"])
    sub(sub(sub(w, "storage"), "local"), "path", wav.parent.as_posix())

    flt = sub(sub(root, "filter"), "audio")

    def loudness(parent):
        l = sub(parent, "loudness")
        if loud_mode == "disabled":
            sub(sub(l, "correction_disabled"), "dialnorm", P["dialnorm"])
        elif loud_mode == "measure_and_correct":
            m = sub(l, "measure_and_correct")
            sub(m, "preset", P["preset"])
            if P["preset"] == "manual":
                for k in ("correction_mode", "loudness_target", "peak_limit_db", "peak_reference"):
                    sub(m, k, P[k])
            if "measure_corrected" in user:
                sub(m, "measure_corrected", P["measure_corrected"])
        else:
            m = sub(l, "measure_only")
            for k in ("metering_mode", "dialogue_intelligence", "speech_threshold"):
                sub(m, k, P[k])

    def drc(parent):
        d = sub(parent, "drc")
        sub(d, "line_mode_drc_profile", P["line_mode_drc_profile"])
        sub(d, "rf_mode_drc_profile", P["rf_mode_drc_profile"])

    def downmix(parent):
        d = sub(parent, "downmix")
        for k in ("loro_center_mix_level", "loro_surround_mix_level", "ltrt_center_mix_level",
                  "ltrt_surround_mix_level", "preferred_downmix_mode"):
            sub(d, k, P[k])

    def timing(e):
        for k in ("timecode_frame_rate", "start", "end", "time_base"):
            sub(e, k, P[k])

    if fmt == "ac3":
        e = sub(flt, "encode_to_dd_single_pass", version="2")
        sub(e, "threads", P["threads"])
        timing(e)
        loudness(e)
        for k in ("bitstream_mode", "acmod", "data_rate", "lfe_on", "dolby_surround_mode", "dolby_surround_ex_mode"):
            sub(e, k, P[k])
        drc(e)
        for k in ("lfe_lowpass_filter", "surround_90_degree_phase_shift", "surround_3db_attenuation"):
            sub(e, k, P[k])
        downmix(e)
        out_tag = "ac3"
    else:
        e = sub(flt, "encode_to_atmos_ddp", version="1")
        loudness(e)
        for r in P["data_rate"].split(","):
            sub(e, "data_rate", r)
        timing(e)
        sub(e, "prepend_silence_duration", P["prepend_silence_duration"])
        sub(e, "append_silence_duration", P["append_silence_duration"])
        drc(e)
        downmix(e)
        t = sub(e, "custom_trims")
        sub(t, "surround_trim_5_1", P["surround_trim_5_1"])
        sub(t, "height_trim_5_1", P["height_trim_5_1"])
        out_tag = "ec3"

    o = sub(sub(root, "output"), out_tag, version="1")
    sub(o, "file_name", out_file.name)
    sub(sub(sub(o, "storage"), "local"), "path", out_file.parent.as_posix())
    t = sub(sub(root, "misc"), "temp_dir")
    sub(t, "clean_temp", "true")
    sub(t, "path", dee_tmp.as_posix())
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return tree


# ====================================================================== CLI
def parse_args():
    ap = argparse.ArgumentParser(
        description="Any audio -> Matrix Lt/Rt (block processing) -> AC-3 / E-AC-3 ด้วย Dolby DEE")
    ap.add_argument("input")
    ap.add_argument("output", help=".ac3 | .ec3 | .eac3 (หรือ .wav เมื่อใช้ --wav-only)")
    ap.add_argument("--format", choices=["auto", "ac3", "ec3"], default="auto",
                    help="auto = ดูจากนามสกุลไฟล์ output")
    ap.add_argument("--ec3-ext", choices=["ec3", "eac3"], default=None, help="บังคับนามสกุลไฟล์ E-AC-3")
    ap.add_argument("--mode", choices=MODES, default="dpl2")
    ap.add_argument("--layout", choices=["auto"] + list(LAYOUTS), default="auto")
    ap.add_argument("--lfe", choices=["drop", "mix"], default="drop")
    ap.add_argument("--lfe-level", type=float, default=-10.0)
    ap.add_argument("--lpf", type=float, default=18500.0, help="Low-pass Hz (0 = ปิด)")
    ap.add_argument("--no-s-bandlimit", action="store_true", help="DPL1: ปิด band-limit 100-7k ของ surround")
    ap.add_argument("--no-normalize", action="store_true", help="ไม่ลด gain เมื่อ peak เกิน 0 dBFS")
    ap.add_argument("--block-size", type=int, default=1 << 18, help="จำนวน frame ต่อ block (default 262144)")
    ap.add_argument("--context", type=int, default=1 << 14, help="context ซ้าย/ขวาต่อ block (default 16384)")
    # ทางลัดของพารามิเตอร์ DEE
    ap.add_argument("--bitrate", help="data_rate (kbps)")
    ap.add_argument("--dialnorm", help="dialnorm -31..-1 (loudness: correction_disabled)")
    ap.add_argument("--fps", help="timecode_frame_rate (ทั้ง input wav และ encoder)")
    ap.add_argument("--drc", help="ตั้ง line/rf DRC profile พร้อมกัน")
    ap.add_argument("--loudness", choices=["disabled", "measure_and_correct", "measure_only"],
                    help="โหมด loudness (ไม่ระบุ = อนุมานจาก --set / ค่าเริ่มต้นของ format)")
    ap.add_argument("--set", dest="sets", action="append", default=[], metavar="KEY=VALUE",
                    help="ตั้งพารามิเตอร์ตาม manifest ของ DEE (ใส่ซ้ำได้) เช่น --set ltrt_center_mix_level=-3")
    ap.add_argument("--save-xml", help="คัดลอก DEE job XML ไปเก็บไว้ที่ path นี้")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--wav-only", action="store_true", help="หยุดที่ Lt/Rt WAV ไม่เรียก DEE")
    a = ap.parse_args()
    if a.block_size < 4096 or a.context < 4096:
        ap.error("--block-size/--context ต้องมีค่าอย่างน้อย 4096")
    return a


def collect_user_params(a, fmt) -> dict:
    raw = {}
    for s in a.sets:
        if "=" not in s:
            die(f"--set ต้องอยู่ในรูป KEY=VALUE (ได้ '{s}')")
        k, v = s.split("=", 1)
        raw[k.strip()] = v.strip()
    if a.bitrate:
        raw["data_rate"] = a.bitrate
    if a.dialnorm:
        raw["dialnorm"] = a.dialnorm
    if a.fps:
        raw["timecode_frame_rate"] = a.fps
    if a.drc:
        raw["line_mode_drc_profile"] = raw["rf_mode_drc_profile"] = a.drc
    return {k: validate(fmt, k, v) for k, v in raw.items()}


def main() -> None:
    a = parse_args()
    input_file = Path(a.input).expanduser().resolve()
    output = Path(a.output).expanduser().resolve()
    if not input_file.exists():
        die(f"ไม่พบไฟล์อินพุต: {input_file}")

    fmt = a.format
    if fmt == "auto":
        suf = output.suffix.lower()
        fmt = "ac3" if suf == ".ac3" else "ec3" if suf in (".ec3", ".eac3") else None
        if a.wav_only:
            fmt = fmt or "ac3"
        if fmt is None:
            die("ระบุ --format ac3|ec3 หรือใช้นามสกุล .ac3 / .ec3 / .eac3")
    if fmt == "ec3" and a.ec3_ext:
        output = output.with_suffix("." + a.ec3_ext)
    output.parent.mkdir(parents=True, exist_ok=True)

    user = collect_user_params(a, fmt)
    loud_mode = resolve_loudness(fmt, user, a.loudness)
    manual_set = set(user) & MANUAL_ONLY
    if manual_set and loud_mode == "measure_and_correct" and user.get("preset", "atsc_a85") != "manual":
        die(f"{sorted(manual_set)} ใช้ได้เฉพาะ preset=manual")

    cache = []
    def ffmpeg():
        if not cache:
            cache.append(find_tool("ffmpeg"))
        return cache[0]

    dee = None if a.wav_only else find_tool("dee")
    work_root = Path(a.work_dir) if a.work_dir else Path(tempfile.gettempdir()) / "dee_task"
    work_root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ltrt_pipe_", dir=str(work_root)) as td:
        tmp = Path(td)
        src = open_source(input_file, ffmpeg)
        if src.nch < 2:
            die("ไม่รองรับไฟล์ mono")
        layout = detect_layout(src.nch, src.mask, a.layout)
        dsp = Dsp(layout, a)
        print("[STAGE 2] Downmix/Matrix แบบ block")
        dsp.describe()

        raw_wav = tmp / "interm_20_raw.wav"
        peak = stream_downmix(src, dsp, raw_wav, a.block_size, a.context)
        src.close()

        wav_20 = raw_wav
        if peak > 1.0 and not a.no_normalize:
            g = 1.0 / peak
            print(f"         Peak +{20 * np.log10(peak):.2f} dBFS -> static gain {20 * np.log10(g):.2f} dB (pass 2)")
            wav_20 = tmp / "interm_20_ltrt.wav"
            rescale_wav(raw_wav, wav_20, g, a.block_size)
            raw_wav.unlink()
        elif peak > 1.0:
            print(f"         [WARN] Peak +{20 * np.log10(peak):.2f} dBFS (ไม่ normalize)")

        if a.wav_only:
            shutil.copy2(wav_20, output)
            print(f"[DONE] {output}")
            return

        P = default_params(fmt, dsp, a.mode)
        P.update(user)
        dee_tmp = tmp / "dee_tmp"
        dee_tmp.mkdir()
        dee_out = tmp / f"dee_out.{fmt}"
        tree = build_job_xml(fmt, P, loud_mode, user, wav_20, dee_out, dee_tmp)
        job = tmp / "dee_job.xml"
        tree.write(job, encoding="utf-8", xml_declaration=True)
        if a.save_xml:
            shutil.copy2(job, a.save_xml)

        label = f"AC-3 {P['data_rate']} kbps" if fmt == "ac3" else f"E-AC-3 (Atmos DD+) {P['data_rate']} kbps"
        print(f"\n[STAGE 3] DEE -> {label} | loudness={loud_mode}")
        run_cmd([dee, "--xml", str(job), "--progress", "--stdout"])
        if not dee_out.exists():
            die(f"DEE ทำงานเสร็จแต่ไม่พบไฟล์ผลลัพธ์: {dee_out.name}")
        shutil.move(str(dee_out), str(output))

    print("\n" + "=" * 70)
    print(f"[COMPLETED] {input_file.name} [{layout}] -> {output.name} (mode={a.mode}, lfe={a.lfe})")
    print("=" * 70)


if __name__ == "__main__":
    main()