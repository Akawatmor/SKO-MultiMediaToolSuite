# -*- coding: utf-8 -*-
"""
Apple TX3G Core Engine for Subler-compatible SRT parsing and MP4 embedding.
Directly implements Apple QuickTime / 3GPP TS 26.245 'tx3g' subtitle specifications
matching Subler (MP42Foundation) behavior.
"""

import os
import re
import struct
import shutil
import subprocess
from typing import List, Tuple, Dict, Optional, Any

# ==============================================================================
# 1. CONSTANTS & MAPPINGS
# ==============================================================================

# Style bit flags (matches MP42HtmlParser.h / 3GPP TS 26.245)
STYLE_BOLD       = 1  # 0x01
STYLE_ITALIC     = 2  # 0x02
STYLE_UNDERLINED = 4  # 0x04

# Display flags for stsd.tx3g (matches MP42SubtitleTrack.m)
DISPLAY_FLAG_VERTICAL_PLACEMENT   = 0x20000000
DISPLAY_FLAG_SOME_SAMPLES_FORCED  = 0x40000000
DISPLAY_FLAG_ALL_SAMPLES_FORCED   = 0x80000000

# Color mapping for standard HTML names
NAMED_COLORS: Dict[str, Tuple[int, int, int, int]] = {
    "yellow":  (255, 255, 0, 255),
    "red":     (255, 0, 0, 255),
    "green":   (0, 255, 0, 255),
    "blue":    (0, 0, 255, 255),
    "cyan":    (0, 255, 255, 255),
    "magenta": (255, 0, 255, 255),
    "white":   (255, 255, 255, 255),
    "black":   (0, 0, 0, 255),
    "orange":  (255, 165, 0, 255),
    "gray":    (128, 128, 128, 255),
    "grey":    (128, 128, 128, 255),
}

# ASS Alignment map
# 1=bottom-left, 2=bottom-center, 3=bottom-right
# 4=mid-left,    5=mid-center,    6=mid-right
# 7=top-left,    8=top-center,    9=top-right
AN_CONFIG = {
    1: {"zone": "bot", "h_just": "Left",   "v_just": "Bottom"},
    2: {"zone": "bot", "h_just": "Center", "v_just": "Bottom"},
    3: {"zone": "bot", "h_just": "Right",  "v_just": "Bottom"},
    4: {"zone": "mid", "h_just": "Left",   "v_just": "Center"},
    5: {"zone": "mid", "h_just": "Center", "v_just": "Center"},
    6: {"zone": "mid", "h_just": "Right",  "v_just": "Center"},
    7: {"zone": "top", "h_just": "Left",   "v_just": "Top"},
    8: {"zone": "top", "h_just": "Center", "v_just": "Top"},
    9: {"zone": "top", "h_just": "Right",  "v_just": "Top"},
}

DEFAULT_TEXT_COLOR = (255, 255, 255, 255)

# ==============================================================================
# 2. DATA STRUCTURES
# ==============================================================================

class StyleSpan:
    """Represents styling across a character range in the UTF-8 text."""
    def __init__(self, start: int, end: int, flags: int, color: Tuple[int, int, int, int]):
        self.start = start          # Character start index (0-indexed)
        self.end = end              # Character end index
        self.flags = flags          # Bitmask: 1=Bold, 2=Italic, 4=Underline
        self.color = color          # (r, g, b, a)

    def __repr__(self):
        return f"<StyleSpan [{self.start}:{self.end}] flags={self.flags} color={self.color}>"

class SubtitleCue:
    """Represents a single parsed subtitle line with timing and styling."""
    def __init__(self,
                 start_ms: int,
                 end_ms: int,
                 text: str,
                 forced: bool = False,
                 is_top: bool = False,
                 an: int = 2,
                 style_spans: Optional[List[StyleSpan]] = None):
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.text = text
        self.forced = forced
        self.is_top = is_top
        self.an = an
        self.style_spans = style_spans or []

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)

# ==============================================================================
# 3. SRT & HTML PARSER (Subler Compatible)
# ==============================================================================

def parse_time_to_ms(time_str: str) -> int:
    """Converts 00:00:02,600 or 00:00:02.600 to milliseconds."""
    time_str = time_str.strip().replace(',', '.')
    parts = time_str.split(':')
    if len(parts) == 3:
        h = int(parts[0])
        m = int(parts[1])
        s_parts = parts[2].split('.')
        s = int(s_parts[0])
        ms = int(s_parts[1].ljust(3, '0')[:3]) if len(s_parts) > 1 else 0
        return (h * 3600 + m * 60 + s) * 1000 + ms
    return 0

def ms_to_time_str(ms: int, sep: str = ',') -> str:
    """Converts milliseconds to 00:00:00,000 format."""
    total_sec, msec = divmod(ms, 1000)
    total_min, sec = divmod(total_sec, 60)
    hour, minute = divmod(total_min, 60)
    return f"{hour:02d}:{minute:02d}:{sec:02d}{sep}{msec:03d}"

