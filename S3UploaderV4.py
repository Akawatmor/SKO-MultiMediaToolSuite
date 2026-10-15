#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VersityGW S3 Uploader V4
- Concurrent small-file + multipart uploads in a single bounded worker pool
- Thread-safe GUI (workers never touch Tk; GUI polls engine snapshots)
- Per-part MD5 / multipart ETag verification, safe MOVE mode
"""

import hashlib
import json
import logging
import math
import os
import queue
import random
import socket
import stat
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter.scrolledtext import ScrolledText

import boto3
import botocore.httpsession
from botocore.awsrequest import AWSConnection
from botocore.config import Config
from botocore.exceptions import ClientError

# ---------- Drag & Drop (optional) ----------
DND_MODE = None
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    DND_MODE = "tkdnd"
except Exception:
    TkinterDnD = None
    DND_FILES = None
    try:
        import windnd
        DND_MODE = "windnd"
    except Exception:
        windnd = None

# ---------- Constants ----------
APP_NAME = "VersityGW S3 Uploader"
APP_VERSION = "V4"
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "S3UploaderV4.log")

MiB = 1024 * 1024
CHUNK_OPTIONS = ["5MB", "8MB", "16MB", "32MB", "64MB", "128MB",
                 "256MB", "512MB", "1024MB", "2048MB"]
TCP_BUF_OPTIONS = ["OS Auto", "1MB", "4MB", "8MB", "16MB"]
MIN_PART = 5 * MiB
MAX_PART = 5 * 1024 * MiB
MAX_PARTS = 10000
SEND_BLOCKSIZE = 1 * MiB
UI_TICK_MS = 200
MAX_LOG_LINES = 5000
MAX_TRANSFER_ROWS = 1500
DEFAULT_RETRIES = 6

NON_RETRY_CODES = {
    "AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "NoSuchBucket",
    "NoSuchUpload", "InvalidBucketName", "AllAccessDisabled", "InvalidRequest",
    "EntityTooLarge", "EntityTooSmall", "InvalidPart", "InvalidPartOrder",
    "MethodNotAllowed", "NotImplemented", "403", "404", "NoSuchKey",
}

# ---------- File logger ----------
logger = logging.getLogger("s3uploader")
logger.setLevel(logging.INFO)
try:
    _fh = RotatingFileHandler(LOG_PATH, maxBytes=5 * MiB, backupCount=3, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(_fh)
except Exception:
    pass

# ---------- Network tuning patches ----------
_TCP_TUNING = {"buf": 4 * MiB}

_orig_pool_kwargs = getattr(botocore.httpsession.URLLib3Session, "_get_pool_manager_kwargs", None)
if _orig_pool_kwargs is not None:
    def _patched_pool_kwargs(self, **extra):
        kw = _orig_pool_kwargs(self, **extra)
        opts = list(kw.get("socket_options") or [])
        if not any(o[:2] == (socket.IPPROTO_TCP, socket.TCP_NODELAY) for o in opts):
            opts.append((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1))
        buf = _TCP_TUNING.get("buf") or 0
        if buf > 0:
            drop = {(socket.SOL_SOCKET, socket.SO_SNDBUF), (socket.SOL_SOCKET, socket.SO_RCVBUF)}
            opts = [o for o in opts if o[:2] not in drop]
            opts.append((socket.SOL_SOCKET, socket.SO_SNDBUF, buf))
            opts.append((socket.SOL_SOCKET, socket.SO_RCVBUF, buf))
        kw["socket_options"] = opts  # keeps tcp_keepalive options from botocore
        return kw
    botocore.httpsession.URLLib3Session._get_pool_manager_kwargs = _patched_pool_kwargs

_orig_awsconn_init = AWSConnection.__init__
def _patched_awsconn_init(self, *a, **k):
    _orig_awsconn_init(self, *a, **k)
    try:
        self.blocksize = SEND_BLOCKSIZE  # default 8KB is CPU-bound at high speed
    except Exception:
        pass
AWSConnection.__init__ = _patched_awsconn_init


# ---------- Helpers ----------
class UploadCancelled(Exception):
    pass


class JobAborted(Exception):
    pass


class IntegrityError(Exception):
    pass


def human(n):
    n = float(max(n, 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def fmt_eta(sec):
    if sec is None or sec < 0 or sec > 360000:
        return "--:--:--"
    sec = int(sec)
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def parse_size(s, default=64 * MiB):
    s = (s or "").strip().upper().replace(" ", "")
    try:
        if s.endswith("GB"):
            return int(float(s[:-2]) * 1024 * MiB)
        if s.endswith("MB"):
            return int(float(s[:-2]) * MiB)
        return int(s)
    except ValueError:
        return default


def clean_etag(v):
    return (v or "").strip().strip('"').lower()


def err_code(e):
    if isinstance(e, ClientError):
        return str(e.response.get("Error", {}).get("Code", ""))
    return ""


def short_err(e, limit=300):
    if isinstance(e, ClientError):
        err = e.response.get("Error", {})
        msg = f"{err.get('Code', '')}: {err.get('Message', '')}"
    else:
        msg = f"{type(e).__name__}: {e}"
    return msg if len(msg) <= limit else msg[:limit] + "..."


def is_retryable(e):
    if isinstance(e, ClientError):
        return err_code(e) not in NON_RETRY_CODES
    if isinstance(e, (FileNotFoundError, PermissionError, IsADirectoryError)):
        return False
    return True


def build_endpoint(proto, ep):
    ep = ep.strip().rstrip("/")
    if "://" in ep:
        return ep
    return f"{proto}://{ep}"


def normalize_prefix(p):
    p = (p or "").strip().replace("\\", "/").lstrip("/")
    if p and not p.endswith("/"):
        p += "/"
    return p


def calc_part_size(size, chunk):
    part = max(MIN_PART, chunk)
    if math.ceil(size / part) > MAX_PARTS:
        part = math.ceil(size / MAX_PARTS / MiB) * MiB
    if part > MAX_PART:
        raise ValueError("ไฟล์ใหญ่เกินขีดจำกัด multipart ของ S3")
    return part


def make_client(cfg):
    _TCP_TUNING["buf"] = cfg.get("tcp_buf", 4 * MiB)
    if not cfg["verify_ssl"]:
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass
    base = dict(
        signature_version="s3v4",
        s3={"addressing_style": cfg["addressing"], "payload_signing_enabled": False},
        max_pool_connections=cfg["threads"] + 8,
        retries={"total_max_attempts": 1},  # we handle retries ourselves
        connect_timeout=20,
        read_timeout=300,
    )
    extras = [
        dict(tcp_keepalive=True, request_checksum_calculation="when_required",
             response_checksum_validation="when_required"),
        dict(tcp_keepalive=True),
        dict(),
    ]
    conf = None
    for ex in extras:
        try:
            conf = Config(**base, **ex)
            break
        except TypeError:
            continue
    session = boto3.session.Session()
    return session.client(
        "s3",
        endpoint_url=cfg["endpoint_url"],
        region_name=cfg["region"] or "us-east-1",
        aws_access_key_id=cfg["access_key"],
        aws_secret_access_key=cfg["secret_key"],
        verify=cfg["verify_ssl"],
        config=conf,
    )


def normalize_and_prune(existing, new_paths):
    """Dedupe, drop missing paths and anything already covered by a chosen folder."""
    items, seen, discarded = [], set(), []
    for raw in list(existing) + list(new_paths):
        if not raw:
            continue
        p = str(raw).strip().strip('"').strip("'")
        if not p:
            continue
        ap = os.path.normpath(os.path.abspath(p))
        k = os.path.normcase(ap)
        if k in seen:
            continue
        seen.add(k)
        if not os.path.exists(ap):
            discarded.append((ap, "ไม่พบ path"))
            continue
        items.append((ap, k, os.path.isdir(ap)))

    def under(child_k, parent_k):
        base = parent_k if parent_k.endswith(os.sep) else parent_k + os.sep
        return child_k.startswith(base)

    dirs = sorted([i for i in items if i[2]], key=lambda x: len(x[1]))
    kept_dirs = []
    for ap, k, _ in dirs:
        if any(under(k, kd[1]) for kd in kept_dirs):
            discarded.append((ap, "อยู่ในโฟลเดอร์ที่เลือกแล้ว"))
        else:
            kept_dirs.append((ap, k))
    kept_files = []
    for ap, k, is_dir in items:
        if is_dir:
            continue
        if any(under(k, kd[1]) for kd in kept_dirs):
            discarded.append((ap, "อยู่ในโฟลเดอร์ที่เลือกแล้ว"))
        else:
            kept_files.append(ap)
    return [d[0] for d in kept_dirs] + kept_files, discarded


# ---------- Streaming body with on-the-fly MD5 ----------
class PartStream:
    def __init__(self, path, offset, size, on_bytes, stop_event):
        self._path = path
        self._offset = offset
        self._size = size
        self._on_bytes = on_bytes
        self._stop = stop_event
        self._f = open(path, "rb")
        try:
            self._f.seek(offset)
        except Exception:
            self._f.close()
            raise
        self._pos = 0
        self.high = 0          # high-water mark reported to progress
        self._md5 = hashlib.md5()
        self._hashed = 0       # bytes hashed sequentially from 0 (-1 = invalid)

    def __len__(self):
        return self._size

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._pos

    def seek(self, offset, whence=os.SEEK_SET):
        if whence == os.SEEK_SET:
            new = offset
        elif whence == os.SEEK_CUR:
            new = self._pos + offset
        elif whence == os.SEEK_END:
            new = self._size + offset
        else:
            raise ValueError("Invalid whence")
        new = max(0, min(new, self._size))
        if new == 0:
            self._md5 = hashlib.md5()
            self._hashed = 0
        elif new != self._hashed:
            self._hashed = -1
        self._f.seek(self._offset + new)
        self._pos = new
        return new

    def read(self, amt=-1):
        if self._stop.is_set():
            raise UploadCancelled()
        remaining = self._size - self._pos
        if remaining <= 0:
            return b""
        if amt is None or amt < 0 or amt > remaining:
            amt = remaining
        data = self._f.read(amt)
        if not data:
            raise IOError(f"ไฟล์สั้นกว่าที่คาด (อาจถูกแก้ไขระหว่างอัปโหลด): {self._path}")
        if self._hashed == self._pos:
            self._md5.update(data)
            self._hashed += len(data)
        self._pos += len(data)
        if self._pos > self.high:
            delta = self._pos - self.high
            self.high = self._pos
            self._on_bytes(delta)
        return data

    def md5_hex(self):
        if self._hashed == self._size:
            return self._md5.hexdigest()
        h = hashlib.md5()
        with open(self._path, "rb") as f:
            f.seek(self._offset)
            left = self._size
            while left > 0:
                b = f.read(min(8 * MiB, left))
                if not b:
                    raise IOError("อ่านไฟล์ไม่ครบระหว่างคำนวณ MD5")
                h.update(b)
                left -= len(b)
        return h.hexdigest()

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass


# ---------- Engine state ----------
class FileTask:
    __slots__ = ("path", "key", "size")

    def __init__(self, path, key, size):
        self.path, self.key, self.size = path, key, size


class FileState:
    __slots__ = ("fid", "name", "size", "done", "status", "kind",
                 "parts_total", "parts_done", "closed")

    def __init__(self, fid, name, size):
        self.fid, self.name, self.size = fid, name, size
        self.done = 0
        self.status = "รอคิว"
        self.kind = "wait"
        self.parts_total = 0
        self.parts_done = 0
        self.closed = False


class MultipartJob:
    def __init__(self, task, fid, part_size, nparts, upload_id, st0):
        self.task, self.fid = task, fid
        self.part_size, self.nparts = part_size, nparts
        self.upload_id, self.st0 = upload_id, st0
        self.lock = threading.Lock()
        self.etags = [None] * nparts
        self.digests = [None] * nparts
        self.remaining = nparts
        self.failed = False
        self.closed = False


class UploadEngine:
    def __init__(self, cfg, sources, log_q):
        self.cfg = cfg
        self.sources = sources
        self.log_q = log_q
        self.bucket = cfg["bucket"]
        self.prefix = normalize_prefix(cfg["prefix"])
        self.move = cfg["move"]
        self.stop_event = threading.Event()
        self.run_event = threading.Event()
        self.run_event.set()
        self.finished = threading.Event()
        self.lock = threading.Lock()
        self.states = {}
        self.dirty = set()
        self._fid_seq = 0
        self.total_bytes = 0
        self.done_bytes = 0
        self.files_total = 0
        self.files_ok = 0
        self.files_failed = 0
        self.files_skipped = 0
        self.files_cancelled = 0
        self.phase = "idle"
        self._jobs_lock = threading.Lock()
        self._open_jobs = set()
        self.s3 = None
        self.ex = None
        self.slots = None
        self.thread = threading.Thread(target=self._run, name="engine", daemon=True)

    # ----- control -----
    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.run_event.set()

    def pause(self):
        self.run_event.clear()

    def resume(self):
        self.run_event.set()

    @property
    def paused(self):
        return not self.run_event.is_set()

    # ----- logging / state -----
    def log(self, msg, level="INFO"):
        logger.log(getattr(logging, level if level != "OK" else "INFO", logging.INFO), msg)
        self.log_q.put((level, time.strftime("%H:%M:%S"), msg))

    def _new_state(self, t):
        with self.lock:
            self._fid_seq += 1
            fid = self._fid_seq
            self.states[fid] = FileState(fid, os.path.basename(t.path), t.size)
            self.dirty.add(fid)
        return fid

    def _set(self, fid, **kw):
        with self.lock:
            st = self.states.get(fid)
            if st is None or st.closed:
                return
            for k, v in kw.items():
                setattr(st, k, v)
            self.dirty.add(fid)

    def _bump_part(self, fid):
        with self.lock:
            st = self.states.get(fid)
            if st is not None and not st.closed:
                st.parts_done += 1
                self.dirty.add(fid)

    def _add_bytes(self, fid, n):
        with self.lock:
            st = self.states.get(fid)
            if st is None or st.closed:
                return
            st.done += n
            self.done_bytes += n
            self.dirty.add(fid)

    def _close_state(self, fid, kind, status):
        with self.lock:
            st = self.states.get(fid)
            if st is None or st.closed:
                return False
            st.closed = True
            st.kind = kind
            st.status = status
            if kind == "ok":
                self.done_bytes += st.size - st.done
                st.done = st.size
                self.files_ok += 1
            else:
                self.done_bytes -= st.done
                self.total_bytes -= st.size
                if kind == "fail":
                    self.files_failed += 1
                elif kind == "skip":
                    self.files_skipped += 1
                else:
                    self.files_cancelled += 1
            self.dirty.add(fid)
            return True

    def pull_changes(self):
        with self.lock:
            ids, self.dirty = self.dirty, set()
            rows = []
            for fid in ids:
                st = self.states.get(fid)
                if st is None:
                    continue
                rows.append((st.fid, st.name, st.size, st.done, st.parts_done,
                             st.parts_total, st.status, st.kind))
                if st.closed:
                    del self.states[fid]
            summary = dict(total_bytes=self.total_bytes, done_bytes=self.done_bytes,
                           files_total=self.files_total, ok=self.files_ok,
                           failed=self.files_failed, skipped=self.files_skipped,
                           cancelled=self.files_cancelled, phase=self.phase)
        return rows, summary

    # ----- flow control -----
    def _wait_running(self):
        while not self.run_event.wait(0.25):
            if self.stop_event.is_set():
                raise UploadCancelled()
        if self.stop_event.is_set():
            raise UploadCancelled()

    def _with_retry(self, fn, what, attempts=None, honor_stop=True):
        attempts = attempts or self.cfg.get("retries", DEFAULT_RETRIES)
        for i in range(1, attempts + 1):
            if honor_stop:
                self._wait_running()
            try:
                return fn()
            except (UploadCancelled, JobAborted):
                raise
            except Exception as e:
                if honor_stop and self.stop_event.is_set():
                    raise UploadCancelled() from e
                if i >= attempts or not is_retryable(e):
                    raise
                delay = min(30.0, 2 ** (i - 1)) * (0.5 + random.random())
                self.log(f"[RETRY {i}/{attempts - 1}] {what}: {short_err(e)} (รอ {delay:.1f}s)", "WARN")
                if honor_stop:
                    if self.stop_event.wait(delay):
                        raise UploadCancelled()
                else:
                    time.sleep(delay)

    def _submit(self, fn, *args):
        while not self.slots.acquire(timeout=0.25):
            if self.stop_event.is_set():
                raise UploadCancelled()
        if self.stop_event.is_set():
            self.slots.release()
            raise UploadCancelled()
        try:
            fut = self.ex.submit(fn, *args)
        except Exception:
            self.slots.release()
            raise
        fut.add_done_callback(self._on_future_done)

    def _on_future_done(self, fut):
        self.slots.release()
        try:
            exc = fut.exception()
        except Exception:
            exc = None
        if exc is not None:
            self.log(f"[BUG] exception ที่ไม่ได้จัดการใน worker: {exc!r}", "ERROR")

    # ----- main -----
    def _run(self):
        try:
            self.phase = "scan"
            tasks = self._scan()
            with self.lock:
                self.files_total = len(tasks)
                self.total_bytes = sum(t.size for t in tasks)
            self.log(f"พบ {len(tasks):,} ไฟล์ รวม {human(self.total_bytes)}")
            if not tasks or self.stop_event.is_set():
                return
            self.phase = "upload"
            self.s3 = make_client(self.cfg)
            threads = self.cfg["threads"]
            chunk = self.cfg["chunk_bytes"]
            self.slots = threading.BoundedSemaphore(threads * 2)
            mode = "MOVE" if self.move else "COPY"
            self.log(f"เริ่มอัปโหลด: {threads} threads, chunk {human(chunk)}, โหมด {mode}")
            with ThreadPoolExecutor(max_workers=threads, thread_name_prefix="s3up") as ex:
                self.ex = ex
                try:
                    for t in tasks:
                        if self.stop_event.is_set():
                            break
                        self._wait_running()
                        if t.size > chunk:
                            self._start_multipart(t, chunk)
                        else:
                            fid = self._new_state(t)
                            try:
                                self._submit(self._small_task, t, fid)
                            except UploadCancelled:
                                self._close_state(fid, "cancel", "ยกเลิก")
                                raise
                except UploadCancelled:
                    pass
            self._abort_open_jobs()
            if (self.move and self.cfg.get("remove_empty_dirs")
                    and not self.stop_event.is_set()):
                self._remove_empty_dirs()
        except Exception as e:
            self.log(f"[FATAL] {short_err(e, 1000)}", "ERROR")
            logger.exception("engine fatal")
            self._abort_open_jobs()
        finally:
            self.phase = "done"
            with self.lock:
                s = (self.files_ok, self.files_failed, self.files_skipped,
                     self.files_cancelled, self.files_total)
            not_started = s[4] - sum(s[:4])
            level = "ERROR" if s[1] else "OK"
            self.log(f"สรุป: สำเร็จ {s[0]:,} | ล้มเหลว {s[1]:,} | ข้าม {s[2]:,} | "
                     f"ยกเลิก {s[3]:,} | ไม่ได้เริ่ม {max(not_started, 0):,}", level)
            self.finished.set()

    # ----- scanning -----
    def _iter_files(self, root):
        stack = [root]
        while stack:
            if self.stop_event.is_set():
                return
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError as ex:
                self.log(f"[WARN] อ่านโฟลเดอร์ไม่ได้ {d}: {ex}", "WARN")
                continue
            subdirs = []
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        subdirs.append(e.path)
                    elif e.is_file():
                        yield e.path, e.stat().st_size
                except OSError as ex:
                    self.log(f"[WARN] ข้าม {e.path}: {ex}", "WARN")
            stack.extend(reversed(subdirs))

    def _scan(self):
        self.log("กำลังสแกนไฟล์...")
        tasks, keys = [], set()
        include = self.cfg.get("include_folder_name", True)

        def add(path, key, size):
            if key in keys:
                self.log(f"[WARN] key ซ้ำ ข้าม: {path} -> {key}", "WARN")
                return
            keys.add(key)
            tasks.append(FileTask(path, key, size))

        for src in self.sources:
            if self.stop_event.is_set():
                break
            src = os.path.normpath(src)
            if os.path.isfile(src):
                try:
                    add(src, self.prefix + os.path.basename(src), os.path.getsize(src))
                except OSError as e:
                    self.log(f"[WARN] ข้าม {src}: {e}", "WARN")
            elif os.path.isdir(src):
                base = os.path.dirname(src) if include else src
                for full, size in self._iter_files(src):
                    rel = os.path.relpath(full, base).replace(os.sep, "/")
                    add(full, self.prefix + rel, size)
            else:
                self.log(f"[WARN] ไม่พบ: {src}", "WARN")
        return tasks

    # ----- helpers -----
    def _exists_same(self, t):
        def fn():
            try:
                h = self.s3.head_object(Bucket=self.bucket, Key=t.key)
                return int(h.get("ContentLength", -1)) == t.size
            except ClientError as e:
                if err_code(e) in ("404", "NoSuchKey", "NotFound"):
                    return False
                raise
        return self._with_retry(fn, f"HEAD {t.key}", attempts=3)

    def _fail_file(self, fid, t, e):
        self.log(f"[FAIL] {t.path}: {short_err(e)}", "ERROR")
        self._close_state(fid, "fail", f"ล้มเหลว: {short_err(e, 80)}")

    def _safe_remove(self, path):
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except Exception:
            pass
        os.remove(path)

    def _finish_ok(self, fid, t, st0, head_check=False):
        status = "สำเร็จ"
        if self.move:
            try:
                st1 = os.stat(t.path)
                if (st1.st_size, st1.st_mtime_ns) != (st0.st_size, st0.st_mtime_ns):
                    status = "สำเร็จ (ไม่ลบ: ไฟล์ถูกแก้ไข)"
                    self.log(f"[WARN] ไฟล์ถูกแก้ไขระหว่างอัปโหลด ไม่ลบต้นทาง: {t.path}", "WARN")
                else:
                    if head_check:
                        h = self._with_retry(
                            lambda: self.s3.head_object(Bucket=self.bucket, Key=t.key),
                            f"HEAD {t.key}", attempts=3, honor_stop=False)
                        if int(h.get("ContentLength", -1)) != t.size:
                            raise IntegrityError("ขนาดบนเซิร์ฟเวอร์ไม่ตรงกับต้นทาง")
                    self._safe_remove(t.path)
                    status = "ย้ายแล้ว"
                    self.log(f"[MOVE] ลบต้นทาง: {t.path}")
            except IntegrityError as e:
                self._fail_file(fid, t, e)
                return
            except Exception as e:
                status = "สำเร็จ (ลบต้นทางไม่ได้)"
                self.log(f"[WARN] ลบต้นทางไม่ได้ {t.path}: {e}", "WARN")
        self._close_state(fid, "ok", status)

    # ----- small file -----
    def _small_task(self, t, fid):
        try:
            self._wait_running()
            if self.cfg.get("skip_existing") and self._exists_same(t):
                self._close_state(fid, "skip", "ข้าม (มีอยู่แล้ว)")
                return
            st0 = os.stat(t.path)
            self._set(fid, status="กำลังอัปโหลด", kind="run")

            def attempt():
                s = PartStream(t.path, 0, t.size, lambda n: self._add_bytes(fid, n), self.stop_event)
                ok = False
                try:
                    resp = self.s3.put_object(Bucket=self.bucket, Key=t.key, Body=s,
                                              ContentLength=t.size)
                    local = s.md5_hex()
                    server = clean_etag(resp.get("ETag"))
                    if len(server) == 32 and server != local:
                        raise IntegrityError(f"ETag ไม่ตรง local={local} server={server}")
                    ok = True
                    return resp
                finally:
                    if not ok:
                        self._add_bytes(fid, -s.high)
                    s.close()

            self._with_retry(attempt, f"PUT {t.key}")
            self._finish_ok(fid, t, st0)
        except UploadCancelled:
            self._close_state(fid, "cancel", "ยกเลิก")
        except Exception as e:
            self._fail_file(fid, t, e)

    # ----- multipart -----
    def _start_multipart(self, t, chunk):
        fid = self._new_state(t)
        try:
            if self.cfg.get("skip_existing") and self._exists_same(t):
                self._close_state(fid, "skip", "ข้าม (มีอยู่แล้ว)")
                return
            part_size = calc_part_size(t.size, chunk)
            nparts = math.ceil(t.size / part_size)
            st0 = os.stat(t.path)
            self._set(fid, status="เริ่ม multipart", kind="run", parts_total=nparts)
            resp = self._with_retry(
                lambda: self.s3.create_multipart_upload(Bucket=self.bucket, Key=t.key),
                f"CREATE {t.key}")
            job = MultipartJob(t, fid, part_size, nparts, resp["UploadId"], st0)
            with self._jobs_lock:
                self._open_jobs.add(job)
        except UploadCancelled:
            self._close_state(fid, "cancel", "ยกเลิก")
            raise
        except Exception as e:
            self._fail_file(fid, t, e)
            return
        if part_size != chunk:
            self.log(f"[INFO] {os.path.basename(t.path)}: ปรับ part size เป็น {human(part_size)} "
                     f"(จำกัด {MAX_PARTS:,} parts)")
        self._set(fid, status="กำลังอัปโหลด")
        for p in range(1, nparts + 1):
            if job.failed:
                break
            self._submit(self._part_task, job, p)  # UploadCancelled propagates to engine loop

    def _part_task(self, job, p):
        t = job.task
        if job.failed:
            return
        off = (p - 1) * job.part_size
        size = min(job.part_size, t.size - off)

        def attempt():
            if job.failed:
                raise JobAborted()
            s = PartStream(t.path, off, size, lambda n: self._add_bytes(job.fid, n), self.stop_event)
            ok = False
            try:
                resp = self.s3.upload_part(Bucket=self.bucket, Key=t.key, PartNumber=p,
                                           UploadId=job.upload_id, Body=s, ContentLength=size)
                local = s.md5_hex()
                server = clean_etag(resp.get("ETag"))
                if len(server) == 32 and server != local:
                    raise IntegrityError(f"ETag part {p} ไม่ตรง local={local} server={server}")
                ok = True
                return (server or local), bytes.fromhex(local)
            finally:
                if not ok:
                    self._add_bytes(job.fid, -s.high)
                s.close()

        try:
            etag, digest = self._with_retry(attempt, f"{os.path.basename(t.path)} part {p}/{job.nparts}")
        except JobAborted:
            return
        except UploadCancelled:
            self._fail_job(job, None)
            return
        except Exception as e:
            self._fail_job(job, e)
            return
        with job.lock:
            if job.failed:
                return
            job.etags[p - 1] = etag
            job.digests[p - 1] = digest
            job.remaining -= 1
            last = job.remaining == 0
        self._bump_part(job.fid)
        if last:
            self._complete_job(job)

    def _complete_job(self, job):
        with job.lock:
            if job.failed or job.closed:
                return
            job.closed = True
        with self._jobs_lock:
            self._open_jobs.discard(job)
        t = job.task
        parts = [{"PartNumber": i + 1, "ETag": f'"{e}"'} for i, e in enumerate(job.etags)]
        expected = hashlib.md5(b"".join(job.digests)).hexdigest() + f"-{job.nparts}"
        self._set(job.fid, status="กำลังรวมไฟล์ (complete)")
        tries = [0]

        def attempt():
            tries[0] += 1
            try:
                return self.s3.complete_multipart_upload(
                    Bucket=self.bucket, Key=t.key, UploadId=job.upload_id,
                    MultipartUpload={"Parts": parts})
            except ClientError as e:
                # previous attempt may have succeeded but the response was lost
                if tries[0] > 1 and err_code(e) == "NoSuchUpload":
                    h = self.s3.head_object(Bucket=self.bucket, Key=t.key)
                    if clean_etag(h.get("ETag")) == expected:
                        return h
                raise

        try:
            resp = self._with_retry(attempt, f"COMPLETE {t.key}", honor_stop=False)
            server = clean_etag(resp.get("ETag"))
            if server and server != expected:
                raise IntegrityError(f"Multipart ETag ไม่ตรง expected={expected} server={server}")
        except Exception as e:
            self._fail_file(job.fid, t, e)
            try:
                self.s3.abort_multipart_upload(Bucket=self.bucket, Key=t.key, UploadId=job.upload_id)
            except Exception:
                pass
            return
        self._finish_ok(job.fid, t, job.st0, head_check=True)

    def _fail_job(self, job, err):
        with job.lock:
            if job.failed or job.closed:
                return
            job.failed = True
        if err is None:
            self._close_state(job.fid, "cancel", "ยกเลิก")
        else:
            self._fail_file(job.fid, job.task, err)
        self._abort_job(job)

    def _abort_job(self, job):
        with job.lock:
            if job.closed:
                return
            job.closed = True
        with self._jobs_lock:
            self._open_jobs.discard(job)
        t = job.task
        try:
            self._with_retry(
                lambda: self.s3.abort_multipart_upload(Bucket=self.bucket, Key=t.key,
                                                       UploadId=job.upload_id),
                f"ABORT {t.key}", attempts=3, honor_stop=False)
            self.log(f"[ABORT] ยกเลิก multipart: {t.key}", "WARN")
        except Exception as e:
            if err_code(e) != "NoSuchUpload":
                self.log(f"[WARN] abort ไม่สำเร็จ (UploadId ค้าง: {job.upload_id}): {short_err(e)}", "WARN")

    def _abort_open_jobs(self):
        with self._jobs_lock:
            jobs = list(self._open_jobs)
        for job in jobs:
            self._fail_job(job, None)

    def _remove_empty_dirs(self):
        for src in self.sources:
            if not os.path.isdir(src):
                continue
            root_k = os.path.normcase(os.path.abspath(src))
            for dirpath, _, _ in os.walk(src, topdown=False):
                if os.path.normcase(os.path.abspath(dirpath)) == root_k:
                    continue
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass


# ---------- GUI ----------
C_BG = "#f3f5f9"
C_CARD = "#ffffff"
C_HEADER = "#0f172a"
C_ACCENT = "#2563eb"
C_TEXT = "#1f2937"
C_MUTED = "#6b7280"
C_OK = "#15803d"
C_FAIL = "#b91c1c"
C_WARN = "#b45309"


def pick_font():
    import tkinter.font as tkfont
    fams = set(tkfont.families())
    for f in ("Leelawadee UI", "Segoe UI", "Tahoma", "Noto Sans Thai", "DejaVu Sans"):
        if f in fams:
            return f
    return "TkDefaultFont"


class PastePathsDialog(tk.Toplevel):
    def __init__(self, master, font):
        super().__init__(master)
        self.title("วาง path (บรรทัดละ 1 รายการ)")
        self.geometry("620x360")
        self.transient(master)
        self.grab_set()
        self.result = []
        ttk.Label(self, text="วาง path ของไฟล์หรือโฟลเดอร์ บรรทัดละ 1 รายการ").pack(anchor="w", padx=10, pady=(10, 4))
        self.txt = tk.Text(self, font=(font, 10), wrap="none")
        self.txt.pack(fill="both", expand=True, padx=10)
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=10)
        ttk.Button(bar, text="ยกเลิก", command=self.destroy).pack(side="right")
        ttk.Button(bar, text="เพิ่ม", style="Accent.TButton", command=self._ok).pack(side="right", padx=6)
        self.txt.focus_set()

    def _ok(self):
        self.result = [l.strip() for l in self.txt.get("1.0", "end").splitlines() if l.strip()]
        self.destroy()


class App:
    def __init__(self, root):
        self.root = root
        self.font = pick_font()
        root.title(f"{APP_NAME} {APP_VERSION}")
        root.geometry("1320x840")
        root.minsize(1100, 700)
        root.configure(bg=C_BG)

        self.config_data = {"active_site": None, "sites": {}}
        self.sources = []
        self.engine = None
        self.log_q = queue.Queue()
        self.ui_q = queue.Queue()
        self.rows = {}
        self.finished_rows = deque()
        self._lockables = []
        self._closing = False
        self._finish_handled = True
        self._speed = 0.0
        self._last_bytes = 0
        self._last_t = time.monotonic()

        self._init_vars()
        self._init_style()
        self._build_ui()
        self._setup_dnd()
        self._load_config()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(UI_TICK_MS, self._tick)

    # ----- setup -----
    def _init_vars(self):
        self.site_var = tk.StringVar()
        self.proto_var = tk.StringVar(value="https")
        self.endpoint_var = tk.StringVar()
        self.region_var = tk.StringVar(value="us-east-1")
        self.access_var = tk.StringVar()
        self.secret_var = tk.StringVar()
        self.bucket_var = tk.StringVar()
        self.prefix_var = tk.StringVar()
        self.addr_var = tk.StringVar(value="path")
        self.verify_var = tk.BooleanVar(value=True)
        self.chunk_var = tk.StringVar(value="64MB")
        self.threads_var = tk.StringVar(value="16")
        self.mode_var = tk.StringVar(value="COPY")
        self.skip_var = tk.BooleanVar(value=False)
        self.incl_var = tk.BooleanVar(value=True)
        self.rmempty_var = tk.BooleanVar(value=False)
        self.tcpbuf_var = tk.StringVar(value="4MB")
        self.show_secret_var = tk.BooleanVar(value=False)

    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        f = self.font
        st.configure(".", background=C_BG, foreground=C_TEXT, font=(f, 10))
        st.configure("TFrame", background=C_BG)
        st.configure("Card.TFrame", background=C_CARD)
        st.configure("TLabelframe", background=C_CARD, bordercolor="#dbe1ea", relief="solid")
        st.configure("TLabelframe.Label", background=C_CARD, foreground=C_ACCENT, font=(f, 10, "bold"))
        st.configure("TLabel", background=C_CARD)
        st.configure("Muted.TLabel", background=C_CARD, foreground=C_MUTED, font=(f, 9))
        st.configure("Foot.TLabel", background=C_BG, foreground=C_TEXT, font=(f, 10))
        st.configure("FootBold.TLabel", background=C_BG, foreground=C_TEXT, font=(f, 10, "bold"))
        st.configure("TCheckbutton", background=C_CARD)
        st.configure("TRadiobutton", background=C_CARD)
        st.configure("TButton", padding=(10, 5))
        st.configure("Accent.TButton", background=C_ACCENT, foreground="white", font=(f, 10, "bold"))
        st.map("Accent.TButton", background=[("disabled", "#93c5fd"), ("active", "#1d4ed8")])
        st.configure("Danger.TButton", background="#dc2626", foreground="white", font=(f, 10, "bold"))
        st.map("Danger.TButton", background=[("disabled", "#fca5a5"), ("active", "#b91c1c")])
        st.configure("Treeview", rowheight=24, background=C_CARD, fieldbackground=C_CARD)
        st.configure("Treeview.Heading", font=(f, 10, "bold"), background="#e5e9f0")
        st.configure("TNotebook", background=C_BG)
        st.configure("TNotebook.Tab", padding=(14, 6), font=(f, 10))
        st.configure("Big.Horizontal.TProgressbar", thickness=18, background=C_ACCENT)

    def _lockable(self, w, normal="normal"):
        self._lockables.append((w, normal))
        return w

    def _build_ui(self):
        f = self.font
        header = tk.Frame(self.root, bg=C_HEADER, height=54)
        header.pack(side="top", fill="x")
        tk.Label(header, text=APP_NAME, bg=C_HEADER, fg="white",
                 font=(f, 15, "bold")).pack(side="left", padx=(16, 8), pady=10)
        tk.Label(header, text=APP_VERSION, bg=C_ACCENT, fg="white",
                 font=(f, 9, "bold"), padx=8, pady=2).pack(side="left")
        self.status_lbl = tk.Label(header, text="พร้อมใช้งาน", bg=C_HEADER, fg="#93c5fd", font=(f, 10, "bold"))
        self.status_lbl.pack(side="right", padx=16)
        dnd_txt = {"tkdnd": "Drag & Drop: tkinterdnd2", "windnd": "Drag & Drop: windnd"}.get(DND_MODE, "Drag & Drop: ไม่มี")
        tk.Label(header, text=dnd_txt, bg=C_HEADER, fg="#64748b", font=(f, 9)).pack(side="right", padx=8)

        self._build_footer()

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(side="top", fill="both", expand=True, padx=10, pady=(10, 0))
        left = ttk.Frame(body, width=390)
        right = ttk.Frame(body)
        body.add(left, weight=0)
        body.add(right, weight=1)
        self._build_connection(left)
        self._build_options(left)

        rpane = ttk.Panedwindow(right, orient="vertical")
        rpane.pack(fill="both", expand=True, padx=(10, 0))
        qf = ttk.Labelframe(rpane, text=" คิวต้นทาง ", padding=8)
        rpane.add(qf, weight=1)
        self._build_queue(qf)
        nb = ttk.Notebook(rpane)
        rpane.add(nb, weight=2)
        self._build_transfers(nb)
        self._build_log(nb)

    def _build_connection(self, parent):
        lf = ttk.Labelframe(parent, text=" การเชื่อมต่อ ", padding=10)
        lf.pack(fill="x")
        lf.columnconfigure(1, weight=1)

        top = ttk.Frame(lf, style="Card.TFrame")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.site_cb = self._lockable(ttk.Combobox(top, textvariable=self.site_var, state="readonly", width=18), "readonly")
        self.site_cb.pack(side="left", fill="x", expand=True)
        self.site_cb.bind("<<ComboboxSelected>>", lambda e: self._on_site_selected())
        for txt, cmd in (("ใหม่", self._site_new), ("บันทึก", self._site_save), ("ลบ", self._site_delete)):
            self._lockable(ttk.Button(top, text=txt, width=6, command=cmd)).pack(side="left", padx=(4, 0))

        r = 1

        def row(label, widget):
            nonlocal r
            ttk.Label(lf, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            widget.grid(row=r, column=1, sticky="ew", pady=3)
            r += 1

        epf = ttk.Frame(lf, style="Card.TFrame")
        self._lockable(ttk.Combobox(epf, textvariable=self.proto_var, values=["https", "http"],
                                    state="readonly", width=6), "readonly").pack(side="left")
        self._lockable(ttk.Entry(epf, textvariable=self.endpoint_var)).pack(side="left", fill="x", expand=True, padx=(4, 0))
        row("Endpoint", epf)
        row("Region", self._lockable(ttk.Entry(lf, textvariable=self.region_var)))
        row("Access Key", self._lockable(ttk.Entry(lf, textvariable=self.access_var)))
        skf = ttk.Frame(lf, style="Card.TFrame")
        self.secret_entry = self._lockable(ttk.Entry(skf, textvariable=self.secret_var, show="•"))
        self.secret_entry.pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(skf, text="แสดง", variable=self.show_secret_var,
                        command=lambda: self.secret_entry.configure(
                            show="" if self.show_secret_var.get() else "•")).pack(side="left", padx=(4, 0))
        row("Secret Key", skf)
        row("Bucket", self._lockable(ttk.Entry(lf, textvariable=self.bucket_var)))
        row("Prefix", self._lockable(ttk.Entry(lf, textvariable=self.prefix_var)))
        row("Addressing", self._lockable(ttk.Combobox(lf, textvariable=self.addr_var,
                                                      values=["path", "virtual", "auto"], state="readonly"), "readonly"))
        row("", self._lockable(ttk.Checkbutton(lf, text="ตรวจสอบใบรับรอง SSL", variable=self.verify_var)))
        self.test_btn = ttk.Button(lf, text="ทดสอบการเชื่อมต่อ", command=self._test_connection)
        self.test_btn.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def _build_options(self, parent):
        lf = ttk.Labelframe(parent, text=" การอัปโหลด ", padding=10)
        lf.pack(fill="x", pady=(10, 0))
        lf.columnconfigure(1, weight=1)
        ttk.Label(lf, text="Chunk size").grid(row=0, column=0, sticky="w", pady=3)
        self._lockable(ttk.Combobox(lf, textvariable=self.chunk_var, values=CHUNK_OPTIONS,
                                    state="readonly", width=10), "readonly").grid(row=0, column=1, sticky="w")
        ttk.Label(lf, text="Threads").grid(row=1, column=0, sticky="w", pady=3)
        self._lockable(ttk.Spinbox(lf, from_=1, to=128, textvariable=self.threads_var, width=8)).grid(row=1, column=1, sticky="w")
        ttk.Label(lf, text="TCP buffer").grid(row=2, column=0, sticky="w", pady=3)
        self._lockable(ttk.Combobox(lf, textvariable=self.tcpbuf_var, values=TCP_BUF_OPTIONS,
                                    state="readonly", width=10), "readonly").grid(row=2, column=1, sticky="w")
        ttk.Label(lf, text="โหมด").grid(row=3, column=0, sticky="w", pady=3)
        mf = ttk.Frame(lf, style="Card.TFrame")
        mf.grid(row=3, column=1, sticky="w")
        self._lockable(ttk.Radiobutton(mf, text="COPY (เก็บต้นทาง)", value="COPY", variable=self.mode_var)).pack(side="left")
        self._lockable(ttk.Radiobutton(mf, text="MOVE (ลบต้นทาง)", value="MOVE", variable=self.mode_var)).pack(side="left", padx=(8, 0))
        self._lockable(ttk.Checkbutton(lf, text="ข้ามไฟล์ที่มีอยู่แล้ว (ขนาดเท่ากัน) - ใช้ resume ได้",
                                       variable=self.skip_var)).grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 2))
        self._lockable(ttk.Checkbutton(lf, text="ใส่ชื่อโฟลเดอร์ต้นทางใน key",
                                       variable=self.incl_var)).grid(row=5, column=0, columnspan=2, sticky="w", pady=2)
        self._lockable(ttk.Checkbutton(lf, text="ลบโฟลเดอร์ว่างหลัง MOVE",
                                       variable=self.rmempty_var)).grid(row=6, column=0, columnspan=2, sticky="w", pady=2)

    def _build_queue(self, parent):
        bar = ttk.Frame(parent, style="Card.TFrame")
        bar.pack(fill="x", pady=(0, 6))
        for txt, cmd in (("เพิ่มไฟล์", self._add_files), ("เพิ่มโฟลเดอร์", self._add_folder),
                         ("วาง path...", self._paste_paths), ("ลบที่เลือก", self._remove_selected),
                         ("ล้างทั้งหมด", self._clear_sources)):
            self._lockable(ttk.Button(bar, text=txt, command=cmd)).pack(side="left", padx=(0, 4))
        self.queue_count = ttk.Label(bar, text="0 รายการ", style="Muted.TLabel")
        self.queue_count.pack(side="right")

        wrap = ttk.Frame(parent, style="Card.TFrame")
        wrap.pack(fill="both", expand=True)
        self.queue_tree = ttk.Treeview(wrap, columns=("type", "path"), show="headings", selectmode="extended")
        self.queue_tree.heading("type", text="ประเภท")
        self.queue_tree.heading("path", text="Path")
        self.queue_tree.column("type", width=90, stretch=False, anchor="center")
        self.queue_tree.column("path", width=600)
        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.queue_tree.yview)
        self.queue_tree.configure(yscrollcommand=vs.set)
        self.queue_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.queue_tree.bind("<Delete>", lambda e: self._remove_selected())
        hint = "ลากไฟล์หรือโฟลเดอร์มาวางที่นี่ได้" if DND_MODE else "ติดตั้ง tkinterdnd2 เพื่อใช้ Drag & Drop"
        self.drop_hint = ttk.Label(parent, text=hint, style="Muted.TLabel")
        self.drop_hint.pack(anchor="w", pady=(4, 0))

    def _build_transfers(self, nb):
        frm = ttk.Frame(nb, style="Card.TFrame", padding=6)
        nb.add(frm, text="  การถ่ายโอน  ")
        cols = ("name", "size", "progress", "parts", "status")
        self.xfer_tree = ttk.Treeview(frm, columns=cols, show="headings")
        for c, txt, w, anchor in (("name", "ไฟล์", 380, "w"), ("size", "ขนาด", 100, "e"),
                                  ("progress", "ความคืบหน้า", 100, "e"), ("parts", "Parts", 90, "center"),
                                  ("status", "สถานะ", 300, "w")):
            self.xfer_tree.heading(c, text=txt)
            self.xfer_tree.column(c, width=w, anchor=anchor, stretch=(c in ("name", "status")))
        self.xfer_tree.tag_configure("run", foreground=C_ACCENT)
        self.xfer_tree.tag_configure("ok", foreground=C_OK)
        self.xfer_tree.tag_configure("fail", foreground=C_FAIL)
        self.xfer_tree.tag_configure("skip", foreground=C_MUTED)
        self.xfer_tree.tag_configure("cancel", foreground=C_WARN)
        vs = ttk.Scrollbar(frm, orient="vertical", command=self.xfer_tree.yview)
        self.xfer_tree.configure(yscrollcommand=vs.set)
        self.xfer_tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

    def _build_log(self, nb):
        frm = ttk.Frame(nb, style="Card.TFrame", padding=6)
        nb.add(frm, text="  Log  ")
        mono = "Consolas" if sys.platform.startswith("win") else "Courier"
        self.log_txt = ScrolledText(frm, font=(mono, 9), state="disabled", wrap="none",
                                    bg="#0b1220", fg="#cbd5e1", insertbackground="white")
        self.log_txt.pack(fill="both", expand=True)
        self.log_txt.tag_configure("INFO", foreground="#cbd5e1")
        self.log_txt.tag_configure("OK", foreground="#4ade80")
        self.log_txt.tag_configure("WARN", foreground="#fbbf24")
        self.log_txt.tag_configure("ERROR", foreground="#f87171")

    def _build_footer(self):
        foot = ttk.Frame(self.root, padding=(10, 8))
        foot.pack(side="bottom", fill="x")
        self.pbar = ttk.Progressbar(foot, mode="determinate", maximum=1000, style="Big.Horizontal.TProgressbar")
        self.pbar.pack(fill="x")
        info = ttk.Frame(foot)
        info.pack(fill="x", pady=(6, 0))
        self.lbl_pct = ttk.Label(info, text="0.0%", style="FootBold.TLabel", width=7)
        self.lbl_pct.pack(side="left")
        self.lbl_bytes = ttk.Label(info, text="0 B / 0 B", style="Foot.TLabel")
        self.lbl_bytes.pack(side="left", padx=(8, 16))
        self.lbl_speed = ttk.Label(info, text="ความเร็ว: -", style="Foot.TLabel")
        self.lbl_speed.pack(side="left", padx=(0, 16))
        self.lbl_eta = ttk.Label(info, text="ETA: --:--:--", style="Foot.TLabel")
        self.lbl_eta.pack(side="left", padx=(0, 16))
        self.lbl_files = ttk.Label(info, text="ไฟล์: 0/0", style="Foot.TLabel")
        self.lbl_files.pack(side="left")
        self.stop_btn = ttk.Button(info, text="หยุด", style="Danger.TButton", command=self._stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.pause_btn = ttk.Button(info, text="หยุดชั่วคราว", command=self._toggle_pause, state="disabled")
        self.pause_btn.pack(side="right", padx=6)
        self.start_btn = ttk.Button(info, text="เริ่มอัปโหลด", style="Accent.TButton", command=self._start)
        self.start_btn.pack(side="right")

    def _setup_dnd(self):
        if DND_MODE == "tkdnd":
            for w in (self.queue_tree, self.drop_hint):
                try:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", self._on_tkdnd_drop)
                except Exception:
                    pass
        elif DND_MODE == "windnd":
            def cb(files):
                paths = []
                for x in files:
                    if isinstance(x, bytes):
                        try:
                            x = x.decode("utf-8")
                        except UnicodeDecodeError:
                            x = x.decode("mbcs", errors="replace")
                    paths.append(x)
                self.ui_q.put(lambda: self._add_sources(paths))
            try:
                windnd.hook_dropfiles(self.root, func=cb, force_unicode=True)
            except TypeError:
                windnd.hook_dropfiles(self.root, func=cb)

    def _on_tkdnd_drop(self, event):
        self._add_sources(self.root.tk.splitlist(event.data))
        return getattr(event, "action", None)

    # ----- status / logs -----
    def _set_status(self, text, color="#93c5fd"):
        self.status_lbl.configure(text=text, fg=color)

    def _ui_log(self, msg, level="INFO"):
        logger.info(msg)
        self.log_q.put((level, time.strftime("%H:%M:%S"), msg))

    def _drain_logs(self):
        items = []
        try:
            while len(items) < 1000:
                items.append(self.log_q.get_nowait())
        except queue.Empty:
            pass
        if not items:
            return
        self.log_txt.configure(state="normal")
        for level, ts, msg in items:
            self.log_txt.insert("end", f"[{ts}] {msg}\n", level)
        lines = int(self.log_txt.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_txt.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    # ----- queue -----
    def _refresh_queue(self):
        self.queue_tree.delete(*self.queue_tree.get_children())
        for p in self.sources:
            self.queue_tree.insert("", "end", values=("โฟลเดอร์" if os.path.isdir(p) else "ไฟล์", p))
        self.queue_count.configure(text=f"{len(self.sources):,} รายการ")

    def _add_sources(self, paths):
        if self.engine and not self.engine.finished.is_set():
            return
        self.sources, discarded = normalize_and_prune(self.sources, paths)
        for p, why in discarded:
            self._ui_log(f"[QUEUE] ไม่เพิ่ม {p}: {why}", "WARN")
        self._refresh_queue()

    def _add_files(self):
        files = filedialog.askopenfilenames(title="เลือกไฟล์")
        if files:
            self._add_sources(files)

    def _add_folder(self):
        d = filedialog.askdirectory(title="เลือกโฟลเดอร์", mustexist=True)
        if d:
            self._add_sources([d])

    def _paste_paths(self):
        dlg = PastePathsDialog(self.root, self.font)
        self.root.wait_window(dlg)
        if dlg.result:
            self._add_sources(dlg.result)

    def _remove_selected(self):
        sel = set(self.queue_tree.item(i, "values")[1] for i in self.queue_tree.selection())
        if sel:
            self.sources = [p for p in self.sources if p not in sel]
            self._refresh_queue()

    def _clear_sources(self):
        self.sources = []
        self._refresh_queue()

    # ----- config / sites -----
    def _collect_site(self):
        return {
            "proto": self.proto_var.get(), "endpoint": self.endpoint_var.get().strip(),
            "region": self.region_var.get().strip(), "access_key": self.access_var.get().strip(),
            "secret_key": self.secret_var.get().strip(), "bucket": self.bucket_var.get().strip(),
            "prefix": self.prefix_var.get().strip(), "addressing_style": self.addr_var.get(),
            "verify_ssl": bool(self.verify_var.get()), "chunk_size": self.chunk_var.get(),
            "threads": self.threads_var.get(), "mode": self.mode_var.get(),
            "skip_existing": bool(self.skip_var.get()), "include_folder_name": bool(self.incl_var.get()),
            "remove_empty_dirs": bool(self.rmempty_var.get()), "tcp_buffer": self.tcpbuf_var.get(),
        }

    def _apply_site(self, d):
        self.proto_var.set(d.get("proto", "https"))
        self.endpoint_var.set(d.get("endpoint", ""))
        self.region_var.set(d.get("region", "us-east-1"))
        self.access_var.set(d.get("access_key", ""))
        self.secret_var.set(d.get("secret_key", ""))
        self.bucket_var.set(d.get("bucket", ""))
        self.prefix_var.set(d.get("prefix", ""))
        self.addr_var.set(d.get("addressing_style", "path"))
        self.verify_var.set(bool(d.get("verify_ssl", True)))
        cs = d.get("chunk_size", "64MB")
        self.chunk_var.set(cs if cs in CHUNK_OPTIONS else "64MB")
        self.threads_var.set(str(d.get("threads", 16)))
        self.mode_var.set(d.get("mode", "COPY") if d.get("mode") in ("COPY", "MOVE") else "COPY")
        self.skip_var.set(bool(d.get("skip_existing", False)))
        self.incl_var.set(bool(d.get("include_folder_name", True)))
        self.rmempty_var.set(bool(d.get("remove_empty_dirs", False)))
        tb = d.get("tcp_buffer", "4MB")
        self.tcpbuf_var.set(tb if tb in TCP_BUF_OPTIONS else "4MB")

    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if isinstance(data, dict) and isinstance(data.get("sites"), dict):
                self.config_data = data
        except FileNotFoundError:
            pass
        except Exception as e:
            self._ui_log(f"[CONFIG] อ่าน config.json ไม่ได้: {e}", "WARN")
        self._refresh_sites()
        active = self.config_data.get("active_site")
        sites = self.config_data["sites"]
        if active not in sites and sites:
            active = next(iter(sites))
        if active:
            self.site_var.set(active)
            self._apply_site(sites[active])

    def _save_config(self):
        tmp = CONFIG_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self.config_data, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)
        except Exception as e:
            self._ui_log(f"[CONFIG] บันทึกไม่ได้: {e}", "ERROR")

    def _refresh_sites(self):
        self.site_cb.configure(values=list(self.config_data["sites"].keys()))

    def _on_site_selected(self):
        name = self.site_var.get()
        if name in self.config_data["sites"]:
            self._apply_site(self.config_data["sites"][name])
            self.config_data["active_site"] = name
            self._save_config()

    def _site_new(self):
        name = simpledialog.askstring("ไซต์ใหม่", "ชื่อไซต์:", parent=self.root)
        if not name:
            return
        name = name.strip()
        self.config_data["sites"][name] = self._collect_site()
        self.config_data["active_site"] = name
        self._refresh_sites()
        self.site_var.set(name)
        self._save_config()
        self._ui_log(f"[CONFIG] สร้างไซต์ '{name}'", "OK")

    def _site_save(self):
        name = self.site_var.get()
        if not name:
            return self._site_new()
        self.config_data["sites"][name] = self._collect_site()
        self.config_data["active_site"] = name
        self._save_config()
        self._ui_log(f"[CONFIG] บันทึกไซต์ '{name}'", "OK")

    def _site_delete(self):
        name = self.site_var.get()
        if not name or name not in self.config_data["sites"]:
            return
        if not messagebox.askyesno("ลบไซต์", f"ลบไซต์ '{name}' ?"):
            return
        del self.config_data["sites"][name]
        nxt = next(iter(self.config_data["sites"]), None)
        self.config_data["active_site"] = nxt
        self._refresh_sites()
        self.site_var.set(nxt or "")
        if nxt:
            self._apply_site(self.config_data["sites"][nxt])
        self._save_config()

    # ----- run config -----
    def _build_run_cfg(self):
        ep = self.endpoint_var.get().strip()
        bucket = self.bucket_var.get().strip()
        ak, sk = self.access_var.get().strip(), self.secret_var.get().strip()
        missing = [n for n, v in (("Endpoint", ep), ("Bucket", bucket), ("Access Key", ak), ("Secret Key", sk)) if not v]
        if missing:
            messagebox.showwarning("ข้อมูลไม่ครบ", "กรุณากรอก: " + ", ".join(missing))
            return None
        try:
            threads = max(1, min(128, int(str(self.threads_var.get()).strip())))
        except ValueError:
            threads = 16
        self.threads_var.set(str(threads))
        tb = self.tcpbuf_var.get()
        return {
            "endpoint_url": build_endpoint(self.proto_var.get(), ep),
            "region": self.region_var.get().strip() or "us-east-1",
            "access_key": ak, "secret_key": sk, "bucket": bucket,
            "prefix": self.prefix_var.get(), "addressing": self.addr_var.get() or "path",
            "verify_ssl": bool(self.verify_var.get()),
            "chunk_bytes": parse_size(self.chunk_var.get()), "threads": threads,
            "move": self.mode_var.get() == "MOVE",
            "skip_existing": bool(self.skip_var.get()),
            "include_folder_name": bool(self.incl_var.get()),
            "remove_empty_dirs": bool(self.rmempty_var.get()),
            "tcp_buf": 0 if tb == "OS Auto" else parse_size(tb, 4 * MiB),
            "retries": DEFAULT_RETRIES,
        }

    def _test_connection(self):
        cfg = self._build_run_cfg()
        if not cfg:
            return
        self.test_btn.configure(state="disabled", text="กำลังทดสอบ...")

        def work():
            try:
                t0 = time.monotonic()
                make_client(cfg).head_bucket(Bucket=cfg["bucket"])
                res = (True, f"เชื่อมต่อสำเร็จ ({(time.monotonic() - t0) * 1000:.0f} ms)")
            except Exception as e:
                res = (False, short_err(e))
            self.ui_q.put(lambda: self._show_test_result(*res))
        threading.Thread(target=work, daemon=True).start()

    def _show_test_result(self, ok, msg):
        self.test_btn.configure(state="normal", text="ทดสอบการเชื่อมต่อ")
        self._ui_log(f"[TEST] {msg}", "OK" if ok else "ERROR")
        (messagebox.showinfo if ok else messagebox.showerror)("ทดสอบการเชื่อมต่อ", msg)

    # ----- run control -----
    def _set_running(self, running):
        for w, normal in self._lockables:
            try:
                w.configure(state="disabled" if running else normal)
            except tk.TclError:
                pass
        self.start_btn.configure(state="disabled" if running else "normal")
        self.pause_btn.configure(state="normal" if running else "disabled", text="หยุดชั่วคราว")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.test_btn.configure(state="disabled" if running else "normal")

    def _start(self):
        if self.engine and not self.engine.finished.is_set():
            return
        if not self.sources:
            messagebox.showwarning("คิวว่าง", "กรุณาเพิ่มไฟล์หรือโฟลเดอร์ก่อน")
            return
        cfg = self._build_run_cfg()
        if not cfg:
            return
        if cfg["move"] and not messagebox.askyesno(
                "ยืนยันโหมด MOVE",
                "โหมด MOVE จะลบไฟล์ต้นทางหลังจากอัปโหลดและตรวจสอบ ETag สำเร็จแล้ว\n\nต้องการดำเนินการต่อหรือไม่?",
                icon="warning"):
            return
        name = self.site_var.get()
        if name:
            self.config_data["sites"][name] = self._collect_site()
            self.config_data["active_site"] = name
            self._save_config()

        self.xfer_tree.delete(*self.xfer_tree.get_children())
        self.rows.clear()
        self.finished_rows.clear()
        self.pbar["value"] = 0
        self._speed = 0.0
        self._last_bytes = 0
        self._last_t = time.monotonic()
        self._finish_handled = False

        self.engine = UploadEngine(cfg, list(self.sources), self.log_q)
        self.engine.start()
        self._set_running(True)
        self._set_status("กำลังอัปโหลด...", "#93c5fd")

    def _toggle_pause(self):
        if not self.engine or self.engine.finished.is_set():
            return
        if self.engine.paused:
            self.engine.resume()
            self.pause_btn.configure(text="หยุดชั่วคราว")
            self._set_status("กำลังอัปโหลด...", "#93c5fd")
            self._ui_log("[CONTROL] ทำงานต่อ")
        else:
            self.engine.pause()
            self.pause_btn.configure(text="ทำต่อ")
            self._set_status("หยุดชั่วคราว (part ที่กำลังส่งจะส่งจนเสร็จ)", "#fbbf24")
            self._ui_log("[CONTROL] หยุดชั่วคราว", "WARN")

    def _stop(self):
        if not self.engine or self.engine.finished.is_set():
            return
        self.engine.stop()
        self.stop_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")
        self._set_status("กำลังหยุดและ abort multipart...", "#fbbf24")
        self._ui_log("[CONTROL] ได้รับคำสั่งหยุด", "WARN")

    # ----- periodic UI refresh -----
    def _tick(self):
        try:
            while True:
                try:
                    fn = self.ui_q.get_nowait()
                except queue.Empty:
                    break
                fn()
            self._drain_logs()
            if self.engine is not None:
                self._refresh_from_engine()
        except Exception:
            logger.exception("UI tick error")
        finally:
            try:
                self.root.after(UI_TICK_MS, self._tick)
            except tk.TclError:
                pass

    def _refresh_from_engine(self):
        eng = self.engine
        finished = eng.finished.is_set()
        rows, s = eng.pull_changes()
        for row in rows:
            self._upsert_row(row)
        self._trim_rows()

        total, done = s["total_bytes"], max(0, s["done_bytes"])
        pct = (done / total) if total > 0 else (1.0 if finished else 0.0)
        self.pbar["value"] = min(1000, pct * 1000)
        self.lbl_pct.configure(text=f"{pct * 100:.1f}%")
        self.lbl_bytes.configure(text=f"{human(done)} / {human(total)}")

        now = time.monotonic()
        dt = now - self._last_t
        if dt >= 0.5:
            inst = max(0.0, (done - self._last_bytes) / dt)
            self._speed = inst if self._speed == 0 else 0.3 * inst + 0.7 * self._speed
            self._last_bytes, self._last_t = done, now
        if s["phase"] == "scan":
            self.lbl_speed.configure(text="กำลังสแกนไฟล์...")
            self.lbl_eta.configure(text="ETA: --:--:--")
        else:
            self.lbl_speed.configure(text=f"ความเร็ว: {human(self._speed)}/s ({self._speed * 8 / 1e6:,.0f} Mbps)")
            eta = (total - done) / self._speed if self._speed > 0 else None
            self.lbl_eta.configure(text=f"ETA: {fmt_eta(eta)}")
        processed = s["ok"] + s["failed"] + s["skipped"] + s["cancelled"]
        self.lbl_files.configure(text=f"ไฟล์: {processed:,}/{s['files_total']:,}  "
                                      f"(สำเร็จ {s['ok']:,} | ล้มเหลว {s['failed']:,} | ข้าม {s['skipped']:,})")

        if finished and not self._finish_handled:
            self._finish_handled = True
            self._drain_logs()
            self._on_engine_finished(s)

    def _upsert_row(self, row):
        fid, name, size, done, pd, pt, status, kind = row
        pct = (done / size * 100) if size > 0 else (100.0 if kind == "ok" else 0.0)
        vals = (name, human(size), f"{pct:.1f}%", f"{pd}/{pt}" if pt else "-", status)
        iid = self.rows.get(fid)
        if iid is not None and self.xfer_tree.exists(iid):
            self.xfer_tree.item(iid, values=vals, tags=(kind,))
        else:
            self.rows[fid] = self.xfer_tree.insert("", 0, values=vals, tags=(kind,))
        if kind in ("ok", "fail", "skip", "cancel"):
            self.finished_rows.append(fid)

    def _trim_rows(self):
        while len(self.rows) > MAX_TRANSFER_ROWS and self.finished_rows:
            fid = self.finished_rows.popleft()
            iid = self.rows.pop(fid, None)
            if iid and self.xfer_tree.exists(iid):
                self.xfer_tree.delete(iid)

    def _on_engine_finished(self, s):
        self._set_running(False)
        stopped = self.engine.stop_event.is_set()
        if s["failed"]:
            self._set_status(f"เสร็จ (มีไฟล์ล้มเหลว {s['failed']:,})", "#f87171")
        elif stopped:
            self._set_status("หยุดแล้ว", "#fbbf24")
        else:
            self._set_status("อัปโหลดเสร็จสมบูรณ์", "#4ade80")
        if self._closing:
            self.root.destroy()
            return
        if s["failed"]:
            messagebox.showwarning("เสร็จสิ้น", f"มีไฟล์ล้มเหลว {s['failed']:,} ไฟล์\nดูรายละเอียดในแท็บ Log")

    def _on_close(self):
        if self.engine and not self.engine.finished.is_set():
            if not messagebox.askyesno("กำลังอัปโหลด", "ต้องการหยุดและปิดโปรแกรมหรือไม่?\n(multipart ที่ค้างจะถูก abort ก่อนปิด)"):
                return
            self._closing = True
            self.engine.stop()
            self._set_status("กำลังหยุดก่อนปิดโปรแกรม...", "#fbbf24")
            return
        name = self.site_var.get()
        if name and name in self.config_data["sites"]:
            self.config_data["sites"][name] = self._collect_site()
            self._save_config()
        self.root.destroy()


def main():
    root = TkinterDnD.Tk() if DND_MODE == "tkdnd" else tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()