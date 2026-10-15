#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VideoConV3 - MP4, Dolby/AAC, Apple TX3G and FastStart."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import tkinter as tk

from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from types import ModuleType


ROOT = Path(__file__).resolve().parent

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".mov", ".ts", ".mts", ".m2ts", ".webm"
}

TEXT_SUBTITLE_CODECS = {
    "subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"
}

AUDIO_MODES = {
    "Dolby Matrix 2.0 (Lt/Rt)": "matrix",
    "Stereo 2.0 (Lo/Ro)": "loro",
    "Raw 5.1 (ไม่ downmix)": "raw51",
}

MATRIX_MODES = {
    "Dolby Surround": "ds",
    "Dolby Pro Logic": "pl",
    "Dolby Pro Logic II": "pl2",
    "PLII Simple": "pl2s",
}

VIDEO_PRESETS = {
    "Copy video (ไม่เข้ารหัสใหม่)": None,
    "Modern Anime 24fps": (
        "crf=31:qpmin=28:qpmax=51:keyint=360:bframes=5:"
        "b-pyramid=1:ref=5:rc-lookahead=48:lookahead-slices=4:"
        "vbv-maxrate=5000:vbv-bufsize=7500:aq-mode=2:aq-strength=1.15:"
        "psy-rd=0.4:psy-rdoq=0.0:deblock=1,1:open-gop=1:weightp=0"
    ),
    "90s MV Tape / Grain": (
        "crf=31:qpmin=28:qpmax=51:keyint=375:bframes=5:"
        "b-pyramid=1:ref=5:rc-lookahead=40:lookahead-slices=4:"
        "vbv-maxrate=5000:vbv-bufsize=7500:aq-mode=2:aq-strength=0.85:"
        "no-sao=1:psy-rd=1.0:psy-rdoq=1.0:deblock=-1,-1:"
        "open-gop=1:weightp=0"
    ),
    "90s MV Tape / Clean": (
        "crf=32:qpmin=30:qpmax=51:keyint=375:bframes=5:"
        "b-pyramid=1:ref=5:rc-lookahead=32:aq-mode=2:aq-strength=1.0:"
        "sao=1:psy-rd=0.7:deblock=0,0:open-gop=1:weightp=0"
    ),
    "Movie / Film": (
        "crf=32:qpmin=30:qpmax=51:keyint=360:bframes=5:"
        "b-pyramid=1:ref=5:rc-lookahead=32:aq-mode=2:aq-strength=0.9:"
        "no-sao=1:psy-rd=1.0:psy-rdoq=1.0:deblock=-1,-1:"
        "open-gop=1:weightp=0"
    ),
    "Classic Anime": (
        "crf=32:qpmin=30:qpmax=51:keyint=360:bframes=5:"
        "b-pyramid=1:ref=5:rc-lookahead=32:aq-mode=2:aq-strength=1.1:"
        "no-sao=1:psy-rd=0.6:psy-rdoq=0.5:deblock=0,0:"
        "open-gop=1:weightp=0"
    ),
}

LANGUAGE_ALIASES = {
    "th": "tha", "tha": "tha", "thai": "tha",
    "en": "eng", "eng": "eng", "english": "eng",
    "und": "und",
}

ASS_TIME = re.compile(r"^\s*(\d+):(\d{2}):(\d{2})\.(\d{2})\s*$")
ASS_ALIGN = re.compile(r"\\an([1-9])")
ASS_TAGS = re.compile(r"\{[^}]*\}")


class AppError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    source: Path
    use_dolby: bool
    use_aac: bool
    dolby_mode: str
    aac_mode: str
    matrix_mode: str
    dolby_format: str
    dolby_rate: int
    aac_rate: int
    audio_scope: str
    fallback1: str
    fallback2: str
    audio_overrides: dict[int, str]
    keep_subtitles: bool
    subtitle_scope: str
    video_preset: str
    deinterlace: bool
    deinterlace_algo: str
    parity: str
    overwrite: bool
    dolby_title: str
    aac_title: str


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


def find_program(name: str) -> str:
    names = [name]
    if os.name == "nt":
        names.insert(0, name + ".exe")

    for directory in (ROOT, ROOT / "bin"):
        for candidate in names:
            path = directory / candidate
            if path.is_file():
                return str(path)

    for candidate in names:
        found = shutil.which(candidate)
        if found:
            return found

    raise AppError(f"ไม่พบ {name} ใน PATH, โฟลเดอร์โปรแกรม หรือ bin/")


def language(value: str | None, fallback: str = "und") -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return fallback
    raw = LANGUAGE_ALIASES.get(raw, raw)
    return raw if re.fullmatch(r"[a-z]{3}", raw) else fallback


def stream_language(info: dict, fallback: str = "und") -> str:
    return language((info.get("tags") or {}).get("language"), fallback)


def parse_overrides(text: str) -> dict[int, str]:
    """เช่น 1=tha,2=eng; หมายเลขนับเฉพาะ audio stream เริ่มที่ 1"""
    result: dict[int, str] = {}
    if not text.strip():
        return result

    for item in text.split(","):
        match = re.fullmatch(
            r"\s*([1-9]\d*)\s*=\s*([A-Za-z]{2,3})\s*", item
        )
        if not match:
            raise AppError(
                "รูปแบบ Override ภาษาไม่ถูกต้อง: ใช้ 1=tha,2=eng"
            )

        number = int(match.group(1))
        code = language(match.group(2), "")
        if not code:
            raise AppError(f"รหัสภาษาของแทร็ก {number} ไม่ถูกต้อง")
        result[number] = code

    return result


def probe(ffprobe: str, path: Path) -> tuple[list[dict], list[dict]]:
    proc = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,channels,channel_layout:"
            "stream_tags=language,title:"
            "stream_disposition=default,forced:"
            "chapter=id,start_time,end_time:chapter_tags=title",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if proc.returncode:
        raise AppError(
            f"ffprobe อ่าน {path.name} ไม่สำเร็จ:\n{proc.stderr[-2000:]}"
        )

    try:
        data = json.loads(proc.stdout)
        return data.get("streams", []), data.get("chapters", [])
    except ValueError as exc:
        raise AppError(f"ข้อมูล stream ของ {path.name} ไม่ถูกต้อง") from exc


def is_raw51(info: dict) -> bool:
    """ไม่ตีความเสียง 6 ช่องที่ไม่ทราบ layout ว่าเป็น 5.1 เอง"""
    layout = str(info.get("channel_layout") or "").lower()
    return int(info.get("channels") or 0) == 6 and layout.startswith("5.1")