def parse_color_hex_or_name(color_str: str) -> Optional[Tuple[int, int, int, int]]:
    """Parses hex (#RRGGBB or #RGB) or named color to (r, g, b, a)."""
    color_str = color_str.strip().lower()
    if color_str.startswith('#'):
        hex_val = color_str[1:]
        if len(hex_val) == 6:
            r = int(hex_val[0:2], 16)
            g = int(hex_val[2:4], 16)
            b = int(hex_val[4:6], 16)
            return (r, g, b, 255)
        elif len(hex_val) == 8:
            r = int(hex_val[0:2], 16)
            g = int(hex_val[2:4], 16)
            b = int(hex_val[4:6], 16)
            a = int(hex_val[6:8], 16)
            return (r, g, b, a)
        elif len(hex_val) == 3:
            r = int(hex_val[0] * 2, 16)
            g = int(hex_val[1] * 2, 16)
            b = int(hex_val[2] * 2, 16)
            return (r, g, b, 255)
    elif color_str in NAMED_COLORS:
        return NAMED_COLORS[color_str]
    return None

def parse_text_and_styles(raw_text: str) -> Tuple[str, List[StyleSpan]]:
    """
    Parses HTML tags (<b>, <i>, <u>, <font color="...">) and ASS tags
    ({\\c&HBBGGRR&}, {\\anX}) matching Subler's MP42HtmlParser.
    Returns (clean_text, list_of_style_spans).
    """
    # 1. Extract and remove ASS override tags first
    # e.g. {\c&H00FFFF&} -> ASS BGR color
    ass_color: Optional[Tuple[int, int, int, int]] = None
    m_ass = re.search(r'\{\\1?c&H([0-9a-fA-F]{6})&\}', raw_text)
    if m_ass:
        bgr = m_ass.group(1)
        bb, gg, rr = int(bgr[0:2], 16), int(bgr[2:4], 16), int(bgr[4:6], 16)
        ass_color = (rr, gg, bb, 255)

    clean_prep = re.sub(r'\{.*?\}', '', raw_text)
    # Replace <br> or <br/> with newline
    clean_prep = re.sub(r'<br\s*/?>', '\n', clean_prep, flags=re.IGNORECASE)

    # 2. Tokenize HTML tags: <b>, </b>, <i>, </i>, <u>, </u>, <font...>, </font>
    tag_regex = re.compile(r'(</?[a-zA-Z]+(?:\s+[^>]*)?>)')
    tokens = tag_regex.split(clean_prep)

    clean_chars = []
    char_styles: List[Dict[str, Any]] = []

    active_bold = False
    active_italic = False
    active_underline = False
    active_colors: List[Tuple[int, int, int, int]] = []
    if ass_color:
        active_colors.append(ass_color)

    for token in tokens:
        if not token:
            continue
        if token.startswith('<') and token.endswith('>'):
            inner = token[1:-1].strip()
            lower_inner = inner.lower()
            if lower_inner == 'b':
                active_bold = True
            elif lower_inner == '/b':
                active_bold = False
            elif lower_inner == 'i':
                active_italic = True
            elif lower_inner == '/i':
                active_italic = False
            elif lower_inner == 'u':
                active_underline = True
            elif lower_inner == '/u':
                active_underline = False
            elif lower_inner.startswith('font'):
                # Extract color attribute
                m_col = re.search(r'color=["\']?([^"\'\s>]+)["\']?', inner, re.IGNORECASE)
                if m_col:
                    parsed_col = parse_color_hex_or_name(m_col.group(1))
                    if parsed_col:
                        active_colors.append(parsed_col)
            elif lower_inner == '/font':
                if active_colors:
                    active_colors.pop()
        else:
            # Regular text
            cur_color = active_colors[-1] if active_colors else DEFAULT_TEXT_COLOR
            cur_flags = (STYLE_BOLD if active_bold else 0) | \
                        (STYLE_ITALIC if active_italic else 0) | \
                        (STYLE_UNDERLINED if active_underline else 0)

            for ch in token:
                clean_chars.append(ch)
                char_styles.append({
                    'flags': cur_flags,
                    'color': cur_color
                })

    clean_text = "".join(clean_chars)
    n = len(clean_text)
    if n == 0:
        return "", []

    # 3. Consolidate consecutive character styles into StyleSpans
    spans: List[StyleSpan] = []
    run_start = 0

    for idx in range(1, n + 1):
        if idx == n or (char_styles[idx]['flags'] != char_styles[run_start]['flags'] or
                        char_styles[idx]['color'] != char_styles[run_start]['color']):
            prop = char_styles[run_start]
            flags = prop['flags']
            color = prop['color']
            # Only add span if there is actual styling or non-default color
            if flags != 0 or color != DEFAULT_TEXT_COLOR:
                spans.append(StyleSpan(run_start, idx, flags, color))
            run_start = idx

    return clean_text, spans

