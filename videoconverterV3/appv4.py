#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VideoConV3 — แปลงวิดีโอเป็น MP4 พร้อม Dolby / AAC, Apple TX3G และตัวแก้ Chapters

จำเป็น : ffmpeg, ffprobe
ตามตัวเลือก : dee (Dolby), qaac64 (AAC), MP4Box (FastStart -inter), mediainfo (Dialnorm)
Helper : ddp_encode.py, 6to2matrixencoder.py, tx3g_apple.py (โฟลเดอร์เดียวกัน หรือ lib/)
Drag & Drop : pip install tkinterdnd2
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import itertools
import json
import os
import queue
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import ModuleType

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # ใช้งานได้ตามปกติ แต่ไม่มี Drag & Drop
    DND_FILES = None
    TkinterDnD = None


# ============================================================ ค่าคงที่

ROOT = Path(__file__).resolve().parent
APP_NAME = "VideoConV3"
DEFAULT_SUFFIX = "_covertedV1"
TEMP_PREFIX = ".videoconv3_"

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".mov", ".ts", ".mts", ".m2ts", ".webm", ".avi",
}
TEXT_SUBTITLE_CODECS = {
    "subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text",
}

AUDIO_MODES = {
    "Dolby Matrix 2.0 (Lt/Rt)": "matrix",
    "Stereo 2.0 (Lo/Ro)": "loro",
    "Passthrough Raw 5.1 (ไม่ downmix)": "raw51",
}
MATRIX_TYPES = {
    "Dolby Pro Logic II": "pl2",
    "Pro Logic II Simple": "pl2s",
    "Dolby Pro Logic": "pl",
    "Dolby Surround": "ds",
}
MATRIX_LABELS = {
    "pl2": "DPL II", "pl2s": "DPL II Simple",
    "pl": "DPL", "ds": "Dolby Surround",
}
DOLBY_FORMATS = {
    "E-AC-3 (Dolby Digital Plus)": "eac3",
    "AC-3 (Dolby Digital)": "ac3",
}
DOLBY_RATES = [
    "96", "128", "160", "192", "224", "256", "320",
    "384", "448", "512", "640", "768", "1024",
]
DRC_PROFILES = [
    "film_light", "film_standard", "music_light",
    "music_standard", "speech", "none",
]
PREFERRED_DOWNMIX = {
    "Lt/Rt (Pro Logic II)": "ltrt-pl2",
    "Lt/Rt": "ltrt",
    "Lo/Ro": "loro",
    "ไม่ระบุ": "not_indicated",
}
CENTER_LEVELS = ["+1.5", "0", "-1.5", "-3", "-4.5", "-6", "-inf"]
SURROUND_LEVELS = ["-1.5", "-3", "-4.5", "-6", "-inf"]

AAC_MODES = {"TVBR (คุณภาพคงที่)": "tvbr", "CVBR": "cvbr", "ABR": "abr"}
AAC_RATES = [
    "96", "112", "128", "160", "192", "224", "256",
    "288", "320", "384", "448", "512",
]
TVBR_QUALITY = {
    96: 54, 112: 63, 128: 73, 160: 82, 192: 91,
    224: 100, 256: 109, 288: 118, 320: 127,
}
HEADROOM = {
    "Peak (สแกน 2 รอบ, แนะนำ)": "peak",
    "Static (ลดแบบ worst-case)": "static",
    "ไม่ลด": "none",
}

VIDEO_PRESETS = {
    "Copy Video Stream": None,
    "Animate 24fps (Modern Anime)": (
        "crf=31:qpmin=28:qpmax=51:keyint=360:bframes=5:"
        "b-pyramid=1:ref=5:rc-lookahead=48:lookahead-slices=4:"
        "vbv-maxrate=5000:vbv-bufsize=7500:aq-mode=2:aq-strength=1.15:"
        "psy-rd=0.4:psy-rdoq=0.0:deblock=1,1:open-gop=1:weightp=0"
    ),
    "90s MV Tape (มี Noise / Grain)": (
        "crf=31:qpmin=28:qpmax=51:keyint=375:bframes=5:"
        "b-pyramid=1:ref=5:rc-lookahead=40:lookahead-slices=4:"
        "vbv-maxrate=5000:vbv-bufsize=7500:aq-mode=2:aq-strength=0.85:"
        "no-sao=1:psy-rd=1.0:psy-rdoq=1.0:deblock=-1,-1:"
        "open-gop=1:weightp=0"
    ),
    "90s MV Tape (ไม่มี Noise / Clean)": (
        "crf=32:qpmin=30:qpmax=51:keyint=375:bframes=5:"
        "b-pyramid=1:ref=5:rc-lookahead=32:aq-mode=2:aq-strength=1.0:"
        "sao=1:psy-rd=0.7:deblock=0,0:open-gop=1:weightp=0"
    ),
    "Movie / Film 24fps (หนังฟิล์ม / เกรนธรรมชาติ)": (
        "crf=32:qpmin=30:qpmax=51:keyint=360:bframes=5:"
        "b-pyramid=1:ref=5:rc-lookahead=32:aq-mode=2:aq-strength=0.9:"
        "no-sao=1:psy-rd=1.0:psy-rdoq=1.0:deblock=-1,-1:"
        "open-gop=1:weightp=0"
    ),
    "Classic Anime 24fps (อนิเมะคลาสสิก / วาดมือ มีเกรน)": (
        "crf=32:qpmin=30:qpmax=51:keyint=360:bframes=5:"
        "b-pyramid=1:ref=5:rc-lookahead=32:aq-mode=2:aq-strength=1.1:"
        "no-sao=1:psy-rd=0.6:psy-rdoq=0.5:deblock=0,0:"
        "open-gop=1:weightp=0"
    ),
}
X265_SPEEDS = ["fast", "medium", "slow", "slower"]
PIX_FMTS = {"8-bit (yuv420p)": "yuv420p", "10-bit (yuv420p10le)": "yuv420p10le"}
DEINT_RATES = {
    "2x — ทุก field (send_field)": "send_field",
    "1x — ทุก frame (send_frame)": "send_frame",
}
FASTSTART = {
    "อัตโนมัติ (MP4Box ถ้ามี ไม่งั้น Native)": "auto",
    "Native (ย้าย moov มาหน้าไฟล์)": "native",
    "MP4Box -inter 500": "mp4box",
    "ปิด": "off",
}
SCOPES = {"ไทยและอังกฤษ": "th_en", "ทุกภาษา": "all"}
TH_EN = {"tha", "eng"}

LANGUAGE_ALIASES = {
    "th": "tha", "tha": "tha", "thai": "tha", "ไทย": "tha",
    "en": "eng", "eng": "eng", "english": "eng",
    "ja": "jpn", "jp": "jpn", "jpn": "jpn", "japanese": "jpn",
    "ko": "kor", "kor": "kor", "korean": "kor",
    "zh": "chi", "zho": "chi", "chi": "chi", "chinese": "chi",
    "fr": "fre", "fra": "fre", "fre": "fre", "french": "fre",
    "de": "ger", "deu": "ger", "ger": "ger", "german": "ger",
    "es": "spa", "spa": "spa", "spanish": "spa",
    "it": "ita", "ita": "ita", "ru": "rus", "rus": "rus",
    "und": "und",
}
LANG_NAMES = {
    "tha": "Thai", "eng": "English", "jpn": "Japanese", "kor": "Korean",
    "chi": "Chinese", "fre": "French", "ger": "German", "spa": "Spanish",
    "ita": "Italian", "rus": "Russian", "und": "Unknown",
}
LANG_CHOICES = list(LANG_NAMES)

AUDIO_KEYWORDS = {
    "tha": ["thai", "พากย์ไทย", "ภาษาไทย", "เสียงไทย"],
    "eng": ["english", "eng", "original"],
}
SUB_KEYWORDS = {
    "tha": ["thai", "ไทย", "ซับไทย", "บรรยายไทย"],
    "eng": ["english", "eng"],
}
FLAG_LABELS = {
    "hearing_impaired": "SDH",
    "commentary": "Commentary",
    "visual_impaired": "AD",
}

CHECK, UNCHECK = "☑", "☐"


# ============================================================ ข้อผิดพลาด / ยูทิลิตี

class AppError(RuntimeError):
    """ข้อผิดพลาดที่แสดงให้ผู้ใช้เข้าใจได้"""


class Cancelled(Exception):
    """ผู้ใช้กดหยุด"""


def hidden_window() -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}


def find_program(*names: str, required: bool = True) -> str | None:
    for name in names:
        candidates = [name + ".exe", name] if os.name == "nt" else [name]
        for directory in (ROOT, ROOT / "bin"):
            for candidate in candidates:
                path = directory / candidate
                if path.is_file():
                    return str(path)
        for candidate in candidates:
            found = shutil.which(candidate)
            if found:
                return found
    if required:
        raise AppError(f"ไม่พบ {names[0]} ใน PATH, โฟลเดอร์โปรแกรม หรือ bin/")
    return None


def load_helper(name: str) -> ModuleType:
    for directory in (ROOT / "lib", ROOT):
        path = directory / f"{name}.py"
        if path.is_file():
            break
    else:
        raise AppError(f"ไม่พบ {name}.py ในโฟลเดอร์โปรแกรมหรือ lib/")

    module_name = f"videoconv3_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise AppError(f"โหลด {path.name} ไม่ได้")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def run_capture(command: list[str], label: str) -> str:
    proc = subprocess.run(
        command, capture_output=True, text=True,
        encoding="utf-8", errors="replace", **hidden_window(),
    )
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()[-2000:]
        raise AppError(f"{label} ไม่สำเร็จ (exit {proc.returncode})\n{detail}")
    return proc.stdout


def to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_rate(value) -> float:
    text = str(value or "")
    if "/" in text:
        num, den = text.split("/", 1)
        den_f = to_float(den)
        return to_float(num) / den_f if den_f else 0.0
    return to_float(text)


def norm_lang(value, fallback: str = "und") -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return fallback
    raw = LANGUAGE_ALIASES.get(raw, raw)
    return raw if re.fullmatch(r"[a-z]{3}", raw) else fallback


def fmt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}.{ms:03}"


def parse_time(text: str) -> float:
    value = text.strip().replace(",", ".")
    parts = value.split(":")
    if not value or len(parts) > 3:
        raise ValueError(text)
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    if seconds < 0:
        raise ValueError(text)
    return seconds