def loro_filter(info: dict) -> str | None:
    channels = int(info.get("channels") or 0)
    layout = str(info.get("channel_layout") or "").lower()

    if channels <= 2:
        return None

    if channels == 8 and layout.startswith("7.1"):
        return (
            "pan=stereo|"
            "FL=0.37*FL+0.26*FC+0.26*BL+0.26*SL|"
            "FR=0.37*FR+0.26*FC+0.26*BR+0.26*SR"
        )

    if channels in (5, 6) and layout.startswith(("5.0", "5.1")):
        left, right = (
            ("SL", "SR") if "side" in layout else ("BL", "BR")
        )
        return (
            f"pan=stereo|FL=0.46*FL+0.32*FC+0.32*{left}|"
            f"FR=0.46*FR+0.32*FC+0.32*{right}"
        )

    if channels == 4 and layout.startswith(("quad", "4.0")):
        left, right = (
            ("SL", "SR") if "side" in layout else ("BL", "BR")
        )
        return (
            f"pan=stereo|FL=0.5*FL+0.5*{left}|"
            f"FR=0.5*FR+0.5*{right}"
        )

    return None


def ass_timestamp(text: str) -> str:
    match = ASS_TIME.match(text)
    if not match:
        raise AppError(f"เวลาใน ASS ไม่ถูกต้อง: {text}")

    hour, minute, second, centisecond = map(int, match.groups())
    return (
        f"{hour:02}:{minute:02}:{second:02},"
        f"{centisecond * 10:03}"
    )


def ass_to_srt(ass_path: Path, srt_path: Path) -> None:
    """
    อ่าน Style: Alignment และ override \\an จาก ASS
    แล้วใส่ {\\anX} ใน SRT ก่อนส่งให้ tx3g_apple
    """
    content = ass_path.read_text(
        encoding="utf-8-sig", errors="replace"
    )

    section = ""
    style_fields: list[str] = []
    event_fields: list[str] = []
    alignment_by_style: dict[str, int] = {}
    items: list[str] = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("[") and line.endswith("]"):
            section = line.lower()
            continue

        if line.lower().startswith("format:"):
            fields = [
                part.strip().lower()
                for part in line.split(":", 1)[1].split(",")
            ]
            if "styles" in section:
                style_fields = fields
            elif section == "[events]":
                event_fields = fields
            continue

        if (
            "styles" in section
            and line.lower().startswith("style:")
            and style_fields
        ):
            values = [
                part.strip()
                for part in line.split(":", 1)[1].split(
                    ",", len(style_fields) - 1
                )
            ]
            if len(values) == len(style_fields):
                record = dict(zip(style_fields, values))
                try:
                    alignment_by_style[record["name"]] = int(
                        record["alignment"]
                    )
                except (KeyError, ValueError):
                    pass
            continue

        if (
            section != "[events]"
            or not line.lower().startswith("dialogue:")
            or not event_fields
        ):
            continue

        values = [
            part.strip()
            for part in line.split(":", 1)[1].split(
                ",", len(event_fields) - 1
            )
        ]
        if len(values) != len(event_fields):
            continue

        record = dict(zip(event_fields, values))

        try:
            start = ass_timestamp(record["start"])
            end = ass_timestamp(record["end"])
        except (KeyError, AppError):
            continue

        raw_text = record.get("text", "")
        inline_alignment = ASS_ALIGN.search(raw_text)
        alignment = (
            int(inline_alignment.group(1))
            if inline_alignment
            else alignment_by_style.get(record.get("style", ""), 2)
        )

        # tx3g_apple รองรับ {\\anX}; คงเฉพาะ alignment ไว้
        # ก่อนตัด ASS override อื่น ๆ ที่ไม่ใช่เนื้อหาซับ
        cleaned = ASS_TAGS.sub("", raw_text)
        cleaned = (
            cleaned.replace(r"\N", "\n")
            .replace(r"\n", "\n")
            .replace(r"\h", " ")
            .strip()
        )

        if not cleaned:
            continue

        prefix = f"{{\\an{alignment}}}" if 1 <= alignment <= 9 else ""
        items.append(
            f"{len(items) + 1}\n"
            f"{start} --> {end}\n"
            f"{prefix}{cleaned}\n"
        )

    if not items:
        raise AppError(
            f"ASS ไม่มี Dialogue ที่นำไปสร้าง TX3G ได้: {ass_path.name}"
        )

    srt_path.write_text("\n".join(items), encoding="utf-8")


def read_box_header(file, position: int, end: int):
    file.seek(position)
    header = file.read(8)
    if len(header) != 8:
        raise AppError("MP4 box header ไม่ครบ")

    size, box_type = struct.unpack(">I4s", header)
    header_size = 8

    if size == 1:
        extra = file.read(8)
        if len(extra) != 8:
            raise AppError("MP4 extended box header ไม่ครบ")
        size = struct.unpack(">Q", extra)[0]
        header_size = 16
    elif size == 0:
        size = end - position

    if size < header_size or position + size > end:
        raise AppError("MP4 box มีขนาดไม่ถูกต้อง")

    return size, box_type, header_size


def patch_chunk_offsets(moov: bytes, shift: int) -> bytes:
    """
    ย้าย moov ไปหน้า media โดยปรับ stco/co64 เท่านั้น
    ไม่ remux เพื่อรักษา TX3G sample/box ที่ helper สร้างไว้
    """
    data = bytearray(moov)
    containers = {
        b"moov", b"trak", b"mdia", b"minf", b"stbl",
        b"edts", b"udta", b"dinf",
    }
    patched = 0

    def visit(start: int, end: int) -> None:
        nonlocal patched
        pos = start

        while pos < end:
            if pos + 8 > end:
                raise AppError("โครงสร้าง moov ไม่สมบูรณ์")

            size = struct.unpack_from(">I", data, pos)[0]
            kind = bytes(data[pos + 4:pos + 8])
            header = 8

            if size == 1:
                if pos + 16 > end:
                    raise AppError("Extended box ไม่สมบูรณ์")
                size = struct.unpack_from(">Q", data, pos + 8)[0]
                header = 16
            elif size == 0:
                size = end - pos

            if size < header or pos + size > end:
                raise AppError("Box ภายใน moov มีขนาดไม่ถูกต้อง")

            body = pos + header
            box_end = pos + size

            if kind in containers:
                visit(body, box_end)

            elif kind in (b"stco", b"co64"):
                if body + 8 > box_end:
                    raise AppError("ตาราง chunk offset ไม่สมบูรณ์")

                count = struct.unpack_from(">I", data, body + 4)[0]
                width = 4 if kind == b"stco" else 8
                first = body + 8

                if first + count * width > box_end:
                    raise AppError("จำนวน chunk offset ไม่ถูกต้อง")

                for index in range(count):
                    offset_pos = first + index * width
                    fmt = ">I" if width == 4 else ">Q"
                    old = struct.unpack_from(fmt, data, offset_pos)[0]
                    new = old + shift

                    if new >= (1 << (width * 8)):
                        raise AppError(
                            "stco/co64 overflow ระหว่างทำ FastStart; "
                            "ไม่เขียนไฟล์ที่ offset เสีย"
                        )

                    struct.pack_into(fmt, data, offset_pos, new)
                    patched += 1

            pos = box_end

    visit(0, len(data))

    if patched == 0:
        raise AppError(
            "ไม่พบ stco/co64 ใน moov; ไม่สามารถยืนยัน FastStart ได้"
        )

    return bytes(data)