def parse_srt_file(content: str) -> List[SubtitleCue]:
    """
    Parses SRT content with full Subler feature support:
    - Timestamps: 00:00:02,600 --> 00:00:06,600
    - Subler Top alignment: 'X1:0'
    - Subler Forced subtitle: '!!!'
    - ASS alignment: {\\an1} - {\\an9}
    - HTML styling: <b>, <i>, <u>, <font color="...">
    """
    # Normalize newlines and strip BOM
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    if content.startswith('\ufeff'):
        content = content[1:]

    # Match SRT blocks:
    # 1. Index line (optional digits)
    # 2. Timestamp line (with possible trailing flags like X1:0 and !!!)
    # 3. Text lines up to blank line
    cue_pattern = re.compile(
        r'(?:^|\n)\s*(?:\d+\s*\n)?'
        r'(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{3})'
        r'([^\n]*)\n'
        r'([\s\S]*?)(?=\n\s*\n|\Z)'
    )

    cues: List[SubtitleCue] = []

    for match in cue_pattern.finditer(content.strip()):
        start_str, end_str, timestamp_trailing, raw_text = match.groups()
        start_ms = parse_time_to_ms(start_str)
        end_ms = parse_time_to_ms(end_str)

        if end_ms <= start_ms:
            continue

        # Check Subler top position: X1:0
        is_top = False
        top_match = re.search(r'X1:(\d+)', timestamp_trailing)
        if top_match:
            pos_val = int(top_match.group(1))
            if pos_val == 0:
                is_top = True

        # Check Subler forced subtitle: !!!
        is_forced = "!!!" in timestamp_trailing or "!!!" in raw_text

        # Check ASS alignment: {\an1} - {\an9}
        an_val = 2
        an_match = re.search(r'\{\\an([1-9])\}', raw_text)
        if an_match:
            an_val = int(an_match.group(1))
            if an_val in (7, 8, 9):
                is_top = True
        else:
            old_a = re.search(r'\{\\a([1-9]|10|11)\}', raw_text)
            if old_a:
                old_map = {1: 1, 2: 2, 3: 3, 5: 7, 6: 8, 7: 9, 9: 4, 10: 5, 11: 6}
                an_val = old_map.get(int(old_a.group(1)), 2)
                if an_val in (7, 8, 9):
                    is_top = True

        # Remove '!!!' from text if it was inside text
        cleaned_raw = raw_text.replace('!!!', '')

        clean_text, style_spans = parse_text_and_styles(cleaned_raw.strip())
        if clean_text:
            cues.append(SubtitleCue(
                start_ms=start_ms,
                end_ms=end_ms,
                text=clean_text,
                forced=is_forced,
                is_top=is_top,
                an=an_val,
                style_spans=style_spans
            ))

    cues.sort(key=lambda c: c.start_ms)
    return cues

# ==============================================================================
# 4. SUB SERIALIZER (Gaps & Overlaps Handling)
# ==============================================================================

class SerializedSample:
    """A discrete sample payload in the timeline."""
    def __init__(self,
                 start_ms: int,
                 duration_ms: int,
                 text: str,
                 forced: bool = False,
                 is_top: bool = False,
                 style_spans: Optional[List[StyleSpan]] = None):
        self.start_ms = start_ms
        self.duration_ms = duration_ms
        self.text = text
        self.forced = forced
        self.is_top = is_top
        self.style_spans = style_spans or []

    @property
    def is_empty(self) -> bool:
        return len(self.text.strip()) == 0

def serialize_cues(cues: List[SubtitleCue], total_duration_ms: Optional[int] = None) -> List[SerializedSample]:
    """
    Ensures continuous timeline with empty subtitle samples for gaps,
    strictly adhering to Apple 3GPP TX3G streaming requirements (MP42SubSerializer).
    """
    samples: List[SerializedSample] = []
    current_time = 0

    for cue in cues:
        # Gap before this cue -> insert empty sample
        if cue.start_ms > current_time:
            gap_dur = cue.start_ms - current_time
            samples.append(SerializedSample(
                start_ms=current_time,
                duration_ms=gap_dur,
                text=""
            ))
            current_time = cue.start_ms

        # Active cue sample
        samples.append(SerializedSample(
            start_ms=cue.start_ms,
            duration_ms=cue.duration_ms,
            text=cue.text,
            forced=cue.forced,
            is_top=cue.is_top,
            style_spans=cue.style_spans
        ))
        current_time = max(current_time, cue.end_ms)

    # Optional trailing gap to match video duration
    if total_duration_ms and total_duration_ms > current_time:
        samples.append(SerializedSample(
            start_ms=current_time,
            duration_ms=total_duration_ms - current_time,
            text=""
        ))

    return samples

# ==============================================================================
# 5. TX3G BINARY SAMPLE BUILDER
# ==============================================================================

def build_tx3g_sample(sample: SerializedSample,
                      video_width: int,
                      video_height: int,
                      has_vertical_placement: bool) -> bytes:
    """
    Constructs the exact binary TX3G sample payload:
    [uint16 text_length] + [UTF-8 text] + ['styl' atom] + ['frcd' atom] + ['tbox' atom]
    Matches Subler's copySubtitleSample() and copyEmptySubtitleSample().
    """
    # Empty sample
    if sample.is_empty:
        payload = bytearray(b'\x00\x00')
        if sample.forced:
            # 8-byte frcd atom
            payload += struct.pack('>I4s', 8, b'frcd')
        return bytes(payload)

    utf8_bytes = sample.text.encode('utf-8')
    text_len = len(utf8_bytes)

    font_size = int(video_height * 0.05) if has_vertical_placement else int(video_height / 0.15 * 0.05)
    font_size = max(12, min(255, font_size))

    # Base text payload: 2-byte length + UTF-8 bytes
    payload = bytearray(struct.pack('>H', text_len))
    payload += utf8_bytes

    # 1. 'styl' Atom (if there are style spans)
    if sample.style_spans:
        style_count = len(sample.style_spans)
        atom_size = 10 + (style_count * 12)
        styl_header = struct.pack('>I4sH', atom_size, b'styl', style_count)
        payload += styl_header

        for span in sample.style_spans:
            r, g, b, a = span.color
            record = struct.pack(
                '>HHHBBBBBB',
                span.start,     # startChar (2 bytes)
                span.end,       # endChar (2 bytes)
                1,              # fontID (2 bytes)
                span.flags,     # flags (1 byte: 1=bold, 2=italic, 4=underline)
                font_size,      # fontSize (1 byte)
                r, g, b, a      # RGBA color (4 bytes)
            )
            payload += record

    # 2. 'frcd' Atom (Forced Subtitle)
    if sample.forced:
        payload += struct.pack('>I4s', 8, b'frcd')

    # 3. 'tbox' Atom (Text Box / Top Alignment)
    if has_vertical_placement:
        if sample.is_top:
            # Top 12% of screen: top=0, bottom=0.12*h (matching Subler)
            top = 0
            left = 0
            bottom = int(video_height * 0.12)
            right = video_width
        else:
            # Bottom 12% of screen: top=0.88*h, bottom=h (matching Subler)
            top = int(video_height * 0.88)
            left = 0
            bottom = video_height
            right = video_width

        tbox_payload = struct.pack(
            '>I4sHHHH',
            16, b'tbox',
            top, left, bottom, right
        )
        payload += tbox_payload

    return bytes(payload)

