#!/usr/bin/env python3
# pipeline_gui.py — GUI ครอบ pipeline_to_ac3_ec3_ltrt.py (tkinter, ไม่ต้องติดตั้งอะไรเพิ่ม)
# วางไฟล์นี้ไว้โฟลเดอร์เดียวกับ pipeline_to_ac3_ec3_ltrt.py แล้วรัน: python pipeline_gui.py

import importlib.util
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "Wav2DolbySurround.py"
SETTINGS = Path.home() / ".pipeline_ltrt_gui.json"

MODE_HELP = {
    "dpl1": "Dolby Surround / Pro Logic I: S=(Ls+Rs) mono, band-limit 100 Hz–7 kHz, หมุนเฟส 90°",
    "dpl2": "Pro Logic II: หมุนเฟส 90°, สัมประสิทธิ์ 0.871 / 0.490",
    "dpl2-no90": "Pro Logic II สูตรเดียวกัน แต่ไม่หมุนเฟส 90°",
    "dolby-surround": "Lt/Rt มาตรฐาน Dolby Digital (A/52): หมุนเฟส 90°, surround −3 dB, full-band",
}
PRIORITY = ["data_rate", "dialnorm", "timecode_frame_rate", "preset", "metering_mode",
            "dolby_surround_mode", "preferred_downmix_mode", "line_mode_drc_profile", "rf_mode_drc_profile",
            "ltrt_center_mix_level", "ltrt_surround_mix_level", "loro_center_mix_level",
            "loro_surround_mix_level"]
PROGRESS_RE = re.compile(r"^\s*block\s+(\d+):\s*(\d+)s(?:/(\d+)s)?")