def read_text_guess(path: Path) -> str:
    """อ่านไฟล์ข้อความโดยเดา encoding (UTF-8, UTF-16, TIS-620/cp874)"""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    for encoding in ("utf-8-sig", "cp874"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def write_text_utf8(path: Path, text: str) -> None:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def command_text(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


# ============================================================ ข้อมูลงาน

@dataclass
class AudioTrack:
    index: int
    ordinal: int
    codec: str
    channels: int
    layout: str
    title: str
    tag_lang: str
    tag_source: str
    offset: float = 0.0
    lang: str = "und"
    lang_source: str = ""
    include: bool = True
    flags: list[str] = field(default_factory=list)


@dataclass
class SubTrack:
    index: int
    ordinal: int
    codec: str
    title: str
    tag_lang: str
    tag_source: str
    lang: str = "und"
    include: bool = True
    forced: bool = False
    flags: list[str] = field(default_factory=list)
    external: str = ""
    auto_sidecar: bool = False

    @property
    def text_based(self) -> bool:
        return bool(self.external) or self.codec in TEXT_SUBTITLE_CODECS


@dataclass
class Chapter:
    start: float
    title: str


@dataclass
class Job:
    jid: int
    path: Path
    duration: float
    fps: float
    vcodec: str
    width: int
    height: int
    video_index: int
    audio: list[AudioTrack]
    subs: list[SubTrack]
    chapters: list[Chapter]
    source_chapters: list[Chapter]
    include_chapters: bool = True
    status: str = "พร้อม"


@dataclass(frozen=True)
class Settings:
    video_preset: str
    x265_speed: str
    pix_fmt: str
    deinterlace: bool
    deint_algo: str
    deint_rate: str
    deint_parity: str
    use_dolby: bool
    dolby_mode: str
    dolby_matrix: str
    dolby_format: str
    dolby_rate: int
    dolby_drc: str
    dolby_pref: str
    dolby_center: str
    dolby_surround: str
    use_aac: bool
    aac_mode: str
    aac_matrix: str
    aac_qaac: str
    aac_rate: int
    dialnorm: bool
    headroom: str
    default_lang: str
    subs_enabled: bool
    faststart: str
    overwrite: bool
    suffix: str
    output_dir: str


def detect_language(info: dict, keywords: dict) -> tuple[str, str]:
    tags = info.get("tags") or {}
    lang = norm_lang(tags.get("language"))
    if lang != "und":
        return lang, "tag"
    title = str(tags.get("title") or "").lower()
    for code, words in keywords.items():
        for word in words:
            if word.isascii():
                if re.search(rf"\b{re.escape(word)}\b", title):
                    return code, "ชื่อแทร็ก"
            elif word in title:
                return code, "ชื่อแทร็ก"
    return "und", ""


def special_flags(info: dict) -> list[str]:
    disposition = info.get("disposition") or {}
    title = str((info.get("tags") or {}).get("title") or "").lower()
    flags = []
    if disposition.get("hearing_impaired") or re.search(r"\bsdh\b", title):
        flags.append("hearing_impaired")
    if disposition.get("commentary") or "commentary" in title:
        flags.append("commentary")
    if disposition.get("visual_impaired") or re.search(
        r"\b(desc|audio description)\b", title
    ):
        flags.append("visual_impaired")
    return flags


def find_sidecars(video: Path, start_ordinal: int) -> list[SubTrack]:
    """หา SRT ข้างวิดีโอ เช่น movie.th.srt, movie.eng.forced.srt"""
    found: list[SubTrack] = []
    stem = video.stem.lower()
    for path in sorted(video.parent.glob("*.srt")):
        name = path.stem
        if not name.lower().startswith(stem):
            continue
        rest = name[len(video.stem):]
        if rest and rest[0] not in "._- ":
            continue
        parts = [p for p in re.split(r"[._\-\s]+", rest.lower()) if p]
        lang, forced = "und", False
        for part in parts:
            if part == "forced":
                forced = True
            elif part in LANGUAGE_ALIASES:
                lang = LANGUAGE_ALIASES[part]
        found.append(SubTrack(
            index=-1,
            ordinal=start_ordinal + len(found),
            codec="subrip",
            title=path.name,
            tag_lang=lang,
            tag_source="ชื่อไฟล์" if lang != "und" else "",
            forced=forced,
            external=str(path),
            auto_sidecar=True,
        ))
    return found


def analyze(ffprobe: str, path: Path, jid: int) -> Job:
    output = run_capture(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "format=duration,start_time:"
            "stream=index,codec_type,codec_name,channels,channel_layout,"
            "width,height,r_frame_rate,avg_frame_rate,start_time:"
            "stream_tags=language,title:"
            "stream_disposition=default,forced,hearing_impaired,"
            "visual_impaired,commentary,attached_pic",
            "-show_chapters",
            "-of", "json", str(path),
        ],
        f"ffprobe {path.name}",
    )
    try:
        data = json.loads(output)
    except ValueError as exc:
        raise AppError(f"อ่านข้อมูล stream ของ {path.name} ไม่ได้") from exc

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next(
        (
            s for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        raise AppError("ไม่มี video stream")

    fps = parse_rate(video.get("avg_frame_rate"))
    if not 10 <= fps <= 240:
        fps = parse_rate(video.get("r_frame_rate"))
    if not 10 <= fps <= 240:
        fps = 24.0

    video_start = to_float(video.get("start_time"))
    audio: list[AudioTrack] = []
    subs: list[SubTrack] = []

    for info in streams:
        kind = info.get("codec_type")
        title = str((info.get("tags") or {}).get("title") or "").strip()
        if kind == "audio":
            lang, source = detect_language(info, AUDIO_KEYWORDS)
            audio.append(AudioTrack(
                index=int(info["index"]),
                ordinal=len(audio) + 1,
                codec=str(info.get("codec_name") or "?"),
                channels=int(info.get("channels") or 0),
                layout=str(info.get("channel_layout") or ""),
                title=title,
                tag_lang=lang,
                tag_source=source,
                offset=to_float(info.get("start_time"), video_start) - video_start,
                flags=special_flags(info),
            ))
        elif kind == "subtitle":
            lang, source = detect_language(info, SUB_KEYWORDS)
            disposition = info.get("disposition") or {}
            subs.append(SubTrack(
                index=int(info["index"]),
                ordinal=len(subs) + 1,
                codec=str(info.get("codec_name") or "?").lower(),
                title=title,
                tag_lang=lang,
                tag_source=source,
                forced=bool(disposition.get("forced")) or "forced" in title.lower(),
                flags=special_flags(info),
            ))

    subs += find_sidecars(path, len(subs) + 1)

    format_start = to_float(fmt.get("start_time"))
    chapters = []
    for number, item in enumerate(data.get("chapters") or [], 1):
        start = max(0.0, to_float(item.get("start_time")) - format_start)
        name = str((item.get("tags") or {}).get("title") or "").strip()
        chapters.append(Chapter(start, name or f"Chapter {number:02}"))
    chapters.sort(key=lambda c: c.start)

    return Job(
        jid=jid,
        path=path,
        duration=to_float(fmt.get("duration")),
        fps=fps,
        vcodec=str(video.get("codec_name") or "?"),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        video_index=int(video["index"]),
        audio=audio,
        subs=subs,
        chapters=chapters,
        source_chapters=copy.deepcopy(chapters),
    )


def apply_rules(job: Job, rules: dict) -> None:
    for track in job.audio:
        lang, source = track.tag_lang, track.tag_source
        if lang == "und":
            if track.ordinal == 1:
                lang, source = rules["fb1"], "สำรองแทร็ก 1"
            elif track.ordinal == 2:
                lang, source = rules["fb2"], "สำรองแทร็ก 2"
        track.lang, track.lang_source = lang, source
        track.include = rules["audio_scope"] == "all" or lang in TH_EN

    if job.audio and not any(t.include for t in job.audio):
        job.audio[0].include = True  # อย่าให้ไฟล์ไม่มีเสียง

    for sub in job.subs:
        sub.lang = sub.tag_lang
        in_scope = rules["sub_scope"] == "all" or sub.lang in TH_EN
        sidecar_ok = not sub.auto_sidecar or rules["sidecar"]
        sub.include = sub.text_based and in_scope and sidecar_ok


def pick_default(tracks: list[AudioTrack], lang: str) -> AudioTrack | None:
    for rule in (
        lambda t: t.lang == lang and not t.flags,
        lambda t: not t.flags,
        lambda t: True,
    ):
        for track in tracks:
            if rule(track):
                return track
    return None


# ============================================================ ซับไตเติล

ASS_TIME = re.compile(r"^\s*(\d+):(\d{1,2}):(\d{1,2})[.:](\d{1,3})\s*$")
LEGACY_ALIGN = {1: 1, 2: 2, 3: 3, 5: 7, 6: 8, 7: 9, 9: 4, 10: 5, 11: 6}
TIMING_LINE = re.compile(
    r"^(\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*"
    r"\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})(.*)$"
)


def ass_seconds(text: str) -> float:
    match = ASS_TIME.match(text)
    if not match:
        raise ValueError(text)
    hours, minutes, seconds, fraction = match.groups()
    return (
        int(hours) * 3600 + int(minutes) * 60 + int(seconds)
        + float("0." + fraction)
    )


def srt_time(seconds: float) -> str:
    return fmt_time(seconds).replace(".", ",")


def convert_ass_text(raw: str, italic: bool, bold: bool, legacy: bool):
    """คืน (alignment, ข้อความแบบ SRT/HTML, ข้ามหรือไม่)"""
    alignment = None
    state = {"i": False, "b": False, "u": False}
    out: list[str] = []

    def set_tag(name: str, on: bool) -> None:
        if state[name] != on:
            state[name] = on
            out.append(f"<{name}>" if on else f"</{name}>")

    set_tag("i", italic)
    set_tag("b", bold)

    for part in re.split(r"(\{[^}]*\})", raw):
        if part.startswith("{") and part.endswith("}"):
            for tag in re.finditer(r"\\([1-4]?[a-z]+)([^\\]*)", part[1:-1]):
                name = tag.group(1)
                number = re.match(r"\s*(\d+)", tag.group(2))
                value = int(number.group(1)) if number else None
                if name == "an" and value and 1 <= value <= 9:
                    alignment = value
                elif name == "a" and value in LEGACY_ALIGN:
                    alignment = LEGACY_ALIGN[value]
                elif name == "p" and value:
                    return None, "", True  # vector drawing
                elif name in ("i", "u") and value is not None:
                    set_tag(name, value != 0)
                elif name == "b" and value is not None:
                    set_tag("b", value == 1 or value >= 600)
                elif name == "r":
                    set_tag("i", italic)
                    set_tag("b", bold)
                    set_tag("u", False)
            continue
        out.append(
            part.replace("\\N", "\n").replace("\\n", " ").replace("\\h", " ")
        )

    for name in ("u", "b", "i"):
        set_tag(name, False)

    text = "".join(out)
    text = re.sub(r"<(\w)></\1>", "", text)
    text = "\n".join(line.strip() for line in text.split("\n")).strip()

    if legacy and alignment is not None:
        pass  # แปลงไว้แล้วผ่าน LEGACY_ALIGN
    return alignment, text, False


def ass_to_srt(ass_path: Path, srt_path: Path) -> int:
    """
    ASS/SSA → SRT สำหรับ tx3g_apple
    คงตำแหน่ง (Style Alignment, {\\anX}, {\\aX}) และ <i>/<b>/<u>
    ตัด effect อื่น เช่น karaoke, \\pos, \\move, drawing
    """
    content = read_text_guess(ass_path)
    section = ""
    style_fields: list[str] = []
    event_fields: list[str] = []
    styles: dict[str, tuple[int, bool, bool]] = {}
    legacy = False
    events: list[tuple[float, float, int, int, str]] = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line.lower()
            if section == "[v4 styles]":
                legacy = True
            continue

        lower = line.lower()
        if lower.startswith("format:"):
            fields = [p.strip().lower() for p in line.split(":", 1)[1].split(",")]
            if "styles" in section:
                style_fields = fields
            elif section == "[events]":
                event_fields = fields
            continue

        if "styles" in section and lower.startswith("style:") and style_fields:
            values = [
                p.strip() for p in
                line.split(":", 1)[1].split(",", len(style_fields) - 1)
            ]
            if len(values) != len(style_fields):
                continue
            record = dict(zip(style_fields, values))
            try:
                align = int(record.get("alignment", "2"))
            except ValueError:
                align = 2
            if legacy:
                align = LEGACY_ALIGN.get(align, 2)
            styles[record.get("name", "")] = (
                align,
                record.get("italic", "0") not in ("0", ""),
                record.get("bold", "0") not in ("0", ""),
            )
            continue

        if section != "[events]" or not lower.startswith("dialogue:") or not event_fields:
            continue

        values = [
            p.strip() for p in
            line.split(":", 1)[1].split(",", len(event_fields) - 1)
        ]
        if len(values) != len(event_fields):
            continue
        record = dict(zip(event_fields, values))
        try:
            start = ass_seconds(record["start"])
            end = ass_seconds(record["end"])
        except (KeyError, ValueError):
            continue
        if end <= start:
            continue

        style_align, italic, bold = styles.get(
            record.get("style", "").lstrip("*"), (2, False, False)
        )
        alignment, text, skip = convert_ass_text(
            record.get("text", ""), italic, bold, legacy
        )
        if skip or not re.sub(r"<[^>]+>", "", text).strip():
            continue
        try:
            layer = int(record.get("layer", "0") or 0)
        except ValueError:
            layer = 0
        events.append((start, end, layer, alignment or style_align, text))

    unique, seen = [], set()
    for event in sorted(events, key=lambda e: (e[0], e[2])):
        key = (round(event[0], 3), round(event[1], 3), event[3], event[4])
        if key not in seen:
            seen.add(key)
            unique.append(event)

    if not unique:
        raise AppError(f"ASS ไม่มี Dialogue ที่ใช้สร้าง TX3G ได้: {ass_path.name}")

    blocks = []
    for number, (start, end, _layer, align, text) in enumerate(unique, 1):
        prefix = f"{{\\an{align}}}" if align != 2 else ""
        blocks.append(
            f"{number}\n{srt_time(start)} --> {srt_time(end)}\n{prefix}{text}\n"
        )
    write_text_utf8(srt_path, "\n".join(blocks))
    return len(unique)


def mark_forced(text: str) -> str:
    """เติม !!! ท้ายบรรทัดเวลา (รูปแบบ Subler) เพื่อให้ tx3g_apple ตั้ง forced"""
    lines = []
    for line in text.split("\n"):
        match = TIMING_LINE.match(line)
        if match and "!!!" not in match.group(2):
            line = line.rstrip() + " !!!"
        lines.append(line)
    return "\n".join(lines)


# ============================================================ Chapters

def write_ffmetadata(path: Path, chapters: list[Chapter], duration: float) -> None:
    def escape(value: str) -> str:
        value = value.replace("\n", " ").replace("\r", " ")
        return re.sub(r"([=;#\\])", r"\\\1", value)

    ordered = sorted(chapters, key=lambda c: c.start)
    lines = [";FFMETADATA1"]
    for i, chapter in enumerate(ordered):
        start = int(round(chapter.start * 1000))
        if i + 1 < len(ordered):
            end = int(round(ordered[i + 1].start * 1000))
        else:
            end = int(round(duration * 1000)) if duration > chapter.start else start + 1000
        end = max(end, start + 1)
        lines += [
            "[CHAPTER]", "TIMEBASE=1/1000",
            f"START={start}", f"END={end}",
            f"title={escape(chapter.title)}",
        ]
    write_text_utf8(path, "\n".join(lines) + "\n")


def parse_chapter_file(text: str, existing: list[Chapter]) -> tuple[list[Chapter], str]:
    """รองรับ OGM (CHAPTER01=… / CHAPTER01NAME=…) และ HandBrake CSV (1,ชื่อ)"""
    times: dict[int, float] = {}
    names: dict[int, str] = {}
    for line in text.splitlines():
        name_match = re.match(r"\s*CHAPTER(\d+)NAME\s*=\s*(.*)$", line, re.I)
        time_match = re.match(r"\s*CHAPTER(\d+)\s*=\s*(.+)$", line, re.I)
        if name_match:
            names[int(name_match.group(1))] = name_match.group(2).strip()
        elif time_match:
            try:
                times[int(time_match.group(1))] = parse_time(time_match.group(2))
            except ValueError:
                pass

    if times:
        chapters = [
            Chapter(times[n], names.get(n) or f"Chapter {i:02}")
            for i, n in enumerate(sorted(times), 1)
        ]
        return sorted(chapters, key=lambda c: c.start), "OGM"

    renamed = copy.deepcopy(existing)
    changed = 0
    for line in text.splitlines():
        match = re.match(r"\s*(\d+)\s*,\s*(.*)$", line)
        if match:
            number = int(match.group(1))
            title = match.group(2).strip().strip('"').replace('\\,', ',')
            if 1 <= number <= len(renamed) and title:
                renamed[number - 1].title = title
                changed += 1
    if changed:
        return renamed, f"CSV (เปลี่ยนชื่อ {changed} บท)"
    raise AppError("ไม่พบรูปแบบ OGM หรือ HandBrake CSV ในไฟล์")


# ============================================================ MP4 / FastStart

def read_box_header(handle, position: int, end: int) -> tuple[int, bytes, int]:
    handle.seek(position)
    header = handle.read(8)
    if len(header) != 8:
        raise AppError("MP4 box header ไม่ครบ")
    size, kind = struct.unpack(">I4s", header)
    header_size = 8
    if size == 1:
        extra = handle.read(8)
        if len(extra) != 8:
            raise AppError("MP4 extended box header ไม่ครบ")
        size = struct.unpack(">Q", extra)[0]
        header_size = 16
    elif size == 0:
        size = end - position
    if size < header_size or position + size > end:
        raise AppError("MP4 box มีขนาดไม่ถูกต้อง")
    return size, kind, header_size


def top_level_boxes(path: Path) -> list[tuple[int, int, bytes]]:
    total = path.stat().st_size
    boxes, position = [], 0
    with path.open("rb") as handle:
        while position < total:
            size, kind, _ = read_box_header(handle, position, total)
            boxes.append((position, size, kind))
            position += size
    return boxes


def moov_before_mdat(path: Path) -> bool:
    kinds = [box[2] for box in top_level_boxes(path)]
    if b"moov" not in kinds or b"mdat" not in kinds:
        return False
    return kinds.index(b"moov") < kinds.index(b"mdat")


def patch_chunk_offsets(moov: bytes, low: int, high: int, shift: int) -> bytes:
    """เลื่อน stco/co64 เฉพาะ offset ที่อยู่ในช่วง [low, high)"""
    data = bytearray(moov)
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
    patched = 0

    def visit(start: int, end: int) -> None:
        nonlocal patched
        position = start
        while position + 8 <= end:
            size = struct.unpack_from(">I", data, position)[0]
            kind = bytes(data[position + 4:position + 8])
            header = 8
            if size == 1:
                size = struct.unpack_from(">Q", data, position + 8)[0]
                header = 16
            elif size == 0:
                size = end - position
            if size < header or position + size > end:
                raise AppError("box ภายใน moov มีขนาดไม่ถูกต้อง")
            body, box_end = position + header, position + size

            if kind in containers:
                visit(body, box_end)
            elif kind in (b"stco", b"co64"):
                count = struct.unpack_from(">I", data, body + 4)[0]
                width = 4 if kind == b"stco" else 8
                fmt = ">I" if width == 4 else ">Q"
                first = body + 8
                if first + count * width > box_end:
                    raise AppError("ตาราง chunk offset ไม่ถูกต้อง")
                for i in range(count):
                    at = first + i * width
                    old = struct.unpack_from(fmt, data, at)[0]
                    if low <= old < high:
                        new = old + shift
                        if new >= 1 << (width * 8):
                            raise AppError(
                                "stco overflow ระหว่าง FastStart — "
                                "ไฟล์ใหญ่เกินสำหรับ Native ให้ใช้ MP4Box"
                            )
                        struct.pack_into(fmt, data, at, new)
                patched += count
            position = box_end

    visit(0, len(data))
    if patched == 0:
        raise AppError("ไม่พบ stco/co64 ใน moov")
    return bytes(data)


def copy_bytes(source, target, count: int) -> None:
    remaining = count
    while remaining > 0:
        chunk = source.read(min(8 * 1024 * 1024, remaining))
        if not chunk:
            raise AppError("อ่าน MP4 ไม่ครบระหว่างทำ FastStart")
        target.write(chunk)
        remaining -= len(chunk)


def faststart_native(source: Path, destination: Path) -> None:
    """ย้าย moov มาไว้หลัง ftyp โดยไม่ remux (TX3G samples ไม่ถูกแตะ)"""
    boxes = top_level_boxes(source)
    kinds = [box[2] for box in boxes]
    if not kinds or kinds[0] != b"ftyp":
        raise AppError("MP4 ไม่มี ftyp เป็น box แรก")
    if any(k in kinds for k in (b"moof", b"mfra", b"sidx")):
        raise AppError("พบ fragmented MP4 — ไม่รองรับ")
    if kinds.count(b"moov") != 1:
        raise AppError("MP4 ต้องมี moov เพียง 1 box")

    moov_index = kinds.index(b"moov")
    if b"mdat" in kinds and moov_index < kinds.index(b"mdat"):
        shutil.copyfile(source, destination)
        return

    total = source.stat().st_size
    ftyp_end = boxes[0][0] + boxes[0][1]
    moov_start, moov_size, _ = boxes[moov_index]

    with source.open("rb") as handle:
        handle.seek(moov_start)
        moov = handle.read(moov_size)
        patched = patch_chunk_offsets(moov, ftyp_end, moov_start, moov_size)
        with destination.open("wb") as out:
            handle.seek(0)
            copy_bytes(handle, out, ftyp_end)
            out.write(patched)
            handle.seek(ftyp_end)
            copy_bytes(handle, out, moov_start - ftyp_end)
            handle.seek(moov_start + moov_size)
            copy_bytes(handle, out, total - moov_start - moov_size)


# ============================================================ ดัก print ของ helper

class StreamToLog(io.TextIOBase):
    """ส่ง stdout/stderr ของ helper ไปที่ Console; บรรทัด \\r = progress (throttle)"""

    def __init__(self, emit, status, prefix: str):
        super().__init__()
        self.emit, self.status, self.prefix = emit, status, prefix
        self.buffer_text = ""
        self.last_transient = 0.0

    def writable(self) -> bool:
        return True

    def write(self, text) -> int:
        text = str(text)
        self.buffer_text += text
        while True:
            match = re.search(r"[\r\n]", self.buffer_text)
            if not match:
                break
            line = self.buffer_text[:match.start()].strip()
            separator = self.buffer_text[match.start()]
            self.buffer_text = self.buffer_text[match.end():]
            if not line:
                continue
            if separator == "\r":
                self.status(f"{self.prefix}: {line[:150]}")
                now = time.monotonic()
                if now - self.last_transient >= 5:
                    self.last_transient = now
                    self.emit(f"  [{self.prefix}] {line}")
            else:
                self.emit(f"  [{self.prefix}] {line}")
        return len(text)

    def finish(self) -> None:
        if self.buffer_text.strip():
            self.emit(f"  [{self.prefix}] {self.buffer_text.strip()}")
        self.buffer_text = ""


# ============================================================ Pipeline (worker thread)

PROGRESS_LINE = re.compile(r"^([a-z_0-9]+)=(.*)$")
PROGRESS_KEYS = {
    "frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms",
    "out_time", "dup_frames", "drop_frames", "speed", "progress",
}


class Pipeline:
    def __init__(self, settings: Settings, emit, post, cancel: threading.Event):
        self.s = settings
        self.emit = emit
        self.post = post
        self.cancel = cancel
        self.proc: subprocess.Popen | None = None

    # ---------- เครื่องมือ

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    @contextlib.contextmanager
    def capture(self, prefix: str):
        writer = StreamToLog(
            self.emit,
            lambda text: self.post("progress", (None, text)),
            prefix,
        )
        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                yield
        finally:
            writer.finish()

    @staticmethod
    def progress_text(state: dict, duration: float) -> tuple[float | None, str]:
        try:
            seconds = max(0.0, int(state.get("out_time_us") or state.get("out_time_ms")) / 1e6)
        except (TypeError, ValueError):
            seconds = 0.0
        speed = to_float(str(state.get("speed") or "").rstrip("x").strip())
        parts = [fmt_time(seconds)[:8]]
        percent = None
        if duration > 0:
            percent = min(100.0, seconds / duration * 100)
            parts.insert(0, f"{percent:5.1f}%")
            if speed > 0 and percent < 100:
                parts.append(f"ETA {fmt_time((duration - seconds) / speed)[:8]}")
        fps = state.get("fps")
        if fps and to_float(fps) > 0:
            parts.append(f"{to_float(fps):.1f} fps")
        if speed > 0:
            parts.append(f"{speed:.2f}x")
        if state.get("progress") == "end" and duration > 0:
            percent = 100.0
        return percent, " · ".join(parts)

    def run(self, command: list[str], label: str, duration: float = 0.0) -> None:
        """รันโปรแกรมภายนอก แสดง log + progress แบบ real-time"""
        self.check_cancel()
        self.emit(f"▶ {label}")
        self.emit(f"  $ {command_text(command)}")
        self.post("progress", (0.0 if duration else None, label))

        try:
            proc = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, **hidden_window(),
            )
        except OSError as exc:
            raise AppError(f"เรียก {label} ไม่ได้: {exc}") from exc

        self.proc = proc
        tail: deque[str] = deque(maxlen=15)
        state: dict[str, str] = {}
        last_logged = -100.0
        last_transient = 0.0

        def handle(line: str, transient: bool) -> None:
            nonlocal last_logged, last_transient
            match = PROGRESS_LINE.match(line)
            if match and (match.group(1) in PROGRESS_KEYS or match.group(1).startswith("stream_")):
                key, value = match.groups()
                state[key] = value
                if key == "progress":
                    percent, text = self.progress_text(state, duration)
                    self.post("progress", (percent, f"{label}: {text}"))
                    if percent is None or percent - last_logged >= 5 or value == "end":
                        if percent is not None:
                            last_logged = percent
                        self.emit(f"  [{label}] {text}")
                return
            tail.append(line)
            if transient:
                self.post("progress", (None, f"{label}: {line[:150]}"))
                now = time.monotonic()
                if now - last_transient >= 5:
                    last_transient = now
                    self.emit(f"  [{label}] {line}")
            else:
                self.emit(f"  [{label}] {line}")

        buffer = b""
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                buffer += chunk
                while True:
                    match = re.search(rb"[\r\n]", buffer)
                    if not match:
                        break
                    at = match.start()
                    if buffer[at:at + 1] == b"\r" and at + 1 == len(buffer):
                        break  # รอดูว่าเป็น \r\n หรือไม่
                    crlf = buffer[at:at + 2] == b"\r\n"
                    transient = buffer[at:at + 1] == b"\r" and not crlf
                    raw, buffer = buffer[:at], buffer[at + (2 if crlf else 1):]
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line:
                        handle(line, transient)
                if self.cancel.is_set():
                    proc.terminate()
            if buffer.strip():
                handle(buffer.decode("utf-8", errors="replace").strip(), False)
        finally:
            code = proc.wait()
            self.proc = None

        self.check_cancel()
        if code != 0:
            raise AppError(f"{label} ล้มเหลว (exit {code})\n" + "\n".join(tail))

    def ffmpeg_base(self, loglevel: str = "warning") -> list[str]:
        return [
            self.ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-loglevel", loglevel, "-nostats", "-progress", "pipe:1",
        ]

    # ---------- เริ่มงาน

    def load_tools(self, jobs: list[Job]) -> None:
        s = self.s
        self.ffmpeg = find_program("ffmpeg")
        self.ffprobe = find_program("ffprobe")
        self.qaac = find_program("qaac64", "qaac") if s.use_aac else None
        self.encoder = load_helper("ddp_encode") if s.use_dolby else None

        need_matrix = (
            (s.use_dolby and s.dolby_mode == "matrix")
            or (s.use_aac and s.aac_mode == "matrix")
        )
        self.matrix = load_helper("6to2matrixencoder") if need_matrix else None

        need_tx3g = s.subs_enabled and any(
            t.include and t.text_based for job in jobs for t in job.subs
        )
        self.tx3g = load_helper("tx3g_apple") if need_tx3g else None

        self.dee = None
        if s.use_dolby:
            self.dee = os.environ.get("DEE_BIN") or find_program("dee", required=False)

        self.mediainfo = find_program("mediainfo", required=False) if s.dialnorm else None
        self.mp4box = (
            find_program("MP4Box", "mp4box", required=False)
            if s.faststart in ("auto", "mp4box") else None
        )
        if s.faststart == "mp4box" and not self.mp4box:
            raise AppError("เลือก FastStart แบบ MP4Box แต่ไม่พบ MP4Box")

        self.emit("เครื่องมือที่ใช้:")
        for name, value in (
            ("ffmpeg", self.ffmpeg), ("ffprobe", self.ffprobe),
            ("qaac", self.qaac), ("DEE", self.dee or ("ให้ ddp_encode ค้นหาเอง" if s.use_dolby else None)),
            ("Matrix", "6to2matrixencoder.py" if self.matrix else None),
            ("TX3G", "tx3g_apple.py" if self.tx3g else None),
            ("MediaInfo", self.mediainfo), ("MP4Box", self.mp4box),
        ):
            if value:
                self.emit(f"  • {name}: {value}")
        if s.dialnorm and not self.mediainfo:
            self.emit("[WARN] ไม่พบ mediainfo — ข้ามการชดเชย Dialnorm")

    def run_jobs(self, jobs: list[Job]) -> str:
        succeeded = failed = 0
        try:
            self.load_tools(jobs)
        except Exception as exc:
            self.emit(f"[ERROR] เริ่มงานไม่ได้: {exc}")
            return "เริ่มงานไม่ได้ — ดูรายละเอียดใน Console"

        total = len(jobs)
        for number, job in enumerate(jobs, 1):
            if self.cancel.is_set():
                break
            self.post("job_status", (job.jid, "กำลังแปลง"))
            self.post("overall", ((number - 1) / total * 100, f"ไฟล์ {number}/{total}: {job.path.name}"))
            self.emit("")
            self.emit(f"══════ [{number}/{total}] {job.path}")
            started = time.monotonic()
            try:
                output = self.convert(job)
                succeeded += 1
                elapsed = fmt_time(time.monotonic() - started)[:8]
                self.post("job_status", (job.jid, "เสร็จ"))
                self.emit(f"[OK] {output} (ใช้เวลา {elapsed})")
            except Cancelled:
                self.post("job_status", (job.jid, "ยกเลิก"))
                self.emit("[WARN] ผู้ใช้หยุดการทำงาน")
                break
            except Exception as exc:
                failed += 1
                self.post("job_status", (job.jid, "ล้มเหลว"))
                self.emit(f"[ERROR] {job.path.name}: {exc}")
                if not isinstance(exc, AppError):
                    self.emit(traceback.format_exc())

        self.post("overall", (100.0, "เสร็จสิ้น"))
        self.post("progress", (0.0, ""))
        summary = f"สำเร็จ {succeeded} ไฟล์ · ล้มเหลว {failed} ไฟล์"
        if self.cancel.is_set():
            summary += " · ถูกหยุดกลางคัน"
        self.emit(f"สรุป: {summary}")
        return summary

    # ---------- เสียง

    def read_dialnorm(self, source: Path) -> dict[int, int]:
        try:
            data = json.loads(run_capture(
                [self.mediainfo, "--Output=JSON", str(source)], "MediaInfo"
            ))
        except (AppError, ValueError) as exc:
            self.emit(f"[WARN] อ่าน Dialnorm ไม่ได้: {exc}")
            return {}

        result, ordinal = {}, 0
        for track in (data.get("media") or {}).get("track") or []:
            if track.get("@type") != "Audio":
                continue
            ordinal += 1
            combined = dict(track)
            combined.update(track.get("extra") or {})
            for key, value in combined.items():
                if "dialnorm" in key.lower() or "dialog_normalization" in key.lower():
                    match = re.search(r"-?\d+", str(value))
                    if match:
                        result[ordinal] = int(match.group())
                        break
        return result

    def extract_pcm(self, source: Path, track: AudioTrack, target: Path,
                    gain: float, duration: float) -> None:
        command = self.ffmpeg_base() + [
            "-i", str(source), "-map", f"0:{track.index}", "-vn", "-sn", "-dn",
        ]
        if gain:
            command += ["-af", f"volume={gain:.2f}dB"]
        command += ["-ar", "48000", "-c:a", "pcm_f32le", "-rf64", "auto", str(target)]
        self.run(command, f"แตกเสียง #{track.ordinal} เป็น PCM", duration)

    def run_matrix(self, pcm: Path, target: Path, matrix_type: str, track: AudioTrack) -> None:
        argv = [
            "-i", str(pcm), "-o", str(target),
            "--mode", matrix_type, "--headroom", self.s.headroom,
            "--ffmpeg", self.ffmpeg, "-y",
        ]
        if track.channels == 6 and not track.layout:
            argv += ["--layout", "L,R,C,LFE,Ls,Rs"]
        self.emit(f"▶ Matrix {MATRIX_LABELS[matrix_type]}: {' '.join(argv)}")
        with self.capture("Matrix"):
            try:
                args = self.matrix.build_arg_parser().parse_args(argv)
                code = self.matrix.EncoderPipeline(args).run()
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        self.check_cancel()
        if code != 0 or not target.is_file():
            raise AppError(f"Matrix encoder ล้มเหลว (exit {code})")

    def prepare_audio(self, pcm: Path, track: AudioTrack, mode: str,
                      matrix_type: str, target: Path, duration: float) -> tuple[Path, str, int]:
        """คืน (ไฟล์ wav, ชนิดจริง, จำนวนช่อง) — ชนิด: matrix/loro/raw51/stereo"""
        channels = track.channels

        if channels <= 2:
            if mode != "loro":
                self.emit("  ต้นฉบับไม่เกิน 2 ช่อง: ใช้ stereo เดิม (ไม่ทำ Matrix/5.1)")
            if channels == 2:
                return pcm, "stereo", 2
            self.run(
                self.ffmpeg_base() + ["-i", str(pcm), "-ac", "2", "-c:a", "pcm_f32le",
                                      "-rf64", "auto", str(target)],
                "แปลง mono → stereo", duration,
            )
            return target, "stereo", 2

        if mode == "raw51":
            if channels == 6 and (not track.layout or track.layout.startswith("5.1")):
                self.emit("  Raw 5.1: ใช้ PCM 6 ช่องเดิม ไม่ downmix")
                return pcm, "raw51", 6
            if not track.layout:
                raise AppError(f"{channels} ช่องแต่ไม่ทราบ layout — แปลงเป็น 5.1 อย่างปลอดภัยไม่ได้")
            self.run(
                self.ffmpeg_base() + ["-i", str(pcm), "-ac", "6", "-c:a", "pcm_f32le",
                                      "-rf64", "auto", str(target)],
                f"จัด {track.layout} → 5.1", duration,
            )
            return target, "raw51", 6

        if mode == "matrix":
            self.run_matrix(pcm, target, matrix_type, track)
            return target, "matrix", 2

        audio_filter = loro_filter(track)
        command = self.ffmpeg_base() + ["-i", str(pcm)]
        if audio_filter:
            command += ["-af", audio_filter]
        else:
            self.emit("  [WARN] ไม่มีสูตร Lo/Ro สำหรับ layout นี้ — ใช้ FFmpeg downmix")
        command += ["-ac", "2", "-c:a", "pcm_f32le", "-rf64", "auto", str(target)]
        self.run(command, "สร้าง Lo/Ro 2.0", duration)
        return target, "loro", 2

    def encode_dolby(self, source: Path, target: Path, kind: str,
                     channels: int, temp: Path) -> None:
        s = self.s
        if channels == 6:
            preferred, surround = s.dolby_pref, "not_indicated"
        elif kind == "matrix":
            preferred = "ltrt-pl2" if s.dolby_matrix in ("pl2", "pl2s") else "ltrt"
            surround = "yes"
        elif kind == "loro":
            preferred, surround = "loro", "no"
        else:
            preferred, surround = "loro", "not_indicated"

        self.emit(
            f"▶ DEE: {s.dolby_format} {s.dolby_rate}k · {channels} ch · "
            f"surround={surround} · downmix={preferred} · DRC={s.dolby_drc}"
        )
        with self.capture("DEE"):
            try:
                code = self.encoder.encode(
                    str(source), str(target),
                    encoder_mode="dd" if s.dolby_format == "ac3" else "ddp",
                    output_format=s.dolby_format,
                    data_rate=s.dolby_rate,
                    downmix_config="off",
                    lfe_on=channels == 6,
                    dolby_surround_mode=surround,
                    dolby_surround_ex_mode="no",
                    line_drc=s.dolby_drc,
                    rf_drc=s.dolby_drc,
                    loudness_mode="measure_only",
                    loro_center=s.dolby_center,
                    loro_surround=s.dolby_surround,
                    ltrt_center=s.dolby_center,
                    ltrt_surround=s.dolby_surround,
                    preferred_downmix=preferred,
                    temp_dir=str(temp),
                    dee_bin=self.dee,
                    verbose=True,
                    quiet=False,
                    overwrite=True,
                )
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        self.check_cancel()
        if code != 0 or not target.is_file() or target.stat().st_size == 0:
            raise AppError(f"เข้ารหัส Dolby ไม่สำเร็จ (exit {code})")

    def encode_aac(self, source: Path, target: Path, duration: float) -> None:
        s = self.s
        if s.aac_qaac == "tvbr":
            quality = TVBR_QUALITY.get(s.aac_rate, 127 if s.aac_rate > 320 else 91)
            options = ["--tvbr", str(quality)]
        else:
            options = [f"--{s.aac_qaac}", str(s.aac_rate)]
        self.run(
            [self.qaac, *options, "--quality", "2", "--no-delay",
             str(source), "-o", str(target)],
            "qaac AAC", duration,
        )
        if not target.is_file() or target.stat().st_size == 0:
            raise AppError("qaac ไม่ได้สร้างไฟล์ AAC")

    def track_title(self, track: AudioTrack, kind: str, channels: int, codec: str) -> str:
        s = self.s
        name = LANG_NAMES.get(track.lang, track.lang.upper())
        matrix_type = s.dolby_matrix if codec == "dolby" else s.aac_matrix
        layout = {
            "matrix": f"{MATRIX_LABELS[matrix_type]} 2.0",
            "loro": "Lo/Ro 2.0",
            "raw51": "5.1",
        }.get(kind, "2.0")
        if codec == "dolby":
            codec_name = (
                f"Dolby Digital Plus {s.dolby_rate}k" if s.dolby_format == "eac3"
                else f"Dolby Digital {s.dolby_rate}k"
            )
        else:
            approx = "~" if s.aac_qaac == "tvbr" else ""
            codec_name = f"Apple AAC {s.aac_qaac.upper()} {approx}{s.aac_rate}k"
        title = f"{name} ({layout} - {codec_name})"
        extras = [FLAG_LABELS[f] for f in track.flags if f in FLAG_LABELS]
        return title + (f" [{', '.join(extras)}]" if extras else "")

    # ---------- ซับ

    def prepare_srt(self, job: Job, sub: SubTrack, temp: Path, number: int) -> Path:
        srt = temp / f"sub{number}.srt"
        if sub.external:
            write_text_utf8(srt, read_text_guess(Path(sub.external)))
            self.emit(f"  ใช้ SRT ภายนอกโดยตรง: {Path(sub.external).name}")
        elif sub.codec in ("ass", "ssa"):
            ass = temp / f"sub{number}.ass"
            self.run(
                [self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
                 "-i", str(job.path), "-map", f"0:{sub.index}",
                 "-c:s", "copy" if sub.codec == "ass" else "ass", "-f", "ass", str(ass)],
                f"แยก ASS ซับ #{sub.ordinal}",
            )
            count = ass_to_srt(ass, srt)
            self.emit(f"  ASS → SRT {count} บรรทัด (คง {{\\anX}} และ <i>/<b>/<u>)")
        else:
            codec = "copy" if sub.codec in ("subrip", "srt") else "srt"
            self.run(
                [self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "warning",
                 "-i", str(job.path), "-map", f"0:{sub.index}",
                 "-c:s", codec, "-f", "srt", str(srt)],
                f"แยก SRT ซับ #{sub.ordinal}",
            )

        text = read_text_guess(srt)
        if "-->" not in text:
            raise AppError(f"ซับ #{sub.ordinal} ไม่มีข้อความ")
        if sub.forced:
            text = mark_forced(text)
            self.emit("  ทำเครื่องหมาย Forced (!!!) ทุกบรรทัด")
        write_text_utf8(srt, text)

        markers = []
        if re.search(r"\{\\an?\d", text):
            markers.append("{\\anX}")
        if "X1:0" in text:
            markers.append("X1:0")
        if "!!!" in text:
            markers.append("!!!")
        if markers:
            self.emit(f"  พบ marker: {', '.join(markers)}")
        return srt

    # ---------- วิดีโอ / mux

    def video_args(self, job: Job) -> tuple[list[str], bool]:
        s = self.s
        preset = VIDEO_PRESETS[s.video_preset]
        if preset is None and not s.deinterlace:
            args = ["-c:v", "copy"]
            if job.vcodec == "hevc":
                args += ["-tag:v", "hvc1"]
            return args, False

        double = s.deinterlace and s.deint_rate == "send_field"
        gop = int(round(job.fps * (2 if double else 1) * 15))
        params = preset or "crf=23"
        if re.search(r"(^|:)keyint=\d+", params):
            params = re.sub(r"(^|:)keyint=\d+", rf"\g<1>keyint={gop}", params)
        else:
            params += f":keyint={gop}"

        args = [
            "-c:v", "libx265", "-preset", s.x265_speed,
            "-pix_fmt", s.pix_fmt, "-x265-params", params, "-tag:v", "hvc1",
        ]
        if s.deinterlace:
            args += [
                "-vf",
                f"{s.deint_algo}=mode={s.deint_rate}:parity={s.deint_parity}:deint=all",
            ]
        self.emit(f"  x265: preset={s.x265_speed} · GOP={gop} ({job.fps:.3f} fps × 15 s"
                  f"{' × 2' if double else ''}) · {s.pix_fmt}")
        return args, True

    def mux(self, job: Job, outputs: list[dict], target: Path,
            temp: Path, use_faststart: bool) -> None:
        video_args, encoding = self.video_args(job)
        command = self.ffmpeg_base("info" if encoding else "warning")
        command += ["-i", str(job.path)]

        for item in outputs:
            offset = item["track"].offset
            if abs(offset) >= 0.001:
                command += ["-itsoffset", f"{offset:.3f}"]
            command += ["-i", str(item["path"])]

        chapter_input = None
        if job.include_chapters and job.chapters:
            metadata = temp / "chapters.ffmeta"
            write_ffmetadata(metadata, job.chapters, job.duration)
            chapter_input = len(outputs) + 1
            command += ["-f", "ffmetadata", "-i", str(metadata)]

        command += ["-map", f"0:{job.video_index}"]
        for number in range(len(outputs)):
            command += ["-map", f"{number + 1}:a:0"]
        command += [
            "-map_metadata", "0",
            "-map_chapters", str(chapter_input) if chapter_input is not None else "-1",
        ]
        command += video_args + ["-c:a", "copy"]

        for number, item in enumerate(outputs):
            flags = (["default"] if item["default"] else []) + item["track"].flags
            command += [
                f"-metadata:s:a:{number}", f"language={item['track'].lang}",
                f"-metadata:s:a:{number}", f"title={item['title']}",
                f"-disposition:a:{number}", "+".join(flags) or "0",
            ]

        if use_faststart:
            command += ["-movflags", "+faststart"]
        command += ["-f", "mp4", str(target)]  # ไม่มี frag_* → ไม่ใช่ fMP4

        label = "x265 + Mux MP4" if encoding else "Mux MP4"
        self.run(command, label, job.duration)

    # ---------- ตรวจผลลัพธ์

    def verify(self, path: Path, audio_count: int, sub_count: int, chapter_count: int) -> None:
        data = json.loads(run_capture(
            [self.ffprobe, "-v", "error", "-show_entries", "stream=codec_type",
             "-show_chapters", "-of", "json", str(path)],
            "ตรวจไฟล์ผลลัพธ์",
        ))
        types = [s.get("codec_type") for s in data.get("streams") or []]
        if types.count("video") < 1:
            raise AppError("ผลลัพธ์ไม่มี video stream")
        if types.count("audio") != audio_count:
            raise AppError(f"แทร็กเสียงไม่ครบ: ต้องมี {audio_count} พบ {types.count('audio')}")
        if types.count("subtitle") < sub_count:
            raise AppError(f"แทร็ก TX3G ไม่ครบ: ต้องมี {sub_count} พบ {types.count('subtitle')}")
        found_chapters = len(data.get("chapters") or [])
        if found_chapters != chapter_count:
            self.emit(f"[WARN] Chapters ในผลลัพธ์ {found_chapters} บท (ตั้งไว้ {chapter_count})")
        layout = "moov อยู่หน้าไฟล์" if moov_before_mdat(path) else "moov อยู่ท้ายไฟล์"
        self.emit(f"  ตรวจแล้ว: video ✓ · audio {audio_count} · TX3G {sub_count} · "
                  f"chapters {found_chapters} · {layout}")

    # ---------- แปลงหนึ่งไฟล์

    def convert(self, job: Job) -> Path:
        s = self.s
        source = job.path
        out_dir = Path(s.output_dir) if s.output_dir else source.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"{source.stem}{s.suffix}.mp4"

        if output.resolve() == source.resolve():
            raise AppError("ชื่อผลลัพธ์ซ้ำกับต้นฉบับ — เปลี่ยน suffix หรือโฟลเดอร์ผลลัพธ์")
        if output.exists() and not s.overwrite:
            raise AppError(f"มีไฟล์ผลลัพธ์อยู่แล้ว: {output}")

        tracks = [t for t in job.audio if t.include]
        if not tracks:
            raise AppError("ไม่ได้เลือกแทร็กเสียงใดเลย")
        subs = [t for t in job.subs if t.include and t.text_based] if s.subs_enabled else []
        if subs and self.tx3g is None:
            raise AppError("ไม่ได้โหลด tx3g_apple.py")

        dialnorm = self.read_dialnorm(source) if self.mediainfo else {}
        default_track = pick_default(tracks, s.default_lang)

        method = s.faststart
        if method == "auto":
            method = "mp4box" if self.mp4box else "native"

        temp = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=out_dir))
        try:
            outputs: list[dict] = []

            for number, track in enumerate(tracks, 1):
                self.check_cancel()
                self.emit(
                    f"■ เสียง {number}/{len(tracks)}: #{track.ordinal} {track.lang} · "
                    f"{track.codec} {track.channels}ch {track.layout or '?'} · "
                    f"{track.title or 'ไม่มีชื่อ'}"
                )
                gain = 0.0
                if track.ordinal in dialnorm:
                    gain = float(abs(dialnorm[track.ordinal]) - 31)
                    self.emit(f"  Dialnorm {dialnorm[track.ordinal]} → ชดเชย {gain:+.0f} dB")
                if abs(track.offset) >= 0.001:
                    self.emit(f"  Audio delay {track.offset * 1000:+.0f} ms (ชดเชยตอน mux)")

                pcm = temp / f"a{number}_pcm.wav"
                self.extract_pcm(source, track, pcm, gain, job.duration)
                cache: dict[tuple[str, str], tuple[Path, str, int]] = {}

                def prepared(mode: str, matrix_type: str):
                    key = (mode, matrix_type if mode == "matrix" else "")
                    if key not in cache:
                        target = temp / f"a{number}_{mode}_{key[1] or 'x'}.wav"
                        cache[key] = self.prepare_audio(
                            pcm, track, mode, matrix_type, target, job.duration
                        )
                    return cache[key]

                if s.use_dolby:
                    path, kind, channels = prepared(s.dolby_mode, s.dolby_matrix)
                    dolby = temp / f"a{number}_dolby.{s.dolby_format}"
                    self.encode_dolby(path, dolby, kind, channels, temp)
                    outputs.append({
                        "path": dolby, "track": track, "default": False,
                        "title": self.track_title(track, kind, channels, "dolby"),
                    })

                if s.use_aac:
                    path, kind, channels = prepared(s.aac_mode, s.aac_matrix)
                    aac = temp / f"a{number}_aac.m4a"
                    self.encode_aac(path, aac, job.duration)
                    outputs.append({
                        "path": aac, "track": track, "default": False,
                        "title": self.track_title(track, kind, channels, "aac"),
                    })

                # ลบ WAV ขนาดใหญ่ทันทีเพื่อประหยัดพื้นที่
                for item in [pcm] + [p for p, _, _ in cache.values()]:
                    with contextlib.suppress(OSError):
                        item.unlink()

            for item in outputs:
                if item["track"] is default_track:
                    item["default"] = True
                    break

            self.emit("■ Mux วิดีโอ + เสียง + Chapters")
            muxed = temp / "muxed.mp4"
            self.mux(job, outputs, muxed, temp,
                     use_faststart=(method == "native" and not subs))
            current = muxed

            for number, sub in enumerate(subs, 1):
                self.check_cancel()
                self.emit(f"■ TX3G {number}/{len(subs)}: ซับ #{sub.ordinal} ภาษา {sub.lang}"
                          f"{' [Forced]' if sub.forced else ''}")
                srt = self.prepare_srt(job, sub, temp, number)
                next_file = temp / f"tx3g_{number}.mp4"

                def on_progress(percent, message, _n=number):
                    self.post("progress", (float(percent), f"TX3G {_n}: {message}"))
                    if int(percent) in (25, 55, 85, 100):
                        self.emit(f"  [TX3G] {percent}% {message}")

                with self.capture("TX3G"):
                    ok = self.tx3g.embed_tx3g_subtitles_native(
                        video_path=str(current),
                        srt_path=str(srt),
                        output_path=str(next_file),
                        lang_code=sub.lang,
                        progress_cb=on_progress,
                    )
                if ok is not True or not next_file.is_file() or next_file.stat().st_size == 0:
                    raise AppError(f"ฝัง TX3G ซับ #{sub.ordinal} ไม่สำเร็จ")
                with contextlib.suppress(OSError):
                    current.unlink()
                current = next_file

            if method != "off":
                if method == "mp4box":
                    target = temp / "mp4box.mp4"
                    self.run([self.mp4box, "-inter", "500", str(current), "-out", str(target)],
                             "MP4Box -inter 500 (FastStart)")
                    current = target
                elif not moov_before_mdat(current):
                    target = temp / "faststart.mp4"
                    self.emit("▶ Native FastStart: ย้าย moov มาหน้าไฟล์ (ไม่ remux)")
                    faststart_native(current, target)
                    current = target
                if not moov_before_mdat(current):
                    self.emit("[WARN] moov ยังไม่อยู่หน้าไฟล์ — HTTP FastStart อาจไม่ทำงาน")

            chapter_count = len(job.chapters) if job.include_chapters else 0
            self.verify(current, len(outputs), len(subs), chapter_count)

            if output.exists() and not s.overwrite:
                raise AppError(f"มีไฟล์ผลลัพธ์ถูกสร้างระหว่างทำงาน: {output}")
            os.replace(current, output)
            return output
        finally:
            shutil.rmtree(temp, ignore_errors=True)