# ==============================================================================
# 6. PURE-PYTHON MP4 CONTAINER PARSER & NATIVE TX3G EMBEDDER
# ==============================================================================

def pack_iso639_2_lang(lang_code: str) -> int:
    """Packs 3-letter ISO-639-2 language code (e.g. 'tha', 'und', 'eng') into 16 bits."""
    lang = (lang_code.lower() + "und")[:3]
    c1 = max(0, ord(lang[0]) - 0x60)
    c2 = max(0, ord(lang[1]) - 0x60)
    c3 = max(0, ord(lang[2]) - 0x60)
    return ((c1 & 0x1F) << 10) | ((c2 & 0x1F) << 5) | (c3 & 0x1F)

class MP4BoxInfo:
    def __init__(self, box_type: bytes, start_offset: int, size: int, header_size: int):
        self.box_type = box_type
        self.start_offset = start_offset
        self.size = size
        self.header_size = header_size

    @property
    def end_offset(self) -> int:
        return self.start_offset + self.size

    @property
    def data_offset(self) -> int:
        return self.start_offset + self.header_size

    @property
    def data_size(self) -> int:
        return self.size - self.header_size

def read_boxes(data: bytes, start: int, end: int) -> List[MP4BoxInfo]:
    """Scans MP4 boxes in a given byte slice."""
    boxes = []
    pos = start
    while pos + 8 <= end:
        size = struct.unpack('>I', data[pos:pos+4])[0]
        btype = data[pos+4:pos+8]
        header_size = 8
        if size == 1:
            # 64-bit large size
            if pos + 16 > end:
                break
            size = struct.unpack('>Q', data[pos+8:pos+16])[0]
            header_size = 16
        elif size == 0:
            # Extends to end of file
            size = end - pos

        if size < header_size or pos + size > end:
            break

        boxes.append(MP4BoxInfo(btype, pos, size, header_size))
        pos += size
    return boxes

def find_box(data: bytes, start: int, end: int, target_type: bytes) -> Optional[MP4BoxInfo]:
    for b in read_boxes(data, start, end):
        if b.box_type == target_type:
            return b
    return None

def extract_video_info(mp4_bytes: bytes) -> Tuple[int, int, int, int]:
    """
    Extracts (width, height, duration_ms, timescale) from the video track in MP4.
    Defaults to (1920, 1080, 0, 1000) if not found.
    """
    file_size = len(mp4_bytes)
    moov = find_box(mp4_bytes, 0, file_size, b'moov')
    if not moov:
        return 1920, 1080, 0, 1000

    mvhd = find_box(mp4_bytes, moov.data_offset, moov.end_offset, b'mvhd')
    mv_timescale = 1000
    mv_duration = 0
    if mvhd:
        v = mp4_bytes[mvhd.data_offset]
        if v == 0:
            mv_timescale, raw_dur = struct.unpack('>II', mp4_bytes[mvhd.data_offset+12:mvhd.data_offset+20])
        else:
            mv_timescale, raw_dur = struct.unpack('>IQ', mp4_bytes[mvhd.data_offset+20:mvhd.data_offset+32])
        if mv_timescale > 0:
            mv_duration = int((raw_dur / mv_timescale) * 1000)

    # Search each trak for handler 'vide'
    for trak in read_boxes(mp4_bytes, moov.data_offset, moov.end_offset):
        if trak.box_type != b'trak':
            continue
        mdia = find_box(mp4_bytes, trak.data_offset, trak.end_offset, b'mdia')
        if not mdia:
            continue
        hdlr = find_box(mp4_bytes, mdia.data_offset, mdia.end_offset, b'hdlr')
        if not hdlr:
            continue
        # Handler type is at offset 8..12 inside hdlr
        handler_type = mp4_bytes[hdlr.data_offset+8:hdlr.data_offset+12]
        if handler_type == b'vide':
            # Extract width and height from tkhd
            tkhd = find_box(mp4_bytes, trak.data_offset, trak.end_offset, b'tkhd')
            if tkhd:
                # Width and height are 16.16 fixed point at the last 8 bytes of tkhd
                w_fixed, h_fixed = struct.unpack('>II', mp4_bytes[tkhd.end_offset-8:tkhd.end_offset])
                width = w_fixed >> 16
                height = h_fixed >> 16
                if width > 0 and height > 0:
                    return width, height, mv_duration, mv_timescale

    return 1920, 1080, mv_duration, mv_timescale