def copy_bytes(source, target, count: int) -> None:
    remaining = count

    while remaining:
        chunk = source.read(min(8 * 1024 * 1024, remaining))
        if not chunk:
            raise AppError("อ่าน MP4 ระหว่างย้าย moov ไม่ครบ")
        target.write(chunk)
        remaining -= len(chunk)


def faststart_native(source: Path, destination: Path) -> None:
    """
    tx3g_apple วาง moov ใหม่ท้ายไฟล์
    ย้ายกลับมาหลัง ftyp และปรับ stco/co64 โดยไม่แตะ TX3G samples

    จำกัดไว้ที่ MP4 แบบ non-fragmented ที่มี ftyp หน้าไฟล์
    และ moov เป็น box สุดท้าย; กรณีอื่นให้หยุดแทนการสร้างไฟล์เสีย
    """
    total = source.stat().st_size

    with source.open("rb") as input_file:
        boxes: list[tuple[int, int, bytes, int]] = []
        pos = 0

        while pos < total:
            size, kind, header = read_box_header(input_file, pos, total)
            boxes.append((pos, size, kind, header))
            pos += size

        if not boxes or boxes[0][2] != b"ftyp":
            raise AppError("MP4 ไม่มี ftyp เป็น box แรก")

        if boxes[-1][2] != b"moov":
            raise AppError(
                "moov ไม่ใช่ box สุดท้ายหลังฝัง TX3G; "
                "ไม่สามารถทำ FastStart อย่างปลอดภัย"
            )

        forbidden = {b"moof", b"mfra", b"sidx", b"tfra"}
        if any(box[2] in forbidden for box in boxes):
            raise AppError(
                "พบโครงสร้าง fragmented/indexed MP4 "
                "ที่ FastStart แบบนี้ไม่รองรับ"
            )

        ftyp_end = boxes[0][0] + boxes[0][1]
        moov_start, moov_size, _, _ = boxes[-1]

        input_file.seek(moov_start)
        moov = input_file.read(moov_size)
        if len(moov) != moov_size:
            raise AppError("อ่าน moov ไม่ครบ")

        # Media เดิมซึ่งอยู่หลัง ftyp จะถูกเลื่อนไปเท่าขนาด moov
        adjusted_moov = patch_chunk_offsets(moov, moov_size)

        with destination.open("wb") as output_file:
            input_file.seek(0)
            copy_bytes(input_file, output_file, ftyp_end)

            output_file.write(adjusted_moov)

            input_file.seek(ftyp_end)
            copy_bytes(
                input_file,
                output_file,
                moov_start - ftyp_end,
            )