def loro_filter(track: AudioTrack) -> str | None:
    """สูตร Lo/Ro จาก V2"""
    channels, layout = track.channels, track.layout.lower()
    if channels == 8 and layout.startswith("7.1"):
        return ("pan=stereo|FL=0.37*FL+0.26*FC+0.26*BL+0.26*SL|"
                "FR=0.37*FR+0.26*FC+0.26*BR+0.26*SR")
    if channels in (5, 6) and layout.startswith(("5.0", "5.1")):
        left, right = ("SL", "SR") if "side" in layout else ("BL", "BR")
        return (f"pan=stereo|FL=0.46*FL+0.32*FC+0.32*{left}|"
                f"FR=0.46*FR+0.32*FC+0.32*{right}")
    if channels in (4, 5) and layout.startswith(("quad", "4.0", "4.1")):
        left, right = ("SL", "SR") if "side" in layout else ("BL", "BR")
        return f"pan=stereo|FL=0.5*FL+0.5*{left}|FR=0.5*FR+0.5*{right}"
    return None


# ============================================================ GUI

def combo(parent, row: int, label: str, variable: tk.StringVar, values,
          col: int = 0, width: int = 26, editable: bool = False) -> ttk.Combobox:
    ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=(0, 8), pady=3)
    widget = ttk.Combobox(parent, textvariable=variable, values=list(values),
                          width=width, state="normal" if editable else "readonly")
    widget.grid(row=row, column=col + 1, sticky="ew", pady=3, padx=(0, 12))
    parent.columnconfigure(col + 1, weight=1)
    return widget