def build_tx3g_trak(track_id: int,
                    samples: List[SerializedSample],
                    sample_offsets: List[int],
                    sample_sizes: List[int],
                    video_width: int,
                    video_height: int,
                    has_vertical_placement: bool,
                    has_forced: bool,
                    lang_code: str = "tha",
                    movie_timescale: int = 1000) -> bytes:
    """
    Constructs the complete 'trak' atom for Apple TX3G subtitle stream.
    Fully implements:
    - tkhd (layer -1, alternate group 2, matrix positioning)
    - mdia/mdhd (timescale 1000, ISO language)
    - mdia/hdlr (type 'sbtl', name 'SubtitleHandler')
    - minf/stbl/stsd/tx3g (displayFlags, fontTable, dimensions)
    - stts, stsc, stsz, stco (sample tables)
    """
    total_duration_ms = sum(s.duration_ms for s in samples)
    movie_duration = int((total_duration_ms / 1000.0) * movie_timescale)

    # 1. Dimensions and Matrix Translation (Subler specification)
    if has_vertical_placement:
        sub_width = video_width
        sub_height = video_height
        trans_y = 0
    else:
        sub_width = video_width
        sub_height = max(1, int(video_height * 0.15))
        trans_y = int(video_height * 0.85)

    # Matrix: [0x00010000, 0, 0, 0, 0x00010000, 0, trans_x, trans_y, 0x40000000]
    matrix = struct.pack(
        '>9I',
        0x00010000, 0, 0,
        0, 0x00010000, 0,
        0, trans_y << 16, 0x40000000
    )

    # --- tkhd (Track Header) ---
    # version=0, flags=0x0F (track_enabled | track_in_movie | track_in_preview)
    tkhd_data = struct.pack(
        '>I II II I II h h h H',
        0x0000000F,             # version (0) + flags (0x0F)
        0, 0,                   # creation_time, modification_time
        track_id, 0,            # track_id, reserved
        movie_duration,         # duration
        0, 0,                   # reserved (8 bytes)
        -1,                     # layer = -1 (draw in front of video)
        2,                      # alternate_group = 2 (subtitles)
        0,                      # volume (0 for subtitles)
        0                       # reserved (2 bytes)
    )
    tkhd_data += matrix
    tkhd_data += struct.pack('>II', sub_width << 16, sub_height << 16)
    tkhd_box = struct.pack('>I4s', len(tkhd_data) + 8, b'tkhd') + tkhd_data

    # --- mdia / mdhd (Media Header) ---
    lang_packed = pack_iso639_2_lang(lang_code)
    mdhd_data = struct.pack(
        '>I II I I H H',
        0,                      # version (0) + flags (0)
        0, 0,                   # creation_time, modification_time
        1000,                   # timescale = 1000 ms
        total_duration_ms,      # duration in timescale units
        lang_packed,            # language code
        0                       # quality
    )
    mdhd_box = struct.pack('>I4s', len(mdhd_data) + 8, b'mdhd') + mdhd_data

    # --- mdia / hdlr (Handler Reference) ---
    # 'sbtl' is Apple Subtitle handler
    hdlr_name = b'SubtitleHandler\x00'
    hdlr_data = struct.pack(
        '>I 4s 4s 3I',
        0,                      # version + flags
        b'\x00\x00\x00\x00',    # pre-defined
        b'sbtl',                # handler_type = 'sbtl'
        0, 0, 0                 # reserved
    ) + hdlr_name
    hdlr_box = struct.pack('>I4s', len(hdlr_data) + 8, b'hdlr') + hdlr_data

    # --- minf / gmhd / gmin (Apple QuickTime Subtitle Media Header) ---
    # According to Apple QuickTime File Format Specification for 'sbtl' media
    gmin_data = struct.pack(
        '>I H 3H h H',
        0,                      # version (0) + flags (0)
        0x0040,                 # graphicsMode: 0x0040 (ditherCopy)
        0, 0, 0,                # opcolor (red, green, blue)
        0,                      # balance
        0                       # reserved
    )
    gmin_box = struct.pack('>I4s', len(gmin_data) + 8, b'gmin') + gmin_data
    gmhd_box = struct.pack('>I4s', len(gmin_box) + 8, b'gmhd') + gmin_box

    # --- minf / dinf / dref ---
    # Self-contained reference (flag 1)
    url_box = struct.pack('>I4s I', 12, b'url ', 1)
    dref_data = struct.pack('>I I', 0, 1) + url_box
    dref_box = struct.pack('>I4s', len(dref_data) + 8, b'dref') + dref_data
    dinf_box = struct.pack('>I4s', len(dref_box) + 8, b'dinf') + dref_box

    # --- minf / stbl / stsd / tx3g ---
    # Display flags: 0x20000000 for vertical placement, 0x40000000 for forced
    display_flags = 0
    if has_vertical_placement:
        display_flags |= DISPLAY_FLAG_VERTICAL_PLACEMENT
    if has_forced:
        display_flags |= DISPLAY_FLAG_SOME_SAMPLES_FORCED

    # Font table: FontID 1 -> "Helvetica"
    font_name = b'Helvetica'
    font_entry = struct.pack('>HB', 1, len(font_name)) + font_name
    ftab_data = struct.pack('>H', 1) + font_entry
    ftab_box = struct.pack('>I4s', len(ftab_data) + 8, b'ftab') + ftab_data

    def_font_size = max(12, int(video_height * 0.05))

    # TextSampleEntry 'tx3g'
    tx3g_payload = struct.pack(
        '>6s H I b b 4B HHHH HH H B B 4B',
        b'\x00' * 6,            # reserved
        1,                      # data_reference_index
        display_flags,          # displayFlags (4 bytes)
        1,                      # horizontalJustification: 1 = Center
        -1,                     # verticalJustification: -1 = Bottom
        0, 0, 0, 0,             # bgColor (RGBA = transparent)
        0, 0, sub_height, sub_width, # defaultTextBox (top, left, bottom, right)
        0, 0,                   # startChar, endChar
        1,                      # fontID = 1
        0,                      # fontFaceFlags = 0
        def_font_size,          # fontSize
        255, 255, 255, 255      # fontColor (white)
    ) + ftab_box
    tx3g_box = struct.pack('>I4s', len(tx3g_payload) + 8, b'tx3g') + tx3g_payload

    stsd_data = struct.pack('>I I', 0, 1) + tx3g_box
    stsd_box = struct.pack('>I4s', len(stsd_data) + 8, b'stsd') + stsd_data

    # --- stts (Time-to-Sample) ---
    # Compress adjacent samples with identical durations
    stts_entries = []
    for s in samples:
        dur = max(1, s.duration_ms)
        if stts_entries and stts_entries[-1][1] == dur:
            stts_entries[-1] = (stts_entries[-1][0] + 1, dur)
        else:
            stts_entries.append((1, dur))

    stts_data = struct.pack('>I I', 0, len(stts_entries))
    for count, dur in stts_entries:
        stts_data += struct.pack('>II', count, dur)
    stts_box = struct.pack('>I4s', len(stts_data) + 8, b'stts') + stts_data

    # --- stsc (Sample-to-Chunk: 1 sample per chunk) ---
    stsc_data = struct.pack(
        '>I I III',
        0, 1,                   # ver_flags=0, entry_count=1
        1, 1, 1                 # first_chunk=1, samples_per_chunk=1, sample_desc_index=1
    )
    stsc_box = struct.pack('>I4s', len(stsc_data) + 8, b'stsc') + stsc_data

    # --- stsz (Sample Sizes) ---
    stsz_data = struct.pack('>I II', 0, 0, len(sample_sizes))
    for sz in sample_sizes:
        stsz_data += struct.pack('>I', sz)
    stsz_box = struct.pack('>I4s', len(stsz_data) + 8, b'stsz') + stsz_data

    # --- stco / co64 (Chunk Offsets) ---
    use_co64 = any(off > 0xFFFFFFFF for off in sample_offsets)
    if use_co64:
        co64_data = struct.pack('>I I', 0, len(sample_offsets))
        for off in sample_offsets:
            co64_data += struct.pack('>Q', off)
        stco_box = struct.pack('>I4s', len(co64_data) + 8, b'co64') + co64_data
    else:
        stco_data = struct.pack('>I I', 0, len(sample_offsets))
        for off in sample_offsets:
            stco_data += struct.pack('>I', off)
        stco_box = struct.pack('>I4s', len(stco_data) + 8, b'stco') + stco_data

    # Assemble stbl
    stbl_content = stsd_box + stts_box + stsc_box + stsz_box + stco_box
    stbl_box = struct.pack('>I4s', len(stbl_content) + 8, b'stbl') + stbl_content

    # Assemble minf
    minf_content = gmhd_box + dinf_box + stbl_box
    minf_box = struct.pack('>I4s', len(minf_content) + 8, b'minf') + minf_content

    # Assemble mdia
    mdia_content = mdhd_box + hdlr_box + minf_box
    mdia_box = struct.pack('>I4s', len(mdia_content) + 8, b'mdia') + mdia_content

    # Assemble trak
    trak_content = tkhd_box + mdia_box
    return struct.pack('>I4s', len(trak_content) + 8, b'trak') + trak_content