class QueueWriter:
    """ส่ง print() ของ Python helpers ไปยัง Console โดยไม่แตะ Tk ข้าม thread"""

    def __init__(self, emit):
        self.emit = emit
        self.pending = ""

    def write(self, text: str) -> int:
        self.pending += str(text)

        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.strip():
                self.emit(line)

        return len(text)

    def flush(self) -> None:
        if self.pending.strip():
            self.emit(self.pending)
        self.pending = ""


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("VideoConV3")
        self.root.geometry("980x740")
        self.root.minsize(820, 610)

        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.running = False
        self._build()
        self.root.after(100, self._drain)

    def emit(self, text: str) -> None:
        self.events.put(("log", text))

        # Console จริงที่ใช้เปิด app.py จาก terminal
        print(text, file=sys.__stdout__, flush=True)

    def _drain(self) -> None:
        try:
            while True:
                kind, text = self.events.get_nowait()

                if kind == "log":
                    self.console.configure(state="normal")
                    self.console.insert("end", text + "\n")
                    self.console.see("end")
                    self.console.configure(state="disabled")

                elif kind == "done":
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.status_var.set(text)
                    messagebox.showinfo("VideoConV3", text)

        except queue.Empty:
            pass

        self.root.after(100, self._drain)

    @staticmethod
    def row_combo(
        parent, label: str, variable: tk.StringVar,
        values: list[str], row: int, column: int = 0,
        width: int = 32,
    ) -> None:
        ttk.Label(parent, text=label).grid(
            row=row, column=column, sticky="w", padx=10, pady=6
        )
        ttk.Combobox(
            parent,
            textvariable=variable,
            values=values,
            width=width,
            state="readonly",
        ).grid(
            row=row, column=column + 1,
            sticky="ew", padx=10, pady=6
        )

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(outer)
        notebook.grid(row=0, column=0, sticky="nsew")

        input_tab = ttk.Frame(notebook, padding=14)
        audio_tab = ttk.Frame(notebook, padding=14)
        subtitle_tab = ttk.Frame(notebook, padding=14)
        output_tab = ttk.Frame(notebook, padding=14)
        console_tab = ttk.Frame(notebook, padding=8)

        notebook.add(input_tab, text="1. ต้นฉบับ")
        notebook.add(audio_tab, text="2. เสียง")
        notebook.add(subtitle_tab, text="3. ซับและภาษา")
        notebook.add(output_tab, text="4. วิดีโอ/ผลลัพธ์")
        notebook.add(console_tab, text="5. Console")

        self.source_var = tk.StringVar()
        self.use_dolby_var = tk.BooleanVar(value=True)
        self.use_aac_var = tk.BooleanVar(value=False)
        self.dolby_mode_var = tk.StringVar(
            value="Dolby Matrix 2.0 (Lt/Rt)"
        )
        self.aac_mode_var = tk.StringVar(
            value="Stereo 2.0 (Lo/Ro)"
        )
        self.matrix_var = tk.StringVar(value="Dolby Pro Logic II")
        self.format_var = tk.StringVar(value="eac3")
        self.dolby_rate_var = tk.StringVar(value="192")
        self.aac_rate_var = tk.StringVar(value="192")
        self.dolby_title_var = tk.StringVar(value="Dolby Audio")
        self.aac_title_var = tk.StringVar(value="AAC Audio")
        self.scope_var = tk.StringVar(value="ไทยและอังกฤษ")
        self.fallback1_var = tk.StringVar(value="tha")
        self.fallback2_var = tk.StringVar(value="eng")
        self.overrides_var = tk.StringVar()
        self.subtitles_var = tk.BooleanVar(value=True)
        self.subtitle_scope_var = tk.StringVar(value="ไทยและอังกฤษ")
        self.video_var = tk.StringVar(
            value="Copy video (ไม่เข้ารหัสใหม่)"
        )
        self.deinterlace_var = tk.BooleanVar(value=False)
        self.deinterlace_algo_var = tk.StringVar(value="bwdif")
        self.parity_var = tk.StringVar(value="auto")
        self.overwrite_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="พร้อมทำงาน")

        input_tab.columnconfigure(0, weight=1)
        ttk.Label(
            input_tab,
            text="เลือกวิดีโอหนึ่งไฟล์ หรือเลือกโฟลเดอร์เพื่อประมวลผลหลายไฟล์",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        ttk.Entry(
            input_tab, textvariable=self.source_var
        ).grid(row=1, column=0, sticky="ew")

        ttk.Button(
            input_tab, text="เลือกไฟล์", command=self._pick_file
        ).grid(row=1, column=1, padx=6)

        ttk.Button(
            input_tab, text="เลือกโฟลเดอร์", command=self._pick_folder
        ).grid(row=1, column=2)

        ttk.Label(
            input_tab,
            text="ผลลัพธ์จะอยู่ข้างไฟล์ต้นฉบับ: ชื่อเดิม_covertedV1.mp4",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=16)

        audio_tab.columnconfigure(1, weight=1)

        dolby = ttk.LabelFrame(audio_tab, text="Dolby")
        dolby.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        dolby.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            dolby,
            text="สร้างแทร็ก Dolby (ค่าเริ่มต้น)",
            variable=self.use_dolby_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=8)

        self.row_combo(
            dolby, "การจัดการช่องเสียง",
            self.dolby_mode_var, list(AUDIO_MODES), 1
        )
        self.row_combo(
            dolby, "ชนิดเสียง",
            self.format_var, ["eac3", "ac3"], 2
        )
        self.row_combo(
            dolby, "Bitrate (kbps)",
            self.dolby_rate_var,
            ["128", "160", "192", "224", "256", "320",
             "384", "448", "512", "640"], 3
        )

        ttk.Label(dolby, text="ชื่อแทร็ก").grid(
            row=4, column=0, sticky="w", padx=10, pady=6
        )
        ttk.Entry(
            dolby, textvariable=self.dolby_title_var
        ).grid(row=4, column=1, sticky="ew", padx=10, pady=6)

        aac = ttk.LabelFrame(audio_tab, text="AAC (qaac)")
        aac.grid(row=1, column=0, columnspan=2, sticky="ew")
        aac.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            aac,
            text="สร้างแทร็ก AAC เพิ่ม หรือเลือก AAC อย่างเดียว",
            variable=self.use_aac_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=10, pady=8)

        self.row_combo(
            aac, "การจัดการช่องเสียง",
            self.aac_mode_var, list(AUDIO_MODES), 1
        )
        self.row_combo(
            aac, "Bitrate CVBR (kbps)",
            self.aac_rate_var,
            ["128", "160", "192", "224", "256", "320",
             "384", "448", "512"], 2
        )

        ttk.Label(aac, text="ชื่อแทร็ก").grid(
            row=3, column=0, sticky="w", padx=10, pady=6
        )
        ttk.Entry(
            aac, textvariable=self.aac_title_var
        ).grid(row=3, column=1, sticky="ew", padx=10, pady=6)

        ttk.Label(
            audio_tab,
            text=(
                "Raw 5.1 = รักษา 6 ช่องไว้แล้วเข้ารหัสใหม่; "
                "ไม่ใช่การคัดลอก AC-3/E-AC-3/AAC bitstream"
            ),
            wraplength=790,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=14)

        subtitle_tab.columnconfigure(1, weight=1)

        self.row_combo(
            subtitle_tab, "เลือกแทร็กเสียง",
            self.scope_var, ["ไทยและอังกฤษ", "ทุกภาษา"], 0
        )
        self.row_combo(
            subtitle_tab, "แทร็กเสียง 1: ภาษาเมื่อไม่พบ tag",
            self.fallback1_var, ["tha", "eng", "und"], 1
        )
        self.row_combo(
            subtitle_tab, "แทร็กเสียง 2: ภาษาเมื่อไม่พบ tag",
            self.fallback2_var, ["eng", "tha", "und"], 2
        )

        ttk.Label(
            subtitle_tab,
            text="Override ภาษาเสียง (แม้มี tag อยู่แล้ว)",
        ).grid(row=3, column=0, sticky="w", padx=10, pady=6)

        ttk.Entry(
            subtitle_tab, textvariable=self.overrides_var
        ).grid(row=3, column=1, sticky="ew", padx=10, pady=6)

        ttk.Label(
            subtitle_tab,
            text=(
                "ตัวอย่าง: 1=tha,2=eng  |  หมายเลขนับตามลำดับ"
                "แทร็กเสียงในไฟล์ เริ่มที่ 1"
            ),
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=10)

        ttk.Separator(
            subtitle_tab, orient="horizontal"
        ).grid(
            row=5, column=0, columnspan=2,
            sticky="ew", pady=20
        )

        ttk.Checkbutton(
            subtitle_tab,
            text="ฝังซับข้อความเป็น Apple-style TX3G",
            variable=self.subtitles_var,
        ).grid(
            row=6, column=0, columnspan=2,
            sticky="w", padx=10
        )

        self.row_combo(
            subtitle_tab, "เลือกแทร็กคำบรรยาย",
            self.subtitle_scope_var,
            ["ไทยและอังกฤษ", "ทุกภาษา"], 7
        )

        ttk.Label(
            subtitle_tab,
            text=(
                "ซับภาพ เช่น PGS/DVD ไม่สามารถฝังเป็น TX3G โดยตรง "
                "ระบบจะข้ามและแจ้งใน Console\n"
                "ASS/SSA: อ่าน Style Alignment และ {\\an8} "
                "ก่อนสร้าง SRT สำหรับตัวฝัง TX3G"
            ),
            wraplength=790,
        ).grid(
            row=8, column=0, columnspan=2,
            sticky="w", padx=10, pady=12
        )

        output_tab.columnconfigure(1, weight=1)

        self.row_combo(
            output_tab, "วิดีโอ",
            self.video_var, list(VIDEO_PRESETS), 0
        )

        ttk.Checkbutton(
            output_tab,
            text="Deinterlace (ต้องเข้ารหัสวิดีโอใหม่)",
            variable=self.deinterlace_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", padx=10, pady=8)

        self.row_combo(
            output_tab, "วิธี Deinterlace",
            self.deinterlace_algo_var, ["bwdif", "yadif"], 2
        )
        self.row_combo(
            output_tab, "Field parity",
            self.parity_var, ["auto", "tff", "bff"], 3
        )

        ttk.Checkbutton(
            output_tab,
            text="เขียนทับไฟล์ผลลัพธ์ถ้ามีอยู่แล้ว",
            variable=self.overwrite_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=10, pady=14)

        ttk.Label(
            output_tab,
            text=(
                "Output: MP4 แบบ non-fragmented เท่านั้น\n"
                "คัดลอก Chapter data และจัด moov หน้าไฟล์ "
                "(FastStart) หลังฝัง TX3G เสร็จ"
            ),
            wraplength=790,
        ).grid(row=5, column=0, columnspan=2, sticky="w", padx=10)

        console_tab.columnconfigure(0, weight=1)
        console_tab.rowconfigure(0, weight=1)

        console_frame = ttk.Frame(console_tab)
        console_frame.grid(row=0, column=0, sticky="nsew")
        console_frame.columnconfigure(0, weight=1)
        console_frame.rowconfigure(0, weight=1)

        self.console = tk.Text(
            console_frame,
            wrap="word",
            state="disabled",
            font=("Consolas", 10),
        )
        self.console.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(
            console_frame,
            orient="vertical",
            command=self.console.yview,
        )
        scroll.grid(row=0, column=1, sticky="ns")
        self.console.configure(yscrollcommand=scroll.set)

        footer = ttk.Frame(outer)
        footer.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(1, weight=1)

        self.start_button = ttk.Button(
            footer,
            text="เริ่มแปลง",
            command=self._start,
        )
        self.start_button.grid(row=0, column=0, padx=(0, 12))

        ttk.Label(
            footer, textvariable=self.status_var
        ).grid(row=0, column=1, sticky="w")

        ttk.Button(
            footer,
            text="เปิด Console",
            command=lambda: notebook.select(console_tab),
        ).grid(row=0, column=2)

    def _pick_file(self) -> None:
        value = filedialog.askopenfilename(
            filetypes=[
                (
                    "Video",
                    "*.mkv *.mp4 *.m4v *.mov "
                    "*.ts *.mts *.m2ts *.webm",
                ),
                ("All files", "*.*"),
            ]
        )
        if value:
            self.source_var.set(value)

    def _pick_folder(self) -> None:
        value = filedialog.askdirectory()
        if value:
            self.source_var.set(value)

    def _settings(self) -> Settings:
        source_text = self.source_var.get().strip()

        if not source_text:
            raise AppError("กรุณาเลือกไฟล์หรือโฟลเดอร์ต้นฉบับ")

        source = Path(source_text).expanduser()
        if not source.exists():
            raise AppError("ไม่พบไฟล์หรือโฟลเดอร์ต้นฉบับ")

        if not (self.use_dolby_var.get() or self.use_aac_var.get()):
            raise AppError("ต้องเลือก Dolby, AAC หรือทั้งคู่")

        dolby_mode = AUDIO_MODES[self.dolby_mode_var.get()]
        aac_mode = AUDIO_MODES[self.aac_mode_var.get()]
        dolby_rate = int(self.dolby_rate_var.get())
        aac_rate = int(self.aac_rate_var.get())

        if self.format_var.get() == "ac3" and dolby_rate > 640:
            raise AppError("AC-3 รองรับ bitrate ไม่เกิน 640 kbps")

        if (
            self.use_aac_var.get()
            and aac_mode == "raw51"
            and aac_rate < 320
        ):
            raise AppError(
                "AAC Raw 5.1 กรุณาเลือก bitrate ตั้งแต่ 320 kbps"
            )

        return Settings(
            source=source,
            use_dolby=self.use_dolby_var.get(),
            use_aac=self.use_aac_var.get(),
            dolby_mode=dolby_mode,
            aac_mode=aac_mode,
            matrix_mode=MATRIX_MODES[self.matrix_var.get()],
            dolby_format=self.format_var.get(),
            dolby_rate=dolby_rate,
            aac_rate=aac_rate,
            audio_scope=self.scope_var.get(),
            fallback1=self.fallback1_var.get(),
            fallback2=self.fallback2_var.get(),
            audio_overrides=parse_overrides(self.overrides_var.get()),
            keep_subtitles=self.subtitles_var.get(),
            subtitle_scope=self.subtitle_scope_var.get(),
            video_preset=self.video_var.get(),
            deinterlace=self.deinterlace_var.get(),
            deinterlace_algo=self.deinterlace_algo_var.get(),
            parity=self.parity_var.get(),
            overwrite=self.overwrite_var.get(),
            dolby_title=self.dolby_title_var.get().strip() or "Dolby Audio",
            aac_title=self.aac_title_var.get().strip() or "AAC Audio",
        )

    def _start(self) -> None:
        if self.running:
            return

        try:
            settings = self._settings()

            if (
                settings.source.is_file()
                and settings.source.suffix.lower()
                not in VIDEO_EXTENSIONS
            ):
                raise AppError("ต้นฉบับไม่ใช่นามสกุลวิดีโอที่รองรับ")

        except (AppError, ValueError, KeyError) as exc:
            messagebox.showerror("VideoConV3", str(exc))
            return

        self.running = True
        self.start_button.configure(state="disabled")
        self.status_var.set("กำลังประมวลผล...")

        threading.Thread(
            target=self._worker,
            args=(settings,),
            daemon=True,
        ).start()

    def run_command(self, command: list[str], label: str) -> None:
        """แสดง stdout/stderr แบบต่อเนื่องทั้งใน Console และ terminal"""
        self.emit(f"$ {label}: {' '.join(map(str, command))}")
        tail: list[str] = []

        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise AppError(f"เรียก {label} ไม่ได้: {exc}") from exc

        assert process.stdout is not None

        with process.stdout:
            for line in process.stdout:
                text = line.rstrip()
                if text:
                    self.emit(f"[{label}] {text}")
                    tail.append(text)
                    tail = tail[-12:]

        code = process.wait()
        if code:
            raise AppError(
                f"{label} ล้มเหลว (exit {code}):\n"
                + "\n".join(tail)
            )

    def _worker(self, settings: Settings) -> None:
        succeeded = 0
        failed = 0

        try:
            ffmpeg = find_program("ffmpeg")
            ffprobe = find_program("ffprobe")
            qaac = find_program("qaac64") if settings.use_aac else None

            encoder = (
                load_helper("ddp_encode")
                if settings.use_dolby else None
            )

            matrix = (
                load_helper("6to2matrixencoder")
                if (
                    (settings.use_dolby and settings.dolby_mode == "matrix")
                    or (settings.use_aac and settings.aac_mode == "matrix")
                )
                else None
            )

            tx3g = (
                load_helper("tx3g_apple")
                if settings.keep_subtitles else None
            )

            dee = os.environ.get("DEE_BIN")
            if settings.use_dolby and not dee:
                dee = find_program("dee")

            if settings.source.is_file():
                files = [settings.source]
            else:
                files = sorted(
                    path for path in settings.source.rglob("*")
                    if (
                        path.is_file()
                        and path.suffix.lower() in VIDEO_EXTENSIONS
                        and not path.stem.endswith("_covertedV1")
                    )
                )

            if not files:
                raise AppError("ไม่พบวิดีโอที่ต้องแปลง")

            for index, source in enumerate(files, 1):
                self.emit(f"\n[{index}/{len(files)}] {source}")

                try:
                    self._convert(
                        settings, source, ffmpeg, ffprobe,
                        qaac, encoder, matrix, tx3g, dee,
                    )
                    succeeded += 1
                except Exception as exc:
                    failed += 1
                    self.emit(f"[ERROR] {source.name}: {exc}")

            summary = (
                f"เสร็จแล้ว: สำเร็จ {succeeded} ไฟล์ "
                f"ล้มเหลว {failed} ไฟล์"
            )
            self.emit(summary)
            self.events.put(("done", summary))

        except Exception as exc:
            self.emit(f"[ERROR] เริ่มงานไม่ได้: {exc}")
            self.events.put(("done", "การแปลงไม่สำเร็จ"))

    def _pcm(
        self,
        ffmpeg: str,
        source: Path,
        stream_index: int,
        destination: Path,
    ) -> None:
        self.run_command(
            [
                ffmpeg, "-hide_banner", "-nostdin",
                "-loglevel", "info", "-nostats", "-y",
                "-i", str(source),
                "-map", f"0:{stream_index}",
                "-vn", "-sn", "-dn",
                "-ar", "48000",
                "-c:a", "pcm_f32le",
                "-rf64", "auto",
                str(destination),
            ],
            f"แตกเสียง stream {stream_index}",
        )

    def _make_audio(
        self,
        ffmpeg: str,
        original: Path,
        destination: Path,
        info: dict,
        mode: str,
        matrix_mode: str,
        matrix: ModuleType | None,
    ) -> tuple[str, int]:
        channels = int(info.get("channels") or 0)

        if mode == "raw51":
            if not is_raw51(info):
                raise AppError(
                    "เลือก Raw 5.1 แต่ต้นฉบับไม่ใช่ "
                    "5.1 ที่ระบุ channel layout ชัดเจน: "
                    f"{channels} ch / "
                    f"{info.get('channel_layout', 'unknown')}"
                )

            self.emit("  คง PCM 5.1 เดิมไว้ ไม่ทำ downmix")
            return "raw51", 6

        if mode == "matrix" and channels > 2:
            if matrix is None:
                raise AppError("ไม่พบตัวเข้ารหัส Matrix")

            args = matrix.build_arg_parser().parse_args(
                [
                    "-i", str(original),
                    "-o", str(destination),
                    "--mode", matrix_mode,
                    "--layout", "auto",
                    "--headroom", "peak",
                    "--ffmpeg", ffmpeg,
                    "-y",
                ]
            )

            writer = QueueWriter(
                lambda text: self.emit(f"[Matrix] {text}")
            )

            with contextlib.redirect_stdout(writer):
                with contextlib.redirect_stderr(writer):
                    result = matrix.EncoderPipeline(args).run()

            writer.flush()

            if result != 0 or not destination.is_file():
                raise AppError(
                    f"สร้าง Dolby Matrix ไม่สำเร็จ (exit {result})"
                )

            return "matrix", 2

        if mode == "matrix":
            self.emit(
                "  ต้นฉบับไม่เกิน 2 ช่อง: "
                "ไม่สร้าง Matrix ใหม่ ใช้ stereo เดิม"
            )

        audio_filter = loro_filter(info)
        if channels > 2 and not audio_filter:
            self.emit(
                "  ไม่ทราบ layout สำหรับสูตร Lo/Ro: "
                "ใช้ FFmpeg downmix แทน"
            )

        command = [
            ffmpeg, "-hide_banner", "-nostdin",
            "-loglevel", "info", "-nostats", "-y",
            "-i", str(original),
            "-map", "0:a:0",
            "-vn", "-sn", "-dn",
        ]

        if audio_filter:
            command += ["-af", audio_filter]

        command += [
            "-ac", "2",
            "-ar", "48000",
            "-c:a", "pcm_f32le",
            "-rf64", "auto",
            str(destination),
        ]

        self.run_command(command, "สร้าง Stereo 2.0")
        return "loro", 2

    def _subtitle_srt(
        self,
        ffmpeg: str,
        source: Path,
        info: dict,
        temp: Path,
        ordinal: int,
    ) -> Path:
        codec = str(info.get("codec_name") or "").lower()
        srt = temp / f"subtitle_{ordinal}.srt"

        if codec in {"ass", "ssa"}:
            ass = temp / f"subtitle_{ordinal}.ass"

            self.run_command(
                [
                    ffmpeg, "-hide_banner", "-nostdin",
                    "-loglevel", "info", "-nostats", "-y",
                    "-i", str(source),
                    "-map", f"0:{info['index']}",
                    "-vn", "-an", "-dn",
                    "-c:s", "ass",
                    "-f", "ass",
                    str(ass),
                ],
                f"แยก ASS/SSA stream {info['index']}",
            )

            ass_to_srt(ass, srt)
            self.emit(
                f"  ASS/SSA stream {info['index']}: "
                "อ่าน Style Alignment / inline \\an แล้ว"
            )

        else:
            self.run_command(
                [
                    ffmpeg, "-hide_banner", "-nostdin",
                    "-loglevel", "info", "-nostats", "-y",
                    "-i", str(source),
                    "-map", f"0:{info['index']}",
                    "-vn", "-an", "-dn",
                    "-c:s", "srt",
                    "-f", "srt",
                    str(srt),
                ],
                f"แยก SRT stream {info['index']}",
            )

        if not srt.is_file() or srt.stat().st_size == 0:
            raise AppError(
                f"ซับ stream {info['index']} ไม่มีข้อความ"
            )

        return srt

    def _mux(
        self,
        settings: Settings,
        ffmpeg: str,
        source: Path,
        encoded: list[dict],
        output: Path,
    ) -> None:
        command = [
            ffmpeg, "-hide_banner", "-nostdin",
            "-loglevel", "info", "-nostats", "-y",
            "-i", str(source),
        ]

        for track in encoded:
            command += ["-i", str(track["path"])]

        command += ["-map", "0:v:0"]

        for index in range(len(encoded)):
            command += ["-map", f"{index + 1}:a:0"]

        command += [
            "-map_metadata", "0",
            "-map_chapters", "0",
            "-dn",
        ]

        preset = VIDEO_PRESETS[settings.video_preset]

        if preset is None and not settings.deinterlace:
            command += ["-c:v", "copy"]
        else:
            command += ["-c:v", "libx265", "-preset", "medium"]

            if preset:
                command += ["-x265-params", preset]
            else:
                command += ["-crf", "23"]

            if settings.deinterlace:
                command += [
                    "-vf",
                    f"{settings.deinterlace_algo}"
                    f"=mode=send_frame:"
                    f"parity={settings.parity}:"
                    "deint=all",
                ]

        command += ["-c:a", "copy"]

        for index, track in enumerate(encoded):
            command += [
                f"-metadata:s:a:{index}",
                f"language={track['language']}",
                f"-metadata:s:a:{index}",
                f"title={track['title']}",
                f"-disposition:a:{index}",
                "default" if index == 0 else "0",
            ]

        # ไม่มี frag_keyframe / empty_moov: output ไม่ใช่ fMP4
        # ตอนนี้ยังไม่ใช้ faststart เพราะ TX3G helper จะย้าย moov อีก
        command += ["-f", "mp4", str(output)]

        self.run_command(command, "Mux MP4 และ Chapters")

    def _convert(
        self,
        settings: Settings,
        source: Path,
        ffmpeg: str,
        ffprobe: str,
        qaac: str | None,
        encoder: ModuleType | None,
        matrix: ModuleType | None,
        tx3g: ModuleType | None,
        dee: str | None,
    ) -> None:
        streams, source_chapters = probe(ffprobe, source)

        if not any(
            item.get("codec_type") == "video"
            for item in streams
        ):
            raise AppError("ไม่มี video stream")

        audio_streams = [
            item for item in streams
            if item.get("codec_type") == "audio"
        ]

        subtitle_streams = [
            item for item in streams
            if item.get("codec_type") == "subtitle"
        ]

        selected_audio: list[tuple[dict, str]] = []

        for ordinal, info in enumerate(audio_streams, 1):
            fallback = (
                settings.fallback1 if ordinal == 1
                else settings.fallback2 if ordinal == 2
                else "und"
            )

            # ลำดับความสำคัญ:
            # explicit override > metadata ของไฟล์ > fallback
            lang = settings.audio_overrides.get(
                ordinal,
                stream_language(info, fallback),
            )

            self.emit(
                f"  เสียง #{ordinal}: stream={info['index']}, "
                f"{info.get('channels', '?')} ch, "
                f"layout={info.get('channel_layout', 'unknown')}, "
                f"ภาษา={lang}"
            )

            if (
                settings.audio_scope == "ไทยและอังกฤษ"
                and lang not in {"tha", "eng"}
            ):
                self.emit("    ข้าม: ไม่ตรงตัวกรองภาษาเสียง")
                continue

            selected_audio.append((info, lang))

        if not selected_audio:
            raise AppError(
                "ไม่เหลือแทร็กเสียงหลังใช้ตัวกรองภาษา"
            )

        selected_subtitles: list[dict] = []

        if settings.keep_subtitles:
            for info in subtitle_streams:
                lang = stream_language(info)
                codec = str(info.get("codec_name") or "").lower()

                if (
                    settings.subtitle_scope == "ไทยและอังกฤษ"
                    and lang not in {"tha", "eng"}
                ):
                    continue

                if codec not in TEXT_SUBTITLE_CODECS:
                    self.emit(
                        f"  ข้ามซับภาพ/ไม่รองรับ: "
                        f"stream {info['index']} ({codec}, {lang})"
                    )
                    continue

                selected_subtitles.append(info)

        output = source.with_name(
            f"{source.stem}_covertedV1.mp4"
        )

        if output.exists() and not settings.overwrite:
            raise AppError(f"ไฟล์ผลลัพธ์มีอยู่แล้ว: {output}")

        with tempfile.TemporaryDirectory(
            prefix=".videoconv3_",
            dir=output.parent,
        ) as temp_dir:
            temp = Path(temp_dir)
            encoded: list[dict] = []

            for ordinal, (info, lang) in enumerate(selected_audio):
                pcm = temp / f"audio_{ordinal}.wav"

                self._pcm(
                    ffmpeg,
                    source,
                    int(info["index"]),
                    pcm,
                )

                prepared: dict[str, tuple[Path, str, int]] = {}

                def input_for(mode: str) -> tuple[Path, str, int]:
                    if mode not in prepared:
                        target = (
                            temp / f"prepared_{ordinal}_{mode}.wav"
                        )

                        actual_mode, channels = self._make_audio(
                            ffmpeg,
                            pcm,
                            target,
                            info,
                            mode,
                            settings.matrix_mode,
                            matrix,
                        )

                        prepared[mode] = (
                            pcm if actual_mode == "raw51" else target,
                            actual_mode,
                            channels,
                        )

                    return prepared[mode]

                original_title = str(
                    (info.get("tags") or {}).get("title") or ""
                ).strip()

                if settings.use_dolby:
                    assert encoder is not None

                    dolby_input, actual_mode, channels = input_for(
                        settings.dolby_mode
                    )

                    dolby_file = (
                        temp
                        / f"dolby_{ordinal}.{settings.dolby_format}"
                    )

                    self.emit(
                        f"  Dolby: {actual_mode}, {channels} ch, "
                        f"{settings.dolby_format}, "
                        f"{settings.dolby_rate} kbps"
                    )

                    writer = QueueWriter(
                        lambda text: self.emit(f"[DEE] {text}")
                    )

                    with contextlib.redirect_stdout(writer):
                        with contextlib.redirect_stderr(writer):
                            result = encoder.encode(
                                str(dolby_input),
                                str(dolby_file),
                                encoder_mode=(
                                    "dd"
                                    if settings.dolby_format == "ac3"
                                    else "ddp"
                                ),
                                output_format=settings.dolby_format,
                                data_rate=settings.dolby_rate,
                                downmix_config="off",
                                lfe_on=(actual_mode == "raw51"),
                                dolby_surround_mode=(
                                    "yes"
                                    if actual_mode == "matrix"
                                    else "no"
                                ),
                                dolby_surround_ex_mode="no",
                                preferred_downmix=(
                                    "ltrt"
                                    if actual_mode == "matrix"
                                    else "loro"
                                ),
                                loudness_mode="measure_only",
                                temp_dir=str(temp),
                                dee_bin=dee,
                                verbose=True,
                                quiet=False,
                                overwrite=True,
                            )

                    writer.flush()

                    if (
                        result != 0
                        or not dolby_file.is_file()
                        or dolby_file.stat().st_size == 0
                    ):
                        raise AppError(
                            f"เข้ารหัส Dolby ไม่สำเร็จ (exit {result})"
                        )

                    encoded.append(
                        {
                            "path": dolby_file,
                            "language": lang,
                            "title": (
                                f"{original_title} - "
                                if original_title else ""
                            ) + settings.dolby_title,
                        }
                    )

                if settings.use_aac:
                    assert qaac is not None

                    aac_input, actual_mode, channels = input_for(
                        settings.aac_mode
                    )

                    aac_file = temp / f"aac_{ordinal}.m4a"

                    self.emit(
                        f"  AAC: {actual_mode}, {channels} ch, "
                        f"CVBR {settings.aac_rate} kbps"
                    )

                    self.run_command(
                        [
                            qaac,
                            "--cvbr", str(settings.aac_rate),
                            "--no-delay",
                            "-q", "2",
                            str(aac_input),
                            "-o", str(aac_file),
                        ],
                        "qaac",
                    )

                    if (
                        not aac_file.is_file()
                        or aac_file.stat().st_size == 0
                    ):
                        raise AppError("qaac ไม่ได้สร้างไฟล์ AAC")

                    encoded.append(
                        {
                            "path": aac_file,
                            "language": lang,
                            "title": (
                                f"{original_title} - "
                                if original_title else ""
                            ) + settings.aac_title,
                        }
                    )

            muxed = temp / "muxed.mp4"
            self._mux(
                settings,
                ffmpeg,
                source,
                encoded,
                muxed,
            )

            current = muxed

            if selected_subtitles:
                if tx3g is None:
                    raise AppError("ไม่พบ tx3g_apple.py")

                for ordinal, info in enumerate(
                    selected_subtitles, 1
                ):
                    srt = self._subtitle_srt(
                        ffmpeg,
                        source,
                        info,
                        temp,
                        ordinal,
                    )

                    next_mp4 = temp / f"tx3g_{ordinal}.mp4"
                    lang = stream_language(info)

                    self.emit(
                        f"  TX3G #{ordinal}: "
                        f"stream={info['index']}, "
                        f"ภาษา={lang}"
                    )

                    writer = QueueWriter(
                        lambda text: self.emit(f"[TX3G] {text}")
                    )

                    with contextlib.redirect_stdout(writer):
                        with contextlib.redirect_stderr(writer):
                            success = (
                                tx3g.embed_tx3g_subtitles_native(
                                    video_path=str(current),
                                    srt_path=str(srt),
                                    output_path=str(next_mp4),
                                    lang_code=lang,
                                    progress_cb=(
                                        lambda percent, text:
                                        self.emit(
                                            f"[TX3G] "
                                            f"{percent}% {text}"
                                        )
                                    ),
                                )
                            )

                    writer.flush()

                    if (
                        success is not True
                        or not next_mp4.is_file()
                        or next_mp4.stat().st_size == 0
                    ):
                        raise AppError(
                            f"ฝัง TX3G #{ordinal} ไม่สำเร็จ"
                        )

                    current = next_mp4

            faststarted = temp / "faststart.mp4"
            self.emit(
                "  จัด moov หน้าไฟล์สำหรับ FastStart "
                "โดยไม่ remux TX3G"
            )

            faststart_native(current, faststarted)

            final_streams, final_chapters = probe(
                ffprobe, faststarted
            )

            video_count = sum(
                item.get("codec_type") == "video"
                for item in final_streams
            )
            audio_count = sum(
                item.get("codec_type") == "audio"
                for item in final_streams
            )
            subtitle_count = sum(
                item.get("codec_type") == "subtitle"
                for item in final_streams
            )

            if video_count < 1:
                raise AppError("ผลลัพธ์ไม่มี video stream")

            if audio_count != len(encoded):
                raise AppError(
                    "จำนวนแทร็กเสียงไม่ตรง: "
                    f"ต้องมี {len(encoded)}, พบ {audio_count}"
                )

            if subtitle_count != len(selected_subtitles):
                raise AppError(
                    "จำนวนแทร็ก TX3G ไม่ตรง: "
                    f"ต้องมี {len(selected_subtitles)}, "
                    f"พบ {subtitle_count}"
                )

            if len(final_chapters) != len(source_chapters):
                raise AppError(
                    "Chapter data ไม่ครบ: "
                    f"ต้นฉบับ {len(source_chapters)}, "
                    f"ผลลัพธ์ {len(final_chapters)}"
                )

            if output.exists() and not settings.overwrite:
                raise AppError(
                    f"ไฟล์ผลลัพธ์ถูกสร้างระหว่างทำงาน: {output}"
                )

            os.replace(faststarted, output)
            self.emit(f"[OK] {output}")


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()