def load_core():
    if not SCRIPT.exists():
        messagebox.showerror("ไม่พบสคริปต์", f"ต้องมีไฟล์ {SCRIPT.name} อยู่โฟลเดอร์เดียวกับ GUI")
        sys.exit(1)
    try:
        spec = importlib.util.spec_from_file_location("pipeline_core", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except SystemExit:
        messagebox.showerror("ขาดไลบรารี", "กรุณาติดตั้ง numpy และ scipy:\n\npip install numpy scipy")
        sys.exit(1)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.core = load_core()
        self.title("Matrix Lt/Rt → AC-3 / E-AC-3 (Dolby DEE)")
        self.geometry("860x820")
        self.minsize(760, 640)

        self.v = {k: tk.StringVar() for k in
                  ("input", "output", "fmt", "ec3ext", "mode", "layout", "lfe", "lfe_level", "lpf",
                   "block", "ctx", "loudness", "save_xml", "work_dir", "tool_dir")}
        self.b = {k: tk.BooleanVar() for k in ("no_sband", "no_norm", "wav_only")}
        self.pvars = {}          # พารามิเตอร์ DEE: key -> StringVar
        self.proc = None
        self.q = queue.Queue()
        self.prog_active = False
        self.cur_fmt = None

        self._saved_params = {}
        self._defaults()
        self._load_settings()
        self._build()
        self._on_fmt_change()
        self._on_mode_change()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.deiconify()
        self.after(100, self._poll)

    # ------------------------------------------------------------ settings
    def _defaults(self):
        for k, val in dict(fmt="auto", ec3ext="", mode="dpl2", layout="auto", lfe="drop", lfe_level="-10",
                           lpf="18500", block="262144", ctx="16384", loudness="").items():
            self.v[k].set(val)

    def _load_settings(self):
        try:
            d = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:
            return
        for k, x in d.get("v", {}).items():
            if k in self.v and k not in ("input", "output"):
                self.v[k].set(x)
        for k, x in d.get("b", {}).items():
            if k in self.b:
                self.b[k].set(x)
        self._saved_params = d.get("p", {})

    def _save_settings(self):
        d = {"v": {k: x.get() for k, x in self.v.items()},
             "b": {k: x.get() for k, x in self.b.items()},
             "p": {k: x.get() for k, x in self.pvars.items() if x.get()}}
        try:
            SETTINGS.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ UI
    def _build(self):
        pad = dict(padx=8, pady=4)
        top = ttk.LabelFrame(self, text="ไฟล์")
        top.pack(fill="x", **pad)
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="อินพุต").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(top, textvariable=self.v["input"]).grid(row=0, column=1, sticky="ew", pady=3)
        ttk.Button(top, text="เลือก…", command=self._pick_input).grid(row=0, column=2, padx=6)
        ttk.Label(top, text="เอาต์พุต").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        e = ttk.Entry(top, textvariable=self.v["output"])
        e.grid(row=1, column=1, sticky="ew", pady=3)
        e.bind("<FocusOut>", lambda _e: self._on_fmt_change())
        ttk.Button(top, text="บันทึกเป็น…", command=self._pick_output).grid(row=1, column=2, padx=6)

        fr = ttk.Frame(top)
        fr.grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(fr, text="Format").pack(side="left")
        cb = ttk.Combobox(fr, textvariable=self.v["fmt"], values=["auto", "ac3", "ec3"], width=6, state="readonly")
        cb.pack(side="left", padx=(4, 14))
        cb.bind("<<ComboboxSelected>>", lambda _e: self._on_fmt_change())
        ttk.Label(fr, text="นามสกุล E-AC-3").pack(side="left")
        ttk.Combobox(fr, textvariable=self.v["ec3ext"], values=["", "ec3", "eac3"], width=6,
                     state="readonly").pack(side="left", padx=4)
        ttk.Label(fr, text="(ว่าง = ตามชื่อไฟล์)", foreground="#777").pack(side="left")

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=False, **pad)
        nb.add(self._tab_matrix(nb), text="Matrix / Downmix")
        nb.add(self._tab_dee(nb), text="Encoder (DEE)")
        nb.add(self._tab_adv(nb), text="ขั้นสูง")

        bar = ttk.Frame(self)
        bar.pack(fill="x", **pad)
        self.btn_run = ttk.Button(bar, text="▶  เริ่มแปลง", command=self._run)
        self.btn_run.pack(side="left")
        self.btn_cancel = ttk.Button(bar, text="■ ยกเลิก", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        ttk.Button(bar, text="คัดลอกคำสั่ง", command=self._copy_cmd).pack(side="left")
        self.status = ttk.Label(bar, text="พร้อม", foreground="#555")
        self.status.pack(side="right")

        self.pbar = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.pbar.pack(fill="x", padx=8)

        lf = ttk.LabelFrame(self, text="Log")
        lf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(lf, height=12, wrap="word", state="disabled", font=("Consolas", 9))
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("err", foreground="#c0392b")
        self.log.tag_configure("ok", foreground="#1e8449")
        self.log.tag_configure("cmd", foreground="#2471a3")

        self.tools = ttk.Label(self, foreground="#777")
        self.tools.pack(anchor="w", padx=10, pady=(0, 6))
        self._refresh_tools()

    def _tab_matrix(self, nb):
        f = ttk.Frame(nb, padding=8)
        f.columnconfigure(1, weight=1)
        r = 0
        ttk.Label(f, text="โหมด Matrix").grid(row=r, column=0, sticky="w", pady=3)
        cb = ttk.Combobox(f, textvariable=self.v["mode"], values=list(self.core.MODES), state="readonly", width=18)
        cb.grid(row=r, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda _e: self._on_mode_change())
        r += 1
        self.mode_help = ttk.Label(f, foreground="#555", wraplength=640, justify="left")
        self.mode_help.grid(row=r, column=1, sticky="w", pady=(0, 6))
        r += 1
        ttk.Label(f, text="Layout อินพุต").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Combobox(f, textvariable=self.v["layout"], values=["auto"] + list(self.core.LAYOUTS),
                     state="readonly", width=18).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(f, text="2.0 = ไม่ทำ matrix | 4.0q = L R Ls Rs | 4.0c = L R C S | 7.1 → 5.1 → 2.0",
                  foreground="#777").grid(row=r, column=1, sticky="w", pady=(0, 6))
        r += 1
        ttk.Label(f, text="LFE (.1)").grid(row=r, column=0, sticky="w", pady=3)
        lf = ttk.Frame(f)
        lf.grid(row=r, column=1, sticky="w")
        ttk.Radiobutton(lf, text="ตัดทิ้ง", value="drop", variable=self.v["lfe"]).pack(side="left")
        ttk.Radiobutton(lf, text="Mix เข้า Lt/Rt", value="mix", variable=self.v["lfe"]).pack(side="left", padx=10)
        ttk.Label(lf, text="ระดับ (dB)").pack(side="left")
        ttk.Entry(lf, textvariable=self.v["lfe_level"], width=7).pack(side="left", padx=4)
        r += 1
        ttk.Label(f, text="Low-pass (Hz)").grid(row=r, column=0, sticky="w", pady=3)
        lp = ttk.Frame(f)
        lp.grid(row=r, column=1, sticky="w")
        ttk.Entry(lp, textvariable=self.v["lpf"], width=9).pack(side="left")
        ttk.Label(lp, text="0 = ปิด", foreground="#777").pack(side="left", padx=6)
        r += 1
        ttk.Checkbutton(f, text="DPL1: ปิด band-limit 100 Hz–7 kHz ของ surround",
                        variable=self.b["no_sband"]).grid(row=r, column=1, sticky="w", pady=2)
        r += 1
        ttk.Checkbutton(f, text="ไม่ลด gain อัตโนมัติเมื่อ peak เกิน 0 dBFS",
                        variable=self.b["no_norm"]).grid(row=r, column=1, sticky="w", pady=2)
        return f

    def _tab_dee(self, nb):
        f = ttk.Frame(nb, padding=8)
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="Loudness").pack(side="left")
        ttk.Combobox(top, textvariable=self.v["loudness"],
                     values=["", "disabled", "measure_and_correct", "measure_only"],
                     state="readonly", width=20).pack(side="left", padx=6)
        ttk.Label(top, text="(ว่าง = อนุมานจากค่าที่กรอก / ค่าเริ่มต้นของ format)",
                  foreground="#777").pack(side="left")
        ttk.Label(f, text="ช่องว่าง = ใช้ค่าเริ่มต้น (สคริปต์ตั้งให้สอดคล้องกับโหมด matrix) "
                          "ค่า surround/downmix/dolby_surround_* DEE ฝังเป็น metadata เท่านั้น",
                  foreground="#555", wraplength=760, justify="left").pack(anchor="w", pady=4)
        outer = ttk.Frame(f)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, height=190, highlightthickness=0)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        self.pframe = ttk.Frame(canvas)
        self.pframe.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.pframe, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        wheel = lambda e: canvas.yview_scroll(int(-e.delta / 120) if e.delta else (1 if e.num == 5 else -1), "units")
        canvas.bind("<Enter>", lambda _e: (canvas.bind_all("<MouseWheel>", wheel),
                                           canvas.bind_all("<Button-4>", wheel), canvas.bind_all("<Button-5>", wheel)))
        canvas.bind("<Leave>", lambda _e: (canvas.unbind_all("<MouseWheel>"),
                                           canvas.unbind_all("<Button-4>"), canvas.unbind_all("<Button-5>")))
        return f

    def _tab_adv(self, nb):
        f = ttk.Frame(nb, padding=8)
        f.columnconfigure(1, weight=1)
        rows = [("Block size (frames)", "block", None), ("Context (frames)", "ctx", None),
                ("โฟลเดอร์ชั่วคราว", "work_dir", "dir"), ("โฟลเดอร์ DEE/FFmpeg (เพิ่มเข้า PATH)", "tool_dir", "dir"),
                ("บันทึก DEE job XML ที่", "save_xml", "save")]
        for i, (lab, key, kind) in enumerate(rows):
            ttk.Label(f, text=lab).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 8))
            ttk.Entry(f, textvariable=self.v[key]).grid(row=i, column=1, sticky="ew")
            if kind == "dir":
                ttk.Button(f, text="เลือก…", command=lambda k=key: self._pick_dir(k)).grid(row=i, column=2, padx=4)
            elif kind == "save":
                ttk.Button(f, text="เลือก…", command=self._pick_xml).grid(row=i, column=2, padx=4)
        ttk.Checkbutton(f, text="หยุดที่ไฟล์ Lt/Rt WAV (ไม่เรียก DEE) — ตั้งชื่อเอาต์พุตเป็น .wav",
                        variable=self.b["wav_only"], command=self._refresh_tools).grid(
            row=len(rows), column=1, sticky="w", pady=8)
        return f

    # ------------------------------------------------------------ พารามิเตอร์ DEE (สร้างจาก SPEC ของสคริปต์)
    def _effective_fmt(self):
        f = self.v["fmt"].get()
        if f != "auto":
            return f
        suf = Path(self.v["output"].get()).suffix.lower()
        return "ec3" if suf in (".ec3", ".eac3") else "ac3"

    def _on_fmt_change(self):
        fmt = self._effective_fmt()
        if fmt == self.cur_fmt:
            return
        for k, sv in self.pvars.items():
            self._saved_params[k] = sv.get()
        self.cur_fmt = fmt
        for w in self.pframe.winfo_children():
            w.destroy()
        spec = self.core.SPEC[fmt]
        keys = [k for k in PRIORITY if k in spec] + sorted(k for k in spec if k not in PRIORITY)
        self.pvars = {}
        for i, k in enumerate(keys):
            sv = tk.StringVar(value=self._saved_params.get(k, ""))
            self.pvars[k] = sv
            ttk.Label(self.pframe, text=k).grid(row=i, column=0, sticky="w", padx=(0, 10), pady=1)
            kind = spec[k][0]
            if kind == "enum":
                w = ttk.Combobox(self.pframe, textvariable=sv, values=[""] + spec[k][1], state="readonly", width=24)
                hint = ""
            elif kind == "bool":
                w = ttk.Combobox(self.pframe, textvariable=sv, values=["", "true", "false"], state="readonly", width=24)
                hint = ""
            else:
                w = ttk.Entry(self.pframe, textvariable=sv, width=26)
                if kind in ("int", "float"):
                    hint = f"{spec[k][1]} ถึง {spec[k][2]}"
                elif kind == "multi":
                    hint = "คั่นด้วย , ได้: " + "/".join(spec[k][1])
                else:
                    hint = ""
            w.grid(row=i, column=1, sticky="w", pady=1)
            if hint:
                ttk.Label(self.pframe, text=hint, foreground="#777").grid(row=i, column=2, sticky="w", padx=8)

    def _on_mode_change(self):
        self.mode_help.config(text=MODE_HELP.get(self.v["mode"].get(), ""))

    # ------------------------------------------------------------ dialogs
    def _pick_input(self):
        p = filedialog.askopenfilename(title="เลือกไฟล์เสียง", filetypes=[
            ("Audio", "*.wav *.flac *.w64 *.thd *.mlp *.ac3 *.eac3 *.ec3 *.dts *.mka *.mkv *.mp4 *.m4a *.aac *.mp3"),
            ("ทั้งหมด", "*.*")])
        if not p:
            return
        self.v["input"].set(p)
        if not self.v["output"].get():
            ext = ".wav" if self.b["wav_only"].get() else (".ec3" if self._effective_fmt() == "ec3" else ".ac3")
            self.v["output"].set(str(Path(p).with_name(Path(p).stem + "_ltrt" + ext)))
            self._on_fmt_change()

    def _pick_output(self):
        p = filedialog.asksaveasfilename(title="บันทึกเป็น", defaultextension=".ac3", filetypes=[
            ("AC-3", "*.ac3"), ("E-AC-3", "*.ec3 *.eac3"), ("WAV", "*.wav")])
        if p:
            self.v["output"].set(p)
            self._on_fmt_change()

    def _pick_dir(self, key):
        p = filedialog.askdirectory()
        if p:
            self.v[key].set(p)
            self._refresh_tools()

    def _pick_xml(self):
        p = filedialog.asksaveasfilename(defaultextension=".xml", filetypes=[("XML", "*.xml")])
        if p:
            self.v["save_xml"].set(p)

    # ------------------------------------------------------------ คำสั่ง
    def _env(self):
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        td = self.v["tool_dir"].get().strip()
        if td:
            env["PATH"] = td + os.pathsep + env.get("PATH", "")
        return env

    def _refresh_tools(self):
        path = self._env()["PATH"]
        ff = shutil.which("ffmpeg", path=path)
        de = shutil.which("dee", path=path)
        need_dee = not self.b["wav_only"].get()
        self.tools.config(text=f"ffmpeg: {'พบ' if ff else 'ไม่พบ (จำเป็นเฉพาะไฟล์ที่ไม่ใช่ WAV 48 kHz)'}   |   "
                               f"dee: {'พบ' if de else ('ไม่พบ' if need_dee else 'ไม่ต้องใช้')}")

    def _build_cmd(self):
        v = {k: x.get().strip() for k, x in self.v.items()}
        if not v["input"] or not Path(v["input"]).exists():
            raise ValueError("กรุณาเลือกไฟล์อินพุตที่มีอยู่จริง")
        if not v["output"]:
            raise ValueError("กรุณาระบุไฟล์เอาต์พุต")
        for k in ("lfe_level", "lpf"):
            try:
                float(v[k])
            except ValueError:
                raise ValueError(f"ค่า {k} ต้องเป็นตัวเลข")
        cmd = [sys.executable, "-u", str(SCRIPT), v["input"], v["output"], "--mode", v["mode"],
               "--lfe", v["lfe"], "--lfe-level", v["lfe_level"], "--lpf", v["lpf"],
               "--block-size", v["block"], "--context", v["ctx"]]
        if v["fmt"] != "auto":
            cmd += ["--format", v["fmt"]]
        if v["ec3ext"]:
            cmd += ["--ec3-ext", v["ec3ext"]]
        if v["layout"] != "auto":
            cmd += ["--layout", v["layout"]]
        if v["loudness"]:
            cmd += ["--loudness", v["loudness"]]
        if self.b["no_sband"].get():
            cmd.append("--no-s-bandlimit")
        if self.b["no_norm"].get():
            cmd.append("--no-normalize")
        if self.b["wav_only"].get():
            cmd.append("--wav-only")
        if v["save_xml"]:
            cmd += ["--save-xml", v["save_xml"]]
        if v["work_dir"]:
            cmd += ["--work-dir", v["work_dir"]]
        for k, sv in self.pvars.items():
            if sv.get().strip():
                cmd += ["--set", f"{k}={sv.get().strip()}"]
        return cmd

    def _copy_cmd(self):
        try:
            cmd = self._build_cmd()
        except ValueError as e:
            messagebox.showwarning("ข้อมูลไม่ครบ", str(e))
            return
        shown = subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(shlex.quote(c) for c in cmd)
        self.clipboard_clear()
        self.clipboard_append(shown)
        self.status.config(text="คัดลอกคำสั่งแล้ว")

    # ------------------------------------------------------------ run / log
    def _write(self, text, tag=None):
        self.log.config(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def _emit(self, token):
        m = PROGRESS_RE.match(token)
        if m:
            done, tot = int(m.group(2)), m.group(3)
            if tot and int(tot) > 0:
                self.pbar.config(mode="determinate", value=min(100, 100.0 * done / int(tot)))
            self.status.config(text=token.strip())
            self.log.config(state="normal")
            if self.prog_active:
                self.log.delete("end-1c linestart", "end-1c")
            self.log.insert("end", token)
            self.log.see("end")
            self.log.config(state="disabled")
            self.prog_active = True
            return
        if self.prog_active:
            self._write("\n")
            self.prog_active = False
        if token.startswith("[STAGE 3]"):
            self.pbar.config(mode="indeterminate")
            self.pbar.start(12)
        tag = "err" if "[ERROR]" in token or "[WARN]" in token else "ok" if "[COMPLETED]" in token or "[DONE]" in token else None
        self._write(token + "\n", tag)

    def _reader(self, proc):
        buf = ""
        while True:
            chunk = proc.stdout.read1(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", errors="replace")
            parts = re.split(r"\r\n|\r|\n", buf)
            buf = parts.pop()
            for p in parts:
                if p.strip():
                    self.q.put(("line", p))
        if buf.strip():
            self.q.put(("line", buf))
        self.q.put(("done", proc.wait()))

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "line":
                    self._emit(val)
                else:
                    self._finished(val)
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _run(self):
        if self.proc:
            return
        try:
            cmd = self._build_cmd()
        except ValueError as e:
            messagebox.showwarning("ข้อมูลไม่ครบ", str(e))
            return
        self._save_settings()
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.prog_active = False
        self._write("> " + (subprocess.list2cmdline(cmd) if os.name == "nt" else " ".join(shlex.quote(c) for c in cmd))
                    + "\n\n", "cmd")
        self.pbar.stop()
        self.pbar.config(mode="determinate", value=0)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         env=self._env(), creationflags=flags)
        except OSError as e:
            messagebox.showerror("เริ่มงานไม่ได้", str(e))
            return
        self.btn_run.config(state="disabled")
        self.btn_cancel.config(state="normal")
        self.status.config(text="กำลังทำงาน…")
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()

    def _cancel(self):
        if not self.proc:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                self.proc.terminate()
        except OSError:
            pass
        self.status.config(text="กำลังยกเลิก…")

    def _finished(self, rc):
        self.proc = None
        self.pbar.stop()
        self.pbar.config(mode="determinate", value=100 if rc == 0 else 0)
        if self.prog_active:
            self._write("\n")
            self.prog_active = False
        self.btn_run.config(state="normal")
        self.btn_cancel.config(state="disabled")
        if rc == 0:
            self.status.config(text="เสร็จสิ้น")
            out = Path(self.v["output"].get())
            if self.v["ec3ext"].get() and self._effective_fmt() == "ec3":
                out = out.with_suffix("." + self.v["ec3ext"].get())
            if out.exists():
                self._write(f"\nไฟล์ผลลัพธ์: {out}  ({out.stat().st_size / 1e6:.2f} MB)\n", "ok")
        else:
            self.status.config(text=f"ล้มเหลว (exit {rc})")
            self._write(f"\nจบการทำงานด้วย exit code {rc}\n", "err")

    def _close(self):
        if self.proc and not messagebox.askyesno("กำลังทำงาน", "ยกเลิกงานที่กำลังทำและออกจากโปรแกรม?"):
            return
        self._cancel()
        self._save_settings()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()