def embed_tx3g_subtitles_native(video_path: str,
                                srt_path: str,
                                output_path: str,
                                lang_code: str = "tha",
                                progress_cb=None) -> bool:
    """
    Directly embeds Apple TX3G subtitle track into MP4/M4V file in Pure Python.
    Zero external dependencies required (no MP4Box or FFmpeg needed).
    """
    if progress_cb:
        progress_cb(10, "กำลังอ่านไฟล์วิดีโอและซับไตเติล...")

    with open(video_path, 'rb') as f:
        src_bytes = f.read()

    with open(srt_path, 'r', encoding='utf-8-sig', errors='ignore') as f:
        srt_content = f.read()

    if progress_cb:
        progress_cb(25, "กำลังแยกวิเคราะห์ข้อมูล SRT และแท็กสไตล์ตามมาตรฐาน Subler...")

    cues = parse_srt_file(srt_content)
    if not cues:
        raise ValueError("ไม่พบข้อมูลซับไตเติลที่ถูกต้องในไฟล์ SRT")

    video_w, video_h, vid_dur_ms, movie_ts = extract_video_info(src_bytes)

    has_vertical = any(c.is_top for c in cues)
    has_forced = any(c.forced for c in cues)

    if progress_cb:
        progress_cb(40, "กำลังจัดคิวช่วงเวลาต่อเนื่องและแทรก Empty Samples...")

    samples = serialize_cues(cues, total_duration_ms=vid_dur_ms)

    if progress_cb:
        progress_cb(55, "กำลังเข้ารหัส Binary TX3G (styl, frcd, tbox atoms)...")

    # Build binary payloads for each sample
    sample_binaries = []
    for s in samples:
        bin_data = build_tx3g_sample(s, video_w, video_h, has_vertical)
        sample_binaries.append(bin_data)

    if progress_cb:
        progress_cb(70, "กำลังจัดเรียง MP4 Container และสร้างแทร็ก Subtitle...")

    # Determine highest track_id and next_track_id from mvhd
    file_len = len(src_bytes)
    moov_box = find_box(src_bytes, 0, file_len, b'moov')
    if not moov_box:
        raise ValueError("ไฟล์วิดีโอไม่ใช่ MP4/M4V ที่ถูกต้อง (ไม่พบกล่อง 'moov')")

    mvhd_box = find_box(src_bytes, moov_box.data_offset, moov_box.end_offset, b'mvhd')
    if not mvhd_box:
        raise ValueError("ไม่พบกล่อง 'mvhd' ภายใน 'moov'")

    # Read existing next_track_id from mvhd
    mvhd_ver = src_bytes[mvhd_box.data_offset]
    if mvhd_ver == 0:
        next_track_id_offset = mvhd_box.data_offset + 96
    else:
        next_track_id_offset = mvhd_box.data_offset + 108

    next_track_id = struct.unpack('>I', src_bytes[next_track_id_offset:next_track_id_offset+4])[0]
    new_sub_track_id = max(next_track_id, 2)

    # Compute layout:
    # We will write:
    # 1. Existing file without old moov (or keep original and put updated moov at the end)
    # Most robust MP4 structure:
    # [Original ftyp & mdat without old moov] + [New Subtitle Samples in mdat] + [New moov with sub trak at end]
    # To keep all existing chunk offsets 100% valid:
    # If old moov was at the beginning: replace old moov in-place with a 'free' box of identical size!
    # That way, EVERY existing chunk offset in audio/video remains exactly the same!

    # Create a working copy of src_bytes
    out_buf = bytearray(src_bytes)

    # Replace old moov with 'free' box of exactly the same size
    # 'free' box header is [size, 'free'] followed by zeros
    old_moov_size = moov_box.size
    free_header = struct.pack('>I4s', old_moov_size, b'free')
    out_buf[moov_box.start_offset : moov_box.start_offset + 8] = free_header
    # Fill remaining bytes of old moov with 0
    zero_len = old_moov_size - 8
    if zero_len > 0:
        out_buf[moov_box.start_offset + 8 : moov_box.end_offset] = b'\x00' * zero_len

    # Now append new subtitle samples as a second 'mdat' at the end of the file!
    sub_mdat_start = len(out_buf)
    total_sub_data_len = sum(len(b) for b in sample_binaries)
    sub_mdat_size = 8 + total_sub_data_len
    sub_mdat_header = struct.pack('>I4s', sub_mdat_size, b'mdat')
    out_buf += sub_mdat_header

    sample_offsets = []
    sample_sizes = []
    curr_sub_off = sub_mdat_start + 8

    for b in sample_binaries:
        sample_offsets.append(curr_sub_off)
        sample_sizes.append(len(b))
        out_buf += b
        curr_sub_off += len(b)

    if progress_cb:
        progress_cb(85, "กำลังสร้าง 'moov.trak' และอัปเดต 'mvhd'...")

    # Build the new subtitle trak
    new_trak_bytes = build_tx3g_trak(
        track_id=new_sub_track_id,
        samples=samples,
        sample_offsets=sample_offsets,
        sample_sizes=sample_sizes,
        video_width=video_w,
        video_height=video_h,
        has_vertical_placement=has_vertical,
        has_forced=has_forced,
        lang_code=lang_code,
        movie_timescale=movie_ts
    )

    # Rebuild moov:
    # Extract all original boxes inside old moov
    old_moov_content = src_bytes[moov_box.data_offset : moov_box.end_offset]
    # Update next_track_id in mvhd
    mvhd_in_content = find_box(old_moov_content, 0, len(old_moov_content), b'mvhd')
    if mvhd_in_content:
        new_mvhd = bytearray(old_moov_content[mvhd_in_content.start_offset : mvhd_in_content.end_offset])
        ver = new_mvhd[8]  # relative to box header
        id_off = 8 + (96 if ver == 0 else 108)
        new_mvhd[id_off:id_off+4] = struct.pack('>I', new_sub_track_id + 1)
        # Replace mvhd in old_moov_content
        old_moov_content = old_moov_content[:mvhd_in_content.start_offset] + bytes(new_mvhd) + old_moov_content[mvhd_in_content.end_offset:]

    # Append new_trak_bytes to moov content
    new_moov_content = old_moov_content + new_trak_bytes
    new_moov_box = struct.pack('>I4s', len(new_moov_content) + 8, b'moov') + new_moov_content

    # Append new moov at the very end
    out_buf += new_moov_box

    if progress_cb:
        progress_cb(95, "กำลังบันทึกไฟล์วิดีโอผลลัพธ์...")

    with open(output_path, 'wb') as f:
        f.write(out_buf)

    if progress_cb:
        progress_cb(100, "ฝังซับไตเติล TX3G สำเร็จเรียบร้อย!")

    return True