def make_tree(parent, columns: list[tuple[str, str, int, bool]], height: int = 8) -> ttk.Treeview:
    frame = ttk.Frame(parent)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings",
                        height=height, selectmode="extended")
    for key, heading, width, stretch in columns:
        tree.heading(key, text=heading)
        tree.column(key, width=width, stretch=stretch,
                    anchor="w" if stretch else "center")
    scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.grid(row=0, column=0, sticky="nsew")
    scroll.grid(row=0, column=1, sticky="ns")
    tree.tag_configure("muted", foreground="#999999")
    tree.frame = frame  # type: ignore[attr-defined]
    return tree


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"{APP_NAME} — MP4 · Dolby / AAC · Apple TX3G · Chapters")
        root.geometry("1260x900")
        root.minsize(1060, 740)

        self.events: queue.Queue = queue.Queue()
        self.jobs: dict[int, Job] = {}
        self.ids = itertools.count(1)
        self.current_jid: int | None = None
        self.running = False
        self.cancel_event = threading.Event()
        self.pipeline: Pipeline | None = None

        self._init_vars()
        self._build()
        self._sync_states()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(80, self._drain)

        dnd = "เปิดใช้งาน" if TkinterDnD else "ปิด (ติดตั้ง: pip install tkinterdnd2)"
        self.emit(f"{APP_NAME} พร้อมทำงาน · Drag & Drop: {dnd}")

    # ---------------------------------------------------- ตัวแปร

    def _init_vars(self) -> None:
        S, B = tk.StringVar, tk.BooleanVar
        self.preset_var = S(value="Copy Video Stream")
        self.x265_speed_var = S(value="slow")
        self.pixfmt_var = S(value=next(iter(PIX_FMTS)))
        self.deint_var = B(value=False)
        self.deint_algo_var = S(value="bwdif")
        self.deint_rate_var = S(value=next(iter(DEINT_RATES)))
        self.deint_parity_var = S(value="auto")

        self.use_dolby_var = B(value=True)
        self.dolby_mode_var = S(value="Dolby Matrix 2.0 (Lt/Rt)")
        self.dolby_matrix_var = S(value="Dolby Pro Logic II")
        self.dolby_format_var = S(value=next(iter(DOLBY_FORMATS)))
        self.dolby_rate_var = S(value="192")
        self.dolby_drc_var = S(value="film_light")
        self.dolby_pref_var = S(value=next(iter(PREFERRED_DOWNMIX)))
        self.dolby_center_var = S(value="-3")
        self.dolby_surround_var = S(value="-3")

        self.use_aac_var = B(value=False)
        self.aac_mode_var = S(value="Stereo 2.0 (Lo/Ro)")
        self.aac_matrix_var = S(value="Dolby Pro Logic II")
        self.aac_qaac_var = S(value=next(iter(AAC_MODES)))
        self.aac_rate_var = S(value="192")

        self.dialnorm_var = B(value=True)
        self.headroom_var = S(value=next(iter(HEADROOM)))

        self.audio_scope_var = S(value="ไทยและอังกฤษ")
        self.fb1_var = S(value="eng")
        self.fb2_var = S(value="tha")
        self.default_lang_var = S(value="tha")

        self.subs_enabled_var = B(value=True)
        self.sub_scope_var = S(value="ไทยและอังกฤษ")
        self.sidecar_var = B(value=True)

        self.chap_include_var = B(value=True)

        self.faststart_var = S(value=next(iter(FASTSTART)))
        self.overwrite_var = B(value=False)
        self.suffix_var = S(value=DEFAULT_SUFFIX)
        self.outdir_var = S(value="")

        self.status_var = S(value="พร้อมทำงาน — เพิ่มไฟล์เพื่อเริ่ม")
        self.step_var = S(value="")

        for var in (self.dolby_mode_var, self.aac_mode_var, self.preset_var,
                    self.deint_var, self.use_dolby_var, self.use_aac_var):
            var.trace_add("write", lambda *_: self._sync_states())
        self.suffix_var.trace_add("write", lambda *_: self._update_output_example())

    # ---------------------------------------------------- โครง UI

    def _build(self) -> None:
        style = ttk.Style(self.root)
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Title.TLabel", font=("TkDefaultFont", 10, "bold"))
        style.configure("Treeview", rowheight=24)

        footer = ttk.Frame(self.root, padding=(10, 8))
        footer.pack(side="bottom", fill="x")
        self._build_footer(footer)

        vertical = ttk.PanedWindow(self.root, orient="vertical")
        vertical.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        horizontal = ttk.PanedWindow(vertical, orient="horizontal")
        console = ttk.LabelFrame(vertical, text="Console — log การประมวลผล", padding=4)
        vertical.add(horizontal, weight=4)
        vertical.add(console, weight=2)

        left = ttk.LabelFrame(horizontal, text="1. คิวไฟล์", padding=6)
        right = ttk.Frame(horizontal)
        horizontal.add(left, weight=2)
        horizontal.add(right, weight=5)

        self._build_queue(left)
        self._build_tabs(right)
        self._build_console(console)

    def _build_footer(self, parent) -> None:
        parent.columnconfigure(2, weight=1)
        self.start_button = ttk.Button(parent, text="▶  เริ่มแปลง", width=16, command=self._start)
        self.start_button.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(0, 6))
        self.stop_button = ttk.Button(parent, text="■  หยุด", width=10,
                                      command=self._stop, state="disabled")
        self.stop_button.grid(row=0, column=1, rowspan=2, sticky="ns", padx=(0, 12))
        self.overall_bar = ttk.Progressbar(parent, maximum=100)
        self.overall_bar.grid(row=0, column=2, sticky="ew")
        ttk.Label(parent, textvariable=self.status_var, width=55).grid(
            row=0, column=3, sticky="w", padx=10)
        self.step_bar = ttk.Progressbar(parent, maximum=100)
        self.step_bar.grid(row=1, column=2, sticky="ew", pady=(5, 0))
        ttk.Label(parent, textvariable=self.step_var, width=55, style="Hint.TLabel").grid(
            row=1, column=3, sticky="w", padx=10, pady=(5, 0))

    def _build_queue(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        for text, command in (
            ("+ ไฟล์", self._pick_files), ("+ โฟลเดอร์", self._pick_folder),
            ("ลบ", self._remove_selected), ("ล้างคิว", self._clear_queue),
        ):
            ttk.Button(bar, text=text, command=command).pack(side="left", padx=(0, 4))

        self.queue_tree = make_tree(parent, [
            ("file", "ไฟล์", 220, True), ("audio", "เสียง", 50, False),
            ("subs", "ซับ", 50, False), ("chap", "บท", 45, False),
            ("status", "สถานะ", 85, False),
        ], height=12)
        self.queue_tree.frame.grid(row=1, column=0, sticky="nsew")
        self.queue_tree.bind("<<TreeviewSelect>>", self._on_queue_select)
        self.queue_tree.bind("<Delete>", lambda _e: self._remove_selected())

        hint = ("ลากไฟล์หรือโฟลเดอร์มาวางในรายการนี้ได้" if TkinterDnD
                else "ติดตั้ง tkinterdnd2 เพื่อใช้ลากไฟล์มาวาง")
        ttk.Label(parent, text=hint + "\nเลือกไฟล์ในคิวเพื่อแก้แทร็กเสียง ซับ และ Chapters ของไฟล์นั้น",
                  style="Hint.TLabel", wraplength=360, justify="left").grid(
            row=2, column=0, sticky="w", pady=(6, 0))

        if TkinterDnD is not None:
            for widget in (self.queue_tree, parent):
                try:
                    widget.drop_target_register(DND_FILES)
                    widget.dnd_bind("<<Drop>>", self._on_drop)
                except (AttributeError, tk.TclError):
                    pass

    def _build_tabs(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        self.selected_info = ttk.Label(parent, style="Title.TLabel",
                                       text="ยังไม่ได้เลือกไฟล์")
        self.selected_info.grid(row=0, column=0, sticky="w", pady=(0, 6))

        self.notebook = ttk.Notebook(parent)
        self.notebook.grid(row=1, column=0, sticky="nsew")
        builders = (
            ("2. Encoder เสียง", self._tab_audio_encoder),
            ("3. แทร็กเสียง", self._tab_audio_tracks),
            ("4. ซับไตเติล", self._tab_subtitles),
            ("5. Chapters", self._tab_chapters),
            ("6. วิดีโอ", self._tab_video),
            ("7. ผลลัพธ์", self._tab_output),
        )
        for title, builder in builders:
            tab = ttk.Frame(self.notebook, padding=12)
            self.notebook.add(tab, text=title)
            builder(tab)

    # ---- แท็บ Encoder เสียง

    def _tab_audio_encoder(self, tab) -> None:
        tab.columnconfigure((0, 1), weight=1)

        dolby = ttk.LabelFrame(tab, text="Dolby (DEE)", padding=10)
        dolby.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        ttk.Checkbutton(dolby, text="สร้างแทร็ก Dolby (ค่าเริ่มต้น)",
                        variable=self.use_dolby_var).grid(row=0, column=0, columnspan=2,
                                                         sticky="w", pady=(0, 6))
        self.dolby_widgets = {
            "mode": combo(dolby, 1, "ช่องเสียง", self.dolby_mode_var, AUDIO_MODES),
            "matrix": combo(dolby, 2, "ชนิด Matrix", self.dolby_matrix_var, MATRIX_TYPES),
            "format": combo(dolby, 3, "Codec", self.dolby_format_var, DOLBY_FORMATS),
            "rate": combo(dolby, 4, "Bitrate (kbps)", self.dolby_rate_var, DOLBY_RATES),
            "drc": combo(dolby, 5, "DRC profile", self.dolby_drc_var, DRC_PROFILES),
        }
        ttk.Label(dolby, text="Metadata เมื่อเลือก Raw 5.1", style="Title.TLabel").grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.dolby_widgets.update({
            "pref": combo(dolby, 7, "Preferred downmix", self.dolby_pref_var, PREFERRED_DOWNMIX),
            "center": combo(dolby, 8, "Center mix (dB)", self.dolby_center_var, CENTER_LEVELS),
            "surround": combo(dolby, 9, "Surround mix (dB)", self.dolby_surround_var, SURROUND_LEVELS),
        })

        aac = ttk.LabelFrame(tab, text="AAC (qaac)", padding=10)
        aac.grid(row=0, column=1, sticky="nsew")
        ttk.Checkbutton(aac, text="สร้างแทร็ก AAC", variable=self.use_aac_var).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.aac_widgets = {
            "mode": combo(aac, 1, "ช่องเสียง", self.aac_mode_var, AUDIO_MODES),
            "matrix": combo(aac, 2, "ชนิด Matrix", self.aac_matrix_var, MATRIX_TYPES),
            "qaac": combo(aac, 3, "โหมด qaac", self.aac_qaac_var, AAC_MODES),
            "rate": combo(aac, 4, "Bitrate (kbps)", self.aac_rate_var, AAC_RATES),
        }
        ttk.Label(aac, text="TVBR: ค่า bitrate ใช้แปลงเป็นระดับคุณภาพ (ประมาณ)",
                  style="Hint.TLabel", wraplength=330).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))

        common = ttk.LabelFrame(tab, text="ตั้งค่าร่วม", padding=10)
        common.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(common, text="ชดเชย Dialnorm ของต้นฉบับ (ต้องมี MediaInfo) — แบบ V2",
                        variable=self.dialnorm_var).grid(row=0, column=0, columnspan=2, sticky="w")
        combo(common, 1, "Headroom ของ Matrix", self.headroom_var, HEADROOM)
        ttk.Label(
            common, style="Hint.TLabel", wraplength=820, justify="left",
            text=("• Dolby และ AAC เลือกโหมดช่องเสียงและชนิด Matrix แยกกัน; ถ้าเลือกเหมือนกัน "
                  "จะสร้าง PCM ครั้งเดียวแล้วใช้ร่วมกัน\n"
                  "• Passthrough Raw 5.1 = คงเสียง 6 ช่อง (7.1 จะรวมเหลือ 5.1) แล้วเข้ารหัสใหม่ "
                  "ไม่ใช่การคัดลอก bitstream เดิม\n"
                  "• ต้นฉบับ 2.0 จะไม่ถูกทำ Matrix ซ้ำ"),
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

    # ---- แท็บแทร็กเสียง

    def _tab_audio_tracks(self, tab) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        rules = ttk.LabelFrame(tab, text="กฎเลือกแทร็กเสียงอัตโนมัติ", padding=8)
        rules.grid(row=0, column=0, sticky="ew")
        combo(rules, 0, "เลือกแทร็ก", self.audio_scope_var, SCOPES, col=0, width=18)
        combo(rules, 0, "แทร็ก default", self.default_lang_var, LANG_CHOICES, col=2, width=10)
        combo(rules, 1, "แทร็ก 1 ไม่มีภาษา →", self.fb1_var, LANG_CHOICES, col=0, width=18)
        combo(rules, 1, "แทร็ก 2 ไม่มีภาษา →", self.fb2_var, LANG_CHOICES, col=2, width=10)
        buttons = ttk.Frame(rules)
        buttons.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(buttons, text="ใช้กฎกับไฟล์นี้",
                   command=lambda: self._apply_rules_to(False)).pack(side="left")
        ttk.Button(buttons, text="ใช้กฎกับทุกไฟล์",
                   command=lambda: self._apply_rules_to(True)).pack(side="left", padx=6)

        self.audio_tree = make_tree(tab, [
            ("use", "ใช้", 40, False), ("no", "#", 35, False),
            ("lang", "ภาษา", 60, False), ("codec", "Codec", 70, False),
            ("ch", "ช่องเสียง", 110, False), ("title", "ชื่อเดิม", 200, True),
            ("note", "ที่มา / หมายเหตุ", 200, True),
        ])
        self.audio_tree.frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.audio_tree.bind("<Button-1>", self._on_audio_click)
        self.audio_tree.bind("<Double-1>", self._on_audio_double)
        ttk.Label(tab, style="Hint.TLabel",
                  text="คลิกช่อง ☑ เพื่อเลือก/ไม่เลือก · ดับเบิลคลิกช่อง “ภาษา” เพื่อ override").grid(
            row=2, column=0, sticky="w", pady=(6, 0))

    # ---- แท็บซับ

    def _tab_subtitles(self, tab) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        rules = ttk.LabelFrame(tab, text="ซับไตเติล → Apple TX3G", padding=8)
        rules.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(rules, text="ฝังซับเป็น Apple TX3G", variable=self.subs_enabled_var).grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(rules, text="หา SRT ข้างไฟล์อัตโนมัติ (ชื่อไฟล์.th.srt)",
                        variable=self.sidecar_var).grid(row=0, column=2, columnspan=2, sticky="w")
        combo(rules, 1, "เลือกแทร็ก", self.sub_scope_var, SCOPES, width=18)
        buttons = ttk.Frame(rules)
        buttons.grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        ttk.Button(buttons, text="ใช้กฎกับไฟล์นี้",
                   command=lambda: self._apply_rules_to(False)).pack(side="left")
        ttk.Button(buttons, text="ใช้กฎกับทุกไฟล์",
                   command=lambda: self._apply_rules_to(True)).pack(side="left", padx=6)
        ttk.Button(buttons, text="+ เพิ่ม SRT ภายนอก",
                   command=self._add_external_sub).pack(side="left", padx=(18, 0))
        ttk.Button(buttons, text="ลบ SRT ภายนอกที่เลือก",
                   command=self._remove_external_sub).pack(side="left", padx=6)

        self.sub_tree = make_tree(tab, [
            ("use", "ใช้", 40, False), ("no", "#", 50, False),
            ("lang", "ภาษา", 60, False), ("type", "ชนิด", 110, False),
            ("forced", "Forced", 60, False), ("title", "ชื่อ", 220, True),
            ("source", "ที่มา", 150, True),
        ])
        self.sub_tree.frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.sub_tree.bind("<Button-1>", self._on_sub_click)
        self.sub_tree.bind("<Double-1>", self._on_sub_double)
        ttk.Label(
            tab, style="Hint.TLabel", wraplength=820, justify="left",
            text=("คลิก ☑ / Forced เพื่อสลับ · ดับเบิลคลิกภาษาเพื่อแก้\n"
                  "SRT ภายนอกถูกส่งเข้า tx3g ตรง ๆ จึงรักษา X1:0 และ !!! ในบรรทัดเวลาได้ · "
                  "SRT ในไฟล์ใช้ stream copy (คง {\\an8}) · ASS แปลง Alignment/{\\anX}/<i><b><u>\n"
                  "ซับภาพ (PGS/VobSub) แปลงเป็น TX3G ไม่ได้ จึงถูกข้าม"),
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))

    # ---- แท็บ Chapters

    def _tab_chapters(self, tab) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        ttk.Checkbutton(tab, text="ใส่ Chapters ในไฟล์ผลลัพธ์ (เฉพาะไฟล์ที่เลือก)",
                        variable=self.chap_include_var,
                        command=self._on_chap_include).grid(row=0, column=0, sticky="w")

        self.chap_tree = make_tree(tab, [
            ("no", "#", 45, False), ("start", "เริ่ม (HH:MM:SS.mmm)", 160, False),
            ("title", "ชื่อบท", 400, True),
        ], height=10)
        self.chap_tree.frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self.chap_tree.bind("<Double-1>", self._on_chap_double)
        self.chap_tree.bind("<Delete>", lambda _e: self._chap_delete())

        side = ttk.Frame(tab)
        side.grid(row=1, column=1, sticky="n", padx=(8, 0), pady=(8, 0))
        for text, command in (
            ("+ เพิ่มบท", self._chap_add), ("ลบบทที่เลือก", self._chap_delete),
            ("ตั้งชื่อใหม่ Chapter 01…", self._chap_renumber),
            ("โหลดจากต้นฉบับใหม่", self._chap_reset),
            ("นำเข้า OGM / CSV…", self._chap_import), ("ส่งออก OGM…", self._chap_export),
        ):
            ttk.Button(side, text=text, command=command, width=24).pack(fill="x", pady=2)

        ttk.Label(tab, style="Hint.TLabel",
                  text="ดับเบิลคลิกเวลา/ชื่อเพื่อแก้ · บทจะเรียงตามเวลาอัตโนมัติ · "
                       "CSV แบบ HandBrake (1,ชื่อ) ใช้เปลี่ยนชื่อบท").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

    # ---- แท็บวิดีโอ

    def _tab_video(self, tab) -> None:
        tab.columnconfigure(0, weight=1)
        box = ttk.LabelFrame(tab, text="วิดีโอ", padding=10)
        box.grid(row=0, column=0, sticky="ew")
        self.preset_combo = combo(box, 0, "Preset", self.preset_var, VIDEO_PRESETS, width=48)
        self.video_widgets = {
            "speed": combo(box, 1, "x265 speed", self.x265_speed_var, X265_SPEEDS),
            "pix": combo(box, 2, "Pixel format", self.pixfmt_var, PIX_FMTS),
        }
        self.preset_detail = ttk.Label(box, style="Hint.TLabel", wraplength=780, justify="left")
        self.preset_detail.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        deint = ttk.LabelFrame(tab, text="Deinterlace", padding=10)
        deint.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Checkbutton(deint, text="เปิด Deinterlace (บังคับเข้ารหัส x265)",
                        variable=self.deint_var).grid(row=0, column=0, columnspan=2, sticky="w")
        self.deint_widgets = {
            "algo": combo(deint, 1, "Algorithm", self.deint_algo_var, ["bwdif", "yadif"]),
            "rate": combo(deint, 2, "Output rate", self.deint_rate_var, DEINT_RATES),
            "parity": combo(deint, 3, "Field order", self.deint_parity_var, ["auto", "tff", "bff"]),
        }
        ttk.Label(tab, style="Hint.TLabel",
                  text="GOP (keyint) คำนวณอัตโนมัติ = fps × 15 วินาที (×2 เมื่อ send_field) เหมือน V2 · "
                       "HEVC ได้ tag hvc1 สำหรับอุปกรณ์ Apple").grid(
            row=2, column=0, sticky="w", pady=(8, 0))

    # ---- แท็บผลลัพธ์

    def _tab_output(self, tab) -> None:
        tab.columnconfigure(0, weight=1)
        box = ttk.LabelFrame(tab, text="ไฟล์ผลลัพธ์ (MP4 เท่านั้น)", padding=10)
        box.grid(row=0, column=0, sticky="ew")
        combo(box, 0, "HTTP FastStart", self.faststart_var, FASTSTART, width=40)

        ttk.Label(box, text="ต่อท้ายชื่อ").grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(box, textvariable=self.suffix_var, width=24).grid(
            row=1, column=1, sticky="w", pady=3)

        ttk.Label(box, text="โฟลเดอร์ผลลัพธ์").grid(row=2, column=0, sticky="w", pady=3)
        folder = ttk.Frame(box)
        folder.grid(row=2, column=1, sticky="ew", pady=3)
        folder.columnconfigure(0, weight=1)
        ttk.Entry(folder, textvariable=self.outdir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(folder, text="เลือก…", command=self._pick_outdir).grid(row=0, column=1, padx=4)
        ttk.Button(folder, text="ข้างต้นฉบับ",
                   command=lambda: self.outdir_var.set("")).grid(row=0, column=2)

        ttk.Checkbutton(box, text="เขียนทับไฟล์ผลลัพธ์ที่มีอยู่",
                        variable=self.overwrite_var).grid(row=3, column=0, columnspan=2,
                                                          sticky="w", pady=(6, 0))
        self.output_example = ttk.Label(box, style="Hint.TLabel")
        self.output_example.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._update_output_example()

        ttk.Label(
            tab, style="Hint.TLabel", wraplength=820, justify="left",
            text=("• MP4 แบบ non-fragmented (ไม่มี moof) เสมอ\n"
                  "• tx3g_apple วาง moov ไว้ท้ายไฟล์ → โปรแกรมย้ายกลับมาหน้าไฟล์หลังฝังซับ "
                  "(Native = ย้าย moov และแก้ stco/co64; MP4Box = interleave 500 ms)\n"
                  "• tx3g_apple อ่านไฟล์ทั้งไฟล์เข้า RAM ระหว่างฝังซับ"),
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))

    def _build_console(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        font = ("Consolas", 9) if os.name == "nt" else ("Courier", 10)
        self.console = tk.Text(parent, height=10, wrap="none", font=font, state="disabled",
                               background="#15171a", foreground="#d8dee9",
                               insertbackground="#d8dee9")
        yscroll = ttk.Scrollbar(parent, orient="vertical", command=self.console.yview)
        xscroll = ttk.Scrollbar(parent, orient="horizontal", command=self.console.xview)
        self.console.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.console.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        for tag, color in (("error", "#ff6b6b"), ("warn", "#ffd166"),
                           ("ok", "#7bd88f"), ("cmd", "#7aa2f7"), ("head", "#c0caf5")):
            self.console.tag_configure(tag, foreground=color)

        side = ttk.Frame(parent)
        side.grid(row=0, column=2, sticky="n", padx=(6, 0))
        ttk.Button(side, text="ล้าง", command=self._clear_console).pack(fill="x")
        ttk.Button(side, text="บันทึก log", command=self._save_console).pack(fill="x", pady=4)

    # ---------------------------------------------------- สถานะ widget

    def _sync_states(self) -> None:
        if not hasattr(self, "aac_widgets"):
            return

        def set_state(widget, enabled: bool) -> None:
            widget.configure(state="readonly" if enabled else "disabled")

        dolby_on = self.use_dolby_var.get()
        dolby_mode = AUDIO_MODES.get(self.dolby_mode_var.get())
        for key, widget in self.dolby_widgets.items():
            enabled = dolby_on
            if key == "matrix":
                enabled = dolby_on and dolby_mode == "matrix"
            elif key in ("pref", "center", "surround"):
                enabled = dolby_on and dolby_mode == "raw51"
            set_state(widget, enabled)

        aac_on = self.use_aac_var.get()
        aac_mode = AUDIO_MODES.get(self.aac_mode_var.get())
        for key, widget in self.aac_widgets.items():
            enabled = aac_on and (key != "matrix" or aac_mode == "matrix")
            set_state(widget, enabled)

        preset = VIDEO_PRESETS.get(self.preset_var.get())
        encoding = preset is not None or self.deint_var.get()
        for widget in self.video_widgets.values():
            set_state(widget, encoding)
        for widget in self.deint_widgets.values():
            set_state(widget, self.deint_var.get())
        self.preset_detail.configure(
            text=("คัดลอก video stream เดิม ไม่เข้ารหัสใหม่" if preset is None
                  and not self.deint_var.get()
                  else f"x265-params: {preset or 'crf=23 (ใช้เมื่อ Deinterlace)'}")
        )

    def _update_output_example(self) -> None:
        if hasattr(self, "output_example"):
            suffix = self.suffix_var.get().strip()
            self.output_example.configure(text=f"ตัวอย่าง: Movie.mkv → Movie{suffix}.mp4")

    # ---------------------------------------------------- Console / events

    def emit(self, text: str) -> None:
        stamped = f"{time.strftime('%H:%M:%S')} {text}" if text else ""
        self.events.put(("log", stamped))
        if sys.__stdout__ is not None:
            try:
                print(stamped, file=sys.__stdout__, flush=True)
            except (OSError, UnicodeEncodeError):
                pass

    def _append_console(self, text: str) -> None:
        tag = ()
        if "[ERROR]" in text:
            tag = ("error",)
        elif "[WARN]" in text:
            tag = ("warn",)
        elif "[OK]" in text:
            tag = ("ok",)
        elif "  $ " in text:
            tag = ("cmd",)
        elif "══" in text or "■" in text or "▶" in text:
            tag = ("head",)
        self.console.configure(state="normal")
        self.console.insert("end", text + "\n", tag)
        lines = int(self.console.index("end-1c").split(".")[0])
        if lines > 8000:
            self.console.delete("1.0", f"{lines - 6000}.0")
        self.console.see("end")
        self.console.configure(state="disabled")

    def _drain(self) -> None:
        try:
            for _ in range(400):
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self._append_console(data)
                elif kind == "progress":
                    percent, text = data
                    if percent is not None:
                        self.step_bar["value"] = percent
                    if text:
                        self.step_var.set(text[:90])
                elif kind == "overall":
                    value, text = data
                    self.overall_bar["value"] = value
                    self.status_var.set(text[:80])
                elif kind == "job_status":
                    jid, status = data
                    if jid in self.jobs:
                        self.jobs[jid].status = status
                        self._refresh_queue_row(self.jobs[jid])
                elif kind == "job_ready":
                    self._on_job_ready(data)
                elif kind == "job_failed":
                    jid, message = data
                    if self.queue_tree.exists(str(jid)):
                        self.queue_tree.set(str(jid), "status", "อ่านไม่ได้")
                    self._append_console(f"[ERROR] {message}")
                elif kind == "done":
                    self._on_done(data)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _clear_console(self) -> None:
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _save_console(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".log",
                                            filetypes=[("Log", "*.log *.txt")])
        if path:
            write_text_utf8(Path(path), self.console.get("1.0", "end"))

    # ---------------------------------------------------- คิวไฟล์

    def _pick_files(self) -> None:
        paths = filedialog.askopenfilenames(filetypes=[
            ("Video", " ".join(f"*{e}" for e in sorted(VIDEO_EXTENSIONS))),
            ("All files", "*.*"),
        ])
        if paths:
            self.add_paths([Path(p) for p in paths])

    def _pick_folder(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.add_paths([Path(path)])

    def _pick_outdir(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self.outdir_var.set(path)

    def _on_drop(self, event):
        self.add_paths([Path(p) for p in self.root.tk.splitlist(event.data)])
        return event.action

    def add_paths(self, paths: list[Path]) -> None:
        suffix = self.suffix_var.get().strip() or DEFAULT_SUFFIX
        files: list[Path] = []
        for path in paths:
            if path.is_dir():
                files += sorted(
                    p for p in path.rglob("*")
                    if p.is_file()
                    and p.suffix.lower() in VIDEO_EXTENSIONS
                    and not p.stem.endswith(suffix)
                    and not any(part.startswith(TEMP_PREFIX) for part in p.parts)
                )
            elif path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
                files.append(path)

        known = {job.path.resolve() for job in self.jobs.values()}
        known |= {Path(self.queue_tree.item(i, "text") or "").resolve()
                  for i in self.queue_tree.get_children() if self.queue_tree.item(i, "text")}
        new = []
        for item in files:
            resolved = item.resolve()
            if resolved not in known:
                known.add(resolved)
                new.append(item)
        if not new:
            self.emit("[WARN] ไม่มีไฟล์วิดีโอใหม่ให้เพิ่ม")
            return

        try:
            ffprobe = find_program("ffprobe")
        except AppError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        entries = []
        for item in new:
            jid = next(self.ids)
            entries.append((jid, item))
            self.queue_tree.insert("", "end", iid=str(jid), text=str(item),
                                   values=(item.name, "…", "…", "…", "กำลังอ่าน"))
        self.emit(f"เพิ่ม {len(new)} ไฟล์ — กำลังวิเคราะห์ stream")
        threading.Thread(target=self._analyze_worker, args=(ffprobe, entries),
                         daemon=True).start()

    def _analyze_worker(self, ffprobe: str, entries: list[tuple[int, Path]]) -> None:
        for jid, path in entries:
            try:
                self.events.put(("job_ready", analyze(ffprobe, path, jid)))
            except Exception as exc:
                self.events.put(("job_failed", (jid, f"{path.name}: {exc}")))

    def _rules(self) -> dict:
        return {
            "audio_scope": SCOPES[self.audio_scope_var.get()],
            "fb1": norm_lang(self.fb1_var.get()),
            "fb2": norm_lang(self.fb2_var.get()),
            "sub_scope": SCOPES[self.sub_scope_var.get()],
            "sidecar": self.sidecar_var.get(),
        }

    def _on_job_ready(self, job: Job) -> None:
        if not self.queue_tree.exists(str(job.jid)):
            return  # ถูกลบออกจากคิวระหว่างวิเคราะห์
        apply_rules(job, self._rules())
        self.jobs[job.jid] = job
        self._refresh_queue_row(job)
        self.emit(
            f"วิเคราะห์ {job.path.name}: {job.vcodec} {job.width}x{job.height} "
            f"{job.fps:.3f} fps · เสียง {len(job.audio)} · ซับ {len(job.subs)} · "
            f"บท {len(job.chapters)}"
        )
        if self.current_jid is None:
            self.queue_tree.selection_set(str(job.jid))

    def _refresh_queue_row(self, job: Job) -> None:
        iid = str(job.jid)
        if not self.queue_tree.exists(iid):
            return
        audio = f"{sum(t.include for t in job.audio)}/{len(job.audio)}"
        subs = f"{sum(t.include for t in job.subs)}/{len(job.subs)}"
        chapters = str(len(job.chapters)) if job.include_chapters else "ปิด"
        self.queue_tree.item(iid, values=(job.path.name, audio, subs, chapters, job.status))

    def _on_queue_select(self, _event=None) -> None:
        selection = self.queue_tree.selection()
        self.current_jid = int(selection[0]) if selection else None
        self._refresh_tables()

    def _current_job(self) -> Job | None:
        return self.jobs.get(self.current_jid) if self.current_jid is not None else None

    def _remove_selected(self) -> None:
        if self.running:
            messagebox.showinfo(APP_NAME, "ลบไฟล์ระหว่างแปลงไม่ได้")
            return
        for iid in self.queue_tree.selection():
            self.queue_tree.delete(iid)
            self.jobs.pop(int(iid), None)
        self.current_jid = None
        self._refresh_tables()

    def _clear_queue(self) -> None:
        if self.running:
            return
        self.queue_tree.delete(*self.queue_tree.get_children())
        self.jobs.clear()
        self.current_jid = None
        self._refresh_tables()

    def _apply_rules_to(self, all_jobs: bool) -> None:
        targets = list(self.jobs.values()) if all_jobs else [j for j in [self._current_job()] if j]
        if not targets:
            messagebox.showinfo(APP_NAME, "ยังไม่มีไฟล์ที่วิเคราะห์เสร็จ")
            return
        rules = self._rules()
        for job in targets:
            apply_rules(job, rules)
            self._refresh_queue_row(job)
        self._refresh_tables()
        self.emit(f"ใช้กฎกับ {len(targets)} ไฟล์")

    # ---------------------------------------------------- ตาราง

    @staticmethod
    def _column_at(tree: ttk.Treeview, x: int) -> str | None:
        column = tree.identify_column(x)
        try:
            index = int(column.lstrip("#")) - 1
        except ValueError:
            return None
        columns = tree["columns"]
        return columns[index] if 0 <= index < len(columns) else None

    def _edit_cell(self, tree, item: str, column: str, initial: str,
                   choices: list[str] | None, on_commit) -> None:
        bbox = tree.bbox(item, column)
        if not bbox:
            return
        x, y, width, height = bbox
        widget = ttk.Combobox(tree, values=choices) if choices else ttk.Entry(tree)
        widget.insert(0, initial)
        widget.place(x=x, y=y, width=max(width, 130), height=height)
        widget.focus_set()
        widget.select_range(0, "end")
        finished = False

        def commit(_event=None):
            nonlocal finished
            if finished:
                return
            finished = True
            value = widget.get()
            widget.destroy()
            on_commit(value)

        def cancel(_event=None):
            nonlocal finished
            finished = True
            widget.destroy()

        def check_focus():
            if finished or not widget.winfo_exists():
                return
            try:
                focus = self.root.focus_get()
            except (KeyError, tk.TclError):
                return  # focus อยู่ใน popdown ของ combobox
            if focus is None or not str(focus).startswith(str(widget)):
                commit()

        widget.bind("<Return>", commit)
        widget.bind("<KP_Enter>", commit)
        widget.bind("<Escape>", cancel)
        widget.bind("<FocusOut>", lambda _e: self.root.after(150, check_focus))
        if choices:
            widget.bind("<<ComboboxSelected>>", commit)

    def _refresh_tables(self) -> None:
        for tree in (self.audio_tree, self.sub_tree, self.chap_tree):
            tree.delete(*tree.get_children())
        job = self._current_job()
        if not job:
            self.selected_info.configure(
                text="ยังไม่ได้เลือกไฟล์ — เลือกไฟล์ในคิวทางซ้ายเพื่อดู/แก้แทร็กและ Chapters")
            return

        self.selected_info.configure(
            text=f"{job.path.name}   ·   {job.vcodec} {job.width}×{job.height}   ·   "
                 f"{job.fps:.3f} fps   ·   {fmt_time(job.duration)[:8]}")

        for i, track in enumerate(job.audio):
            notes = [track.lang_source or "ไม่ทราบภาษา"]
            notes += [FLAG_LABELS[f] for f in track.flags if f in FLAG_LABELS]
            if abs(track.offset) >= 0.001:
                notes.append(f"delay {track.offset * 1000:+.0f} ms")
            self.audio_tree.insert(
                "", "end", iid=str(i),
                values=(CHECK if track.include else UNCHECK, track.ordinal, track.lang,
                        track.codec, f"{track.channels} ({track.layout or '?'})",
                        track.title, " · ".join(notes)),
                tags=() if track.include else ("muted",),
            )

        for i, sub in enumerate(job.subs):
            kind = "ข้อความ" if sub.text_based else "ภาพ — ข้าม"
            source = Path(sub.external).name if sub.external else "ในไฟล์"
            self.sub_tree.insert(
                "", "end", iid=str(i),
                values=(CHECK if sub.include else UNCHECK,
                        "ภายนอก" if sub.external else sub.ordinal, sub.lang,
                        f"{sub.codec} · {kind}", CHECK if sub.forced else "",
                        sub.title, source),
                tags=() if sub.include else ("muted",),
            )

        self.chap_include_var.set(job.include_chapters)
        self._refresh_chapters()

    def _refresh_chapters(self) -> None:
        self.chap_tree.delete(*self.chap_tree.get_children())
        job = self._current_job()
        if not job:
            return
        for i, chapter in enumerate(job.chapters):
            self.chap_tree.insert("", "end", iid=str(i),
                                  values=(i + 1, fmt_time(chapter.start), chapter.title),
                                  tags=() if job.include_chapters else ("muted",))
        self._refresh_queue_row(job)

    # ---- แทร็กเสียง

    def _on_audio_click(self, event):
        job = self._current_job()
        row = self.audio_tree.identify_row(event.y)
        if not job or not row or self._column_at(self.audio_tree, event.x) != "use":
            return None
        track = job.audio[int(row)]
        track.include = not track.include
        self._refresh_tables()
        self._refresh_queue_row(job)
        return "break"

    def _on_audio_double(self, event):
        job = self._current_job()
        row = self.audio_tree.identify_row(event.y)
        if not job or not row or self._column_at(self.audio_tree, event.x) != "lang":
            return
        track = job.audio[int(row)]

        def commit(value: str) -> None:
            code = norm_lang(value, "")
            if not code:
                messagebox.showerror(APP_NAME, "รหัสภาษาต้องเป็น ISO 639-2 สามตัวอักษร เช่น tha, eng")
                return
            track.lang, track.lang_source = code, "ผู้ใช้กำหนด"
            self._refresh_tables()

        self._edit_cell(self.audio_tree, row, "lang", track.lang, LANG_CHOICES, commit)

    # ---- ซับ

    def _on_sub_click(self, event):
        job = self._current_job()
        row = self.sub_tree.identify_row(event.y)
        column = self._column_at(self.sub_tree, event.x)
        if not job or not row or column not in ("use", "forced"):
            return None
        sub = job.subs[int(row)]
        if column == "use":
            if not sub.text_based:
                self.emit(f"[WARN] ซับ #{sub.ordinal} ({sub.codec}) เป็นซับภาพ แปลงเป็น TX3G ไม่ได้")
                return "break"
            sub.include = not sub.include
        else:
            sub.forced = not sub.forced
        self._refresh_tables()
        self._refresh_queue_row(job)
        return "break"

    def _on_sub_double(self, event):
        job = self._current_job()
        row = self.sub_tree.identify_row(event.y)
        if not job or not row or self._column_at(self.sub_tree, event.x) != "lang":
            return
        sub = job.subs[int(row)]

        def commit(value: str) -> None:
            code = norm_lang(value, "")
            if not code:
                messagebox.showerror(APP_NAME, "รหัสภาษาต้องเป็น ISO 639-2 สามตัวอักษร")
                return
            sub.lang = code
            self._refresh_tables()

        self._edit_cell(self.sub_tree, row, "lang", sub.lang, LANG_CHOICES, commit)

    def _add_external_sub(self) -> None:
        job = self._current_job()
        if not job:
            messagebox.showinfo(APP_NAME, "เลือกไฟล์ในคิวก่อน")
            return
        paths = filedialog.askopenfilenames(initialdir=str(job.path.parent),
                                            filetypes=[("SubRip", "*.srt")])
        for raw in paths:
            path = Path(raw)
            if any(s.external and Path(s.external) == path for s in job.subs):
                continue
            parts = re.split(r"[._\-\s]+", path.stem.lower())
            lang = next((LANGUAGE_ALIASES[p] for p in reversed(parts) if p in LANGUAGE_ALIASES), "und")
            job.subs.append(SubTrack(
                index=-1, ordinal=len(job.subs) + 1, codec="subrip", title=path.name,
                tag_lang=lang, tag_source="ชื่อไฟล์", lang=lang, include=True,
                forced="forced" in parts, external=str(path),
            ))
        self._refresh_tables()
        self._refresh_queue_row(job)

    def _remove_external_sub(self) -> None:
        job = self._current_job()
        if not job:
            return
        remove = {int(i) for i in self.sub_tree.selection() if job.subs[int(i)].external}
        if not remove:
            messagebox.showinfo(APP_NAME, "เลือกแถวที่เป็น SRT ภายนอกก่อน")
            return
        job.subs = [s for i, s in enumerate(job.subs) if i not in remove]
        self._refresh_tables()
        self._refresh_queue_row(job)

    # ---- Chapters

    def _on_chap_include(self) -> None:
        job = self._current_job()
        if job:
            job.include_chapters = self.chap_include_var.get()
            self._refresh_chapters()

    def _on_chap_double(self, event):
        job = self._current_job()
        row = self.chap_tree.identify_row(event.y)
        column = self._column_at(self.chap_tree, event.x)
        if not job or not row or column not in ("start", "title"):
            return
        chapter = job.chapters[int(row)]

        if column == "title":
            def commit_title(value: str) -> None:
                chapter.title = value.strip() or chapter.title
                self._refresh_chapters()
            self._edit_cell(self.chap_tree, row, "title", chapter.title, None, commit_title)
            return

        def commit_start(value: str) -> None:
            try:
                seconds = parse_time(value)
            except ValueError:
                messagebox.showerror(APP_NAME, "รูปแบบเวลา: HH:MM:SS.mmm หรือ MM:SS หรือวินาที")
                return
            if job.duration and seconds >= job.duration:
                messagebox.showerror(APP_NAME, "เวลาเริ่มเกินความยาววิดีโอ")
                return
            chapter.start = seconds
            job.chapters.sort(key=lambda c: c.start)
            self._refresh_chapters()

        self._edit_cell(self.chap_tree, row, "start", fmt_time(chapter.start), None, commit_start)

    def _chap_add(self) -> None:
        job = self._current_job()
        if not job:
            return
        selection = self.chap_tree.selection()
        base = job.chapters[int(selection[-1])].start if selection else (
            job.chapters[-1].start if job.chapters else -60.0)
        start = base + 60.0
        if job.duration:
            start = min(start, max(0.0, job.duration - 1))
        job.chapters.append(Chapter(max(0.0, start), f"Chapter {len(job.chapters) + 1:02}"))
        job.chapters.sort(key=lambda c: c.start)
        self._refresh_chapters()

    def _chap_delete(self) -> None:
        job = self._current_job()
        if not job:
            return
        remove = {int(i) for i in self.chap_tree.selection()}
        job.chapters = [c for i, c in enumerate(job.chapters) if i not in remove]
        self._refresh_chapters()

    def _chap_renumber(self) -> None:
        job = self._current_job()
        if job:
            for i, chapter in enumerate(job.chapters, 1):
                chapter.title = f"Chapter {i:02}"
            self._refresh_chapters()

    def _chap_reset(self) -> None:
        job = self._current_job()
        if job:
            job.chapters = copy.deepcopy(job.source_chapters)
            self._refresh_chapters()

    def _chap_import(self) -> None:
        job = self._current_job()
        if not job:
            return
        path = filedialog.askopenfilename(filetypes=[("Chapters", "*.txt *.csv"), ("All", "*.*")])
        if not path:
            return
        try:
            chapters, kind = parse_chapter_file(read_text_guess(Path(path)), job.chapters)
        except AppError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        job.chapters = chapters
        self._refresh_chapters()
        self.emit(f"นำเข้า Chapters ({kind}) {len(chapters)} บท")

    def _chap_export(self) -> None:
        job = self._current_job()
        if not job or not job.chapters:
            return
        path = filedialog.asksaveasfilename(defaultextension=".txt",
                                            initialfile=f"{job.path.stem}.chapters.txt")
        if not path:
            return
        lines = []
        for i, chapter in enumerate(job.chapters, 1):
            lines += [f"CHAPTER{i:02}={fmt_time(chapter.start)}",
                      f"CHAPTER{i:02}NAME={chapter.title}"]
        write_text_utf8(Path(path), "\n".join(lines) + "\n")
        self.emit(f"ส่งออก Chapters: {path}")

    # ---------------------------------------------------- เริ่ม / หยุด

    def _settings(self) -> Settings:
        use_dolby, use_aac = self.use_dolby_var.get(), self.use_aac_var.get()
        if not (use_dolby or use_aac):
            raise AppError("ต้องเลือก Dolby, AAC หรือทั้งคู่")
        dolby_format = DOLBY_FORMATS[self.dolby_format_var.get()]
        dolby_rate = int(self.dolby_rate_var.get())
        if use_dolby and dolby_format == "ac3" and dolby_rate > 640:
            raise AppError("AC-3 รองรับ bitrate สูงสุด 640 kbps")

        suffix = self.suffix_var.get().strip()
        if re.search(r'[<>:"/\\|?*]', suffix):
            raise AppError("คำต่อท้ายชื่อไฟล์มีอักขระที่ใช้ไม่ได้")
        outdir = self.outdir_var.get().strip()
        if outdir and not Path(outdir).is_dir():
            raise AppError("ไม่พบโฟลเดอร์ผลลัพธ์")

        return Settings(
            video_preset=self.preset_var.get(),
            x265_speed=self.x265_speed_var.get(),
            pix_fmt=PIX_FMTS[self.pixfmt_var.get()],
            deinterlace=self.deint_var.get(),
            deint_algo=self.deint_algo_var.get(),
            deint_rate=DEINT_RATES[self.deint_rate_var.get()],
            deint_parity=self.deint_parity_var.get(),
            use_dolby=use_dolby,
            dolby_mode=AUDIO_MODES[self.dolby_mode_var.get()],
            dolby_matrix=MATRIX_TYPES[self.dolby_matrix_var.get()],
            dolby_format=dolby_format,
            dolby_rate=dolby_rate,
            dolby_drc=self.dolby_drc_var.get(),
            dolby_pref=PREFERRED_DOWNMIX[self.dolby_pref_var.get()],
            dolby_center=self.dolby_center_var.get(),
            dolby_surround=self.dolby_surround_var.get(),
            use_aac=use_aac,
            aac_mode=AUDIO_MODES[self.aac_mode_var.get()],
            aac_matrix=MATRIX_TYPES[self.aac_matrix_var.get()],
            aac_qaac=AAC_MODES[self.aac_qaac_var.get()],
            aac_rate=int(self.aac_rate_var.get()),
            dialnorm=self.dialnorm_var.get(),
            headroom=HEADROOM[self.headroom_var.get()],
            default_lang=norm_lang(self.default_lang_var.get(), "tha"),
            subs_enabled=self.subs_enabled_var.get(),
            faststart=FASTSTART[self.faststart_var.get()],
            overwrite=self.overwrite_var.get(),
            suffix=suffix,
            output_dir=outdir,
        )

    def _start(self) -> None:
        if self.running:
            return
        ordered = [self.jobs[int(i)] for i in self.queue_tree.get_children() if int(i) in self.jobs]
        if not ordered:
            messagebox.showinfo(APP_NAME, "เพิ่มไฟล์และรอให้วิเคราะห์เสร็จก่อน")
            return
        pending = [job for job in ordered if job.status != "เสร็จ"]
        if not pending:
            if not messagebox.askyesno(APP_NAME, "ทุกไฟล์แปลงเสร็จแล้ว ต้องการแปลงใหม่ทั้งหมดหรือไม่?"):
                return
            pending = ordered

        try:
            settings = self._settings()
            for job in pending:
                starts = [round(c.start, 3) for c in job.chapters]
                if job.include_chapters and len(starts) != len(set(starts)):
                    raise AppError(f"{job.path.name}: มี Chapter เวลาเริ่มซ้ำกัน")
                if not any(t.include for t in job.audio):
                    raise AppError(f"{job.path.name}: ไม่ได้เลือกแทร็กเสียง")
        except (AppError, ValueError, KeyError) as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return

        for job in pending:
            job.status = "รอคิว"
            self._refresh_queue_row(job)

        self.cancel_event.clear()
        self.running = True
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.overall_bar["value"] = 0
        self.step_bar["value"] = 0
        self.status_var.set("กำลังเริ่ม…")
        snapshot = copy.deepcopy(pending)
        threading.Thread(target=self._run_pipeline, args=(settings, snapshot), daemon=True).start()

    def _run_pipeline(self, settings: Settings, jobs: list[Job]) -> None:
        pipeline = Pipeline(settings, self.emit,
                            lambda kind, data: self.events.put((kind, data)),
                            self.cancel_event)
        self.pipeline = pipeline
        try:
            summary = pipeline.run_jobs(jobs)
        except Exception as exc:
            self.emit(f"[ERROR] {exc}\n{traceback.format_exc()}")
            summary = "เกิดข้อผิดพลาด — ดู Console"
        self.events.put(("done", summary))

    def _stop(self) -> None:
        if not self.running:
            return
        self.cancel_event.set()
        pipeline = self.pipeline
        if pipeline and pipeline.proc and pipeline.proc.poll() is None:
            pipeline.proc.terminate()
        self.status_var.set("กำลังหยุด… (helper Python จะหยุดหลังจบขั้นตอนปัจจุบัน)")
        self.emit("[WARN] ส่งคำสั่งหยุด")

    def _on_done(self, summary: str) -> None:
        self.running = False
        self.pipeline = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set(summary)
        self.step_var.set("")
        messagebox.showinfo(APP_NAME, summary)

    def _on_close(self) -> None:
        if self.running and not messagebox.askyesno(APP_NAME, "กำลังแปลงอยู่ ต้องการหยุดและปิดโปรแกรม?"):
            return
        self._stop()
        self.root.destroy()


def main() -> None:
    root = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()