# ==============================================================================
# 7. GPAC TTXT GENERATOR & MP4BOX MUXER (Alternative Backend)
# ==============================================================================

def generate_subler_ttxt(cues: List[SubtitleCue],
                         output_file: str,
                         width: int = 1920,
                         height: int = 1080) -> None:
    """Generates an enhanced Subler-compatible GPAC TTXT XML file."""
    import html
    font_size = max(18, int(height * 0.045))
    margin_x = int(width * 0.05)
    box_left = margin_x
    box_right = width - margin_x

    zones = {
        "top": (int(height * 0.04), int(height * 0.26)),
        "mid": (int(height * 0.38), int(height * 0.62)),
        "bot": (int(height * 0.74), int(height * 0.96)),
    }

    lines = [
        '<?xml version="1.0" encoding="UTF-8" ?>',
        '<TextStream version="1.1" xml:space="preserve">',
        f'  <TextStreamHeader width="{width}" height="{height}" layer="-1" translation_x="0" translation_y="0">',
        '    <TextSampleDescription horizontalJustification="Center" verticalJustification="Bottom" backColor="00 00 00 00" verticalText="no" fillTextRegion="no" continuousKaraoke="no" scroll="None">',
        '      <FontTable>',
        '        <FontTableEntry fontName="Helvetica" fontID="1"/>',
        '      </FontTable>',
        f'      <TextBox top="{zones["top"][0]}" left="{box_left}" bottom="{zones["bot"][1]}" right="{box_right}"/>',
        f'      <Style fontID="1" fontSize="{font_size}" color="ff ff ff ff"/>',
        '    </TextSampleDescription>',
        '  </TextStreamHeader>\n',
    ]

    for i, cue in enumerate(cues):
        start_str = ms_to_time_str(cue.start_ms, sep='.')
        end_str = ms_to_time_str(cue.end_ms, sep='.')

        # Calculate bounding box
        if cue.is_top:
            top, bottom = zones["top"]
        else:
            top, bottom = zones["bot"]

        box_tag = f'<TextBox top="{top}" left="{box_left}" bottom="{bottom}" right="{box_right}"/>'

        # Style tags
        style_tags = []
        for span in cue.style_spans:
            attr = [f'fromChar="{span.start}"', f'toChar="{span.end}"']
            if span.color != DEFAULT_TEXT_COLOR:
                r, g, b, a = span.color
                attr.append(f'color="{r:02x} {g:02x} {b:02x} {a:02x}"')
            tokens = []
            if span.flags & STYLE_BOLD:
                tokens.append("Bold")
            if span.flags & STYLE_ITALIC:
                tokens.append("Italic")
            if span.flags & STYLE_UNDERLINED:
                tokens.append("Underlined")
            if tokens:
                attr.append(f'styles="{" ".join(tokens)}"')
            style_tags.append(f'<Style {" ".join(attr)}/>')

        escaped_text = html.escape(cue.text, quote=False)
        sample_body = f"{box_tag}{''.join(style_tags)}{escaped_text}"

        lines.append(f'  <TextSample sampleTime="{start_str}" sampleDescriptionIndex="1">{sample_body}</TextSample>')

        # Insert empty sample at end time if there is a gap
        is_last = (i == len(cues) - 1)
        if is_last or cue.end_ms < cues[i + 1].start_ms:
            lines.append(f'  <TextSample sampleTime="{end_str}" sampleDescriptionIndex="1"></TextSample>')

    lines.append('</TextStream>')

    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

def embed_tx3g_subtitles_mp4box(mp4box_bin: str,
                                video_path: str,
                                srt_path: str,
                                output_path: str,
                                lang_code: str = "tha",
                                progress_cb=None) -> bool:
    """Embeds subtitles using MP4Box via Subler-optimized TTXT."""
    with open(srt_path, 'r', encoding='utf-8-sig', errors='ignore') as f:
        srt_content = f.read()

    cues = parse_srt_file(srt_content)
    if not cues:
        raise ValueError("ไม่พบข้อมูลซับไตเติลในไฟล์ SRT")

    with open(video_path, 'rb') as f:
        v_bytes = f.read()
    w, h, _, _ = extract_video_info(v_bytes)

    base_dir = os.path.dirname(output_path) or "."
    temp_ttxt = os.path.join(base_dir, "_temp_subler.ttxt")

    try:
        if progress_cb:
            progress_cb(30, "กำลังสร้าง GPAC TTXT ตามมาตรฐาน Subler...")

        generate_subler_ttxt(cues, temp_ttxt, width=w, height=h)

        if progress_cb:
            progress_cb(60, "กำลังเรียกใช้งาน MP4Box เพื่อมักซ์ซับไตเติล...")

        cmd = [
            mp4box_bin,
            "-add", video_path,
            "-add", f"{temp_ttxt}:lang={lang_code}:layout={w}x{h}:group=2",
            "-new", output_path
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
        if proc.returncode != 0:
            raise RuntimeError(f"MP4Box Error:\n{proc.stderr or proc.stdout}")

        if progress_cb:
            progress_cb(100, "มักซ์ไฟล์ด้วย MP4Box สำเร็จ!")
        return True

    finally:
        if os.path.exists(temp_ttxt):
            try:
                os.remove(temp_ttxt)
            except Exception:
                pass
