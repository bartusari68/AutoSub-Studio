from __future__ import annotations

import json
import os
import queue
import re
import threading
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import ctranslate2
from faster_whisper import WhisperModel

APP_DIR = Path(__file__).resolve().parent

LANGUAGES = {
    "Auto detect": {"whisper": None, "nllb": None},
    "Turkish": {"whisper": "tr", "nllb": "tur_Latn"},
    "English": {"whisper": "en", "nllb": "eng_Latn"},
    "German": {"whisper": "de", "nllb": "deu_Latn"},
    "French": {"whisper": "fr", "nllb": "fra_Latn"},
    "Spanish": {"whisper": "es", "nllb": "spa_Latn"},
    "Italian": {"whisper": "it", "nllb": "ita_Latn"},
    "Dutch": {"whisper": "nl", "nllb": "nld_Latn"},
    "Portuguese": {"whisper": "pt", "nllb": "por_Latn"},
    "Polish": {"whisper": "pl", "nllb": "pol_Latn"},
    "Russian": {"whisper": "ru", "nllb": "rus_Cyrl"},
    "Ukrainian": {"whisper": "uk", "nllb": "ukr_Cyrl"},
    "Arabic": {"whisper": "ar", "nllb": "arb_Arab"},
    "Chinese": {"whisper": "zh", "nllb": "zho_Hans"},
    "Japanese": {"whisper": "ja", "nllb": "jpn_Jpan"},
    "Korean": {"whisper": "ko", "nllb": "kor_Hang"},
}
WHISPER_TO_NLLB = {v["whisper"]: v["nllb"] for v in LANGUAGES.values() if v["whisper"] and v["nllb"]}

CONFIG = {
    "asr_model": "large-v3",
    "device": "auto",
    "compute_type_cuda": "float16",
    "compute_type_cpu": "int8",
    "beam_size": 5,
    "vad_filter": False,
    "caption": {
        "max_words": 11,
        "max_source_chars": 68,
        "min_duration": 1.0,
        "max_duration": 4.8,
        "pause_break": 0.62,
        "max_chars_per_line": 42,
        "max_lines": 2,
        "target_cps": 17.0,
        "max_cps": 20.0,
    },
    "translation": {
        "model": "facebook/nllb-200-distilled-600M",
        "batch_size": 8,
        "num_beams": 4,
    },
    "ass_style": {
        "name": "ClassicBox",
        "play_res_x": 512,
        "play_res_y": 288,
        "font_name": "Arial",
        "font_size": 15.0,
        "bold": False,
        "primary_color": "&H00FFFFFF",
        "outline_color": "&H66000000",
        "back_color": "&H66000000",
        "border_style": 3,
        "outline": 0.5,
        "shadow": 0.0,
        "alignment": 2,
        "margin_l": 10,
        "margin_r": 10,
        "margin_v": 5,
    },
}


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class Caption:
    start: float
    end: float
    text: str


def clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    text = re.sub(r"\s+([,.;:!?%])", r"\1", text)
    return text


def join_words(words: Sequence[Word]) -> str:
    out = ""
    for w in words:
        token = (w.text or "").strip()
        if not token:
            continue
        if not out:
            out = token
        elif re.match(r"^[,.;:!?%’')\]]", token):
            out += token
        elif token.startswith("'"):
            out += token
        else:
            out += " " + token
    return clean_text(out)


def ends_sentence(text: str) -> bool:
    return bool(re.search(r'[.!?]["”’\']?$', text.strip()))


def soft_break(text: str) -> bool:
    return bool(re.search(r'[,;:]["”’\']?$', text.strip()))


def build_captions(words: Sequence[Word], cfg: dict) -> List[Caption]:
    if not words:
        return []
    max_words = int(cfg["max_words"])
    max_chars = int(cfg["max_source_chars"])
    min_duration = float(cfg["min_duration"])
    max_duration = float(cfg["max_duration"])
    pause_break = float(cfg["pause_break"])

    captions: List[Caption] = []
    bucket: List[Word] = []

    def flush():
        nonlocal bucket
        if not bucket:
            return
        txt = join_words(bucket)
        if txt:
            captions.append(Caption(bucket[0].start, bucket[-1].end, txt))
        bucket = []

    for w in words:
        if not bucket:
            bucket = [w]
            continue
        current = join_words(bucket)
        candidate = join_words(bucket + [w])
        cur_dur = bucket[-1].end - bucket[0].start
        new_dur = w.end - bucket[0].start
        gap = max(0.0, w.start - bucket[-1].end)

        do_break = (
            (gap >= pause_break and cur_dur >= 0.65)
            or (ends_sentence(current) and cur_dur >= min_duration)
            or len(bucket) >= max_words
            or len(candidate) > max_chars
            or new_dur > max_duration
            or (soft_break(current) and len(current) >= int(max_chars * 0.72) and cur_dur >= min_duration)
        )
        if do_break:
            flush()
            bucket = [w]
        else:
            bucket.append(w)
    flush()

    # Merge very short neighboring captions where safe.
    merged: List[Caption] = []
    i = 0
    while i < len(captions):
        cur = captions[i]
        dur = cur.end - cur.start
        if dur < min_duration and i + 1 < len(captions):
            nxt = captions[i + 1]
            gap = nxt.start - cur.end
            combined = clean_text(cur.text + " " + nxt.text)
            if gap <= 0.35 and (nxt.end - cur.start) <= max_duration and len(combined) <= max_chars + 12:
                merged.append(Caption(cur.start, nxt.end, combined))
                i += 2
                continue
        merged.append(cur)
        i += 1

    for i in range(len(merged) - 1):
        if merged[i].end >= merged[i + 1].start:
            merged[i].end = max(merged[i].start + 0.25, merged[i + 1].start - 0.03)
    return merged


def smart_wrap(text: str, width: int = 42) -> str:
    text = clean_text(text)
    if len(text) <= width:
        return text
    words = text.split()
    candidates = []
    for i in range(1, len(words)):
        a = " ".join(words[:i])
        b = " ".join(words[i:])
        overflow = max(0, len(a) - width) + max(0, len(b) - width)
        score = overflow * 1000 + abs(len(a) - len(b))
        candidates.append((score, a, b))
    if not candidates:
        return text
    _, a, b = min(candidates, key=lambda x: x[0])
    return a + "\n" + b


def srt_time(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def ass_time(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def write_srt(captions: Sequence[Caption], path: Path):
    width = int(CONFIG["caption"]["max_chars_per_line"])
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        for i, c in enumerate(captions, 1):
            f.write(f"{i}\n{srt_time(c.start)} --> {srt_time(c.end)}\n{smart_wrap(c.text, width)}\n\n")


def ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def write_ass(captions: Sequence[Caption], path: Path):
    s = CONFIG["ass_style"]
    bold = -1 if s["bold"] else 0
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {s['play_res_x']}
PlayResY: {s['play_res_y']}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: {s['name']},{s['font_name']},{s['font_size']},{s['primary_color']},&H000000FF,{s['outline_color']},{s['back_color']},{bold},0,0,0,100,100,0,0,{s['border_style']},{s['outline']},{s['shadow']},{s['alignment']},{s['margin_l']},{s['margin_r']},{s['margin_v']},1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    width = int(CONFIG["caption"]["max_chars_per_line"])
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(header)
        for c in captions:
            txt = ass_escape(smart_wrap(c.text, width))
            f.write(f"Dialogue: 0,{ass_time(c.start)},{ass_time(c.end)},{s['name']},,0,0,0,,{txt}\n")


def choose_device(requested: str) -> Tuple[str, str]:
    if requested == "cpu":
        return "cpu", CONFIG["compute_type_cpu"]
    if requested == "cuda":
        return "cuda", CONFIG["compute_type_cuda"]
    try:
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", CONFIG["compute_type_cuda"]
    except Exception:
        pass
    return "cpu", CONFIG["compute_type_cpu"]


def format_media_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _safe_report(report, percent: float, stage: str):
    if report is None:
        return
    try:
        report(max(0.0, min(100.0, float(percent))), stage)
    except Exception:
        pass


def transcribe(
    video: str,
    source_code: Optional[str],
    model_size: str,
    device_request: str,
    vad_filter: bool,
    log,
    report,
):
    device, compute = choose_device(device_request)
    log(f"ASR modeli yükleniyor: {model_size} / {device} / {compute}")
    _safe_report(report, 2.0, f"Whisper modeli yükleniyor • {model_size} / {device}")

    model = WhisperModel(model_size, device=device, compute_type=compute)

    kwargs = dict(
        beam_size=int(CONFIG["beam_size"]),
        word_timestamps=True,
        vad_filter=bool(vad_filter),
        condition_on_previous_text=True,
        temperature=0.0,
    )
    if source_code:
        kwargs["language"] = source_code

    log("Video taranıyor; kelime zamanları çıkarılıyor...")
    _safe_report(report, 5.0, "Konuşma taranıyor")

    segments, info = model.transcribe(video, **kwargs)

    duration = float(getattr(info, "duration", 0.0) or 0.0)
    if duration <= 0:
        try:
            duration = float(ffprobe_info(video).get("format", {}).get("duration") or 0.0)
        except Exception:
            duration = 0.0

    words: List[Word] = []
    last_reported_sec = -1

    for seg in segments:
        for w in (getattr(seg, "words", None) or []):
            if w.start is None or w.end is None:
                continue
            token = (w.word or "").strip()
            if token:
                words.append(Word(float(w.start), float(w.end), token))

        sec = int(float(seg.end))
        if sec != last_reported_sec:
            if duration > 0:
                ratio = min(1.0, max(0.0, float(seg.end) / duration))
                pct = 5.0 + (45.0 * ratio)
                stage = (
                    f"Konuşma taranıyor • "
                    f"{format_media_time(seg.end)} / {format_media_time(duration)}"
                )
            else:
                pct = 5.0
                stage = f"Konuşma taranıyor • {format_media_time(seg.end)} işlendi"

            _safe_report(report, pct, stage)
            log(f"Tarama: {seg.end:.3f} sn")
            last_reported_sec = sec

    _safe_report(
        report,
        50.0,
        f"Konuşma taraması tamamlandı • {format_media_time(duration)}"
        if duration > 0
        else "Konuşma taraması tamamlandı",
    )

    return words, info.language, getattr(info, "language_probability", None), device

def translate_texts(
    texts: Sequence[str],
    src_lang: str,
    tgt_lang: str,
    log,
    report,
) -> List[str]:
    if src_lang == tgt_lang:
        _safe_report(report, 75.0, "Çeviri gerekmiyor • kaynak ve hedef dil aynı")
        return list(texts)

    try:
        import torch
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
    except ImportError as e:
        raise RuntimeError("Çeviri modülü kurulu değil. INSTALL_TRANSLATION.bat çalıştır.") from e

    model_name = CONFIG["translation"]["model"]
    log(f"Çeviri modeli yükleniyor: {model_name}")
    _safe_report(report, 54.0, "Çeviri modeli yükleniyor")

    tokenizer = AutoTokenizer.from_pretrained(model_name, src_lang=src_lang)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_name)
    model.eval()

    _safe_report(report, 56.0, "Çeviri modeli hazır")

    bos = tokenizer.convert_tokens_to_ids(tgt_lang)
    batch_size = int(CONFIG["translation"]["batch_size"])
    num_beams = int(CONFIG["translation"]["num_beams"])
    out: List[str] = []
    total = len(texts)

    if total == 0:
        _safe_report(report, 75.0, "Çeviri tamamlandı • 0/0 caption")
        return out

    for off in range(0, total, batch_size):
        batch = list(texts[off:off + batch_size])
        enc = tokenizer(
            batch,
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=256,
        )
        with torch.inference_mode():
            gen = model.generate(
                **enc,
                forced_bos_token_id=bos,
                num_beams=num_beams,
                max_new_tokens=220,
            )

        out.extend(
            x.strip()
            for x in tokenizer.batch_decode(gen, skip_special_tokens=True)
        )

        done = min(off + len(batch), total)
        ratio = done / total
        pct = 56.0 + (19.0 * ratio)
        _safe_report(report, pct, f"Çeviri • {done}/{total} caption")
        log(f"Çeviri: {done}/{total}")

    _safe_report(report, 75.0, f"Çeviri tamamlandı • {total}/{total} caption")
    return out

def run_pipeline(
    video_path: str,
    source_name: str,
    target_name: str,
    model_size: str,
    device_request: str,
    vad_filter: bool,
    output_dir: Optional[str],
    log,
    report,
):
    video = Path(video_path).expanduser().resolve()
    if not video.exists():
        raise FileNotFoundError(video)

    _safe_report(report, 0.5, "Video hazırlanıyor")

    source_info = LANGUAGES[source_name]
    target_info = LANGUAGES[target_name]

    words, detected, probability, actual_device = transcribe(
        str(video),
        source_info["whisper"],
        model_size,
        device_request,
        vad_filter,
        log,
        report,
    )

    if not words:
        raise RuntimeError("Kelime zamanları alınamadı. VAD'i kapatıp tekrar dene.")

    source_whisper = source_info["whisper"] or detected
    source_nllb = source_info["nllb"] or WHISPER_TO_NLLB.get(source_whisper)
    target_nllb = target_info["nllb"]

    if not source_nllb:
        raise RuntimeError(
            f"Algılanan kaynak dil ({source_whisper}) henüz dil haritasında yok."
        )

    _safe_report(report, 52.0, "Caption'lar oluşturuluyor")
    captions = build_captions(words, CONFIG["caption"])
    log(f"Caption sayısı: {len(captions)}")

    translated = translate_texts(
        [c.text for c in captions],
        source_nllb,
        target_nllb,
        log,
        report,
    )
    target_caps = [
        Caption(c.start, c.end, t)
        for c, t in zip(captions, translated)
    ]

    out_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else video.parent / "output"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = video.stem
    src_suffix = source_whisper or "src"
    tgt_suffix = target_info["whisper"] or target_name.lower()

    source_srt = out_dir / f"{stem}.{src_suffix}.source.srt"
    target_srt = out_dir / f"{stem}.{tgt_suffix}.srt"
    target_ass = out_dir / f"{stem}.{tgt_suffix}.ass"
    words_json = out_dir / f"{stem}.words.json"
    timeline_json = out_dir / f"{stem}.timeline.json"

    _safe_report(report, 77.0, "SRT ve ASS dosyaları yazılıyor")

    write_srt(captions, source_srt)
    write_srt(target_caps, target_srt)
    write_ass(target_caps, target_ass)

    words_json.write_text(
        json.dumps(
            {
                "detected_language": detected,
                "language_probability": probability,
                "words": [asdict(w) for w in words],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    timeline_json.write_text(
        json.dumps(
            {
                "source_language": source_whisper,
                "target_language": target_name,
                "captions": [
                    {
                        "start_ms": int(round(src.start * 1000)),
                        "end_ms": int(round(src.end * 1000)),
                        "source": src.text,
                        "translation": dst.text,
                    }
                    for src, dst in zip(captions, target_caps)
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log("Altyazı dosyaları hazır.")
    _safe_report(report, 80.0, "SRT + ASS hazır")

    return {
        "device": actual_device,
        "detected_language": detected,
        "caption_count": len(target_caps),
        "source_srt": str(source_srt),
        "target_srt": str(target_srt),
        "target_ass": str(target_ass),
        "words_json": str(words_json),
        "timeline_json": str(timeline_json),
        "output_dir": str(out_dir),
    }

def find_ffmpeg_tools():
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "FFmpeg/ffprobe bulunamadı. INSTALL_CORE.bat dosyasını tekrar çalıştır "
            "ve gerekirse Windows terminalini yeniden aç."
        )
    return ffmpeg, ffprobe


def ffprobe_info(video_path: str) -> dict:
    _, ffprobe = find_ffmpeg_tools()
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries",
        "format=duration,size,bit_rate:stream=index,codec_type,codec_name,bit_rate,pix_fmt,width,height,avg_frame_rate",
        "-of", "json",
        video_path,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError("ffprobe başarısız:\n" + p.stderr[-3000:])
    return json.loads(p.stdout)


def _audio_bitrate_from_probe(info: dict) -> int:
    total = 0
    audio_streams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    for s in audio_streams:
        try:
            br = int(s.get("bit_rate") or 0)
        except Exception:
            br = 0
        # Missing stream bitrate is common; use a conservative AAC-like estimate.
        total += br if br > 0 else 192_000
    return total


def _source_codec_from_probe(info: dict) -> str:
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return (s.get("codec_name") or "").lower()
    return ""


def _encoder_for_source(info: dict) -> str:
    # Final MP4 standard is always H.264/AVC for maximum compatibility.
    return "libx264"


def _safe_ass_for_filter(ass_path: str, output_dir: Path) -> Path:
    safe = output_dir / "__autosub_style.ass"
    shutil.copy2(ass_path, safe)
    return safe


def _parse_ffmpeg_clock(value: str) -> float:
    try:
        h, m, s = value.strip().split(":")
        return (float(h) * 3600.0) + (float(m) * 60.0) + float(s)
    except Exception:
        return 0.0


def _run_ffmpeg_with_progress(
    cmd,
    duration: float,
    report,
    start_percent: float,
    end_percent: float,
    stage_name: str,
    cwd: Optional[str] = None,
):
    """
    Runs ffmpeg with -progress pipe:1 and maps media out_time to the GUI progress range.
    """
    progress_cmd = list(cmd)
    insert_at = max(1, len(progress_cmd) - 1)
    progress_cmd[insert_at:insert_at] = [
        "-progress", "pipe:1",
        "-nostats",
    ]

    proc = subprocess.Popen(
        progress_cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    tail = []
    last_second = -1

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if line:
            tail.append(line)
            if len(tail) > 120:
                tail = tail[-120:]

        if line.startswith("out_time="):
            current = _parse_ffmpeg_clock(line.split("=", 1)[1])
            sec = int(current)

            if sec != last_second:
                if duration > 0:
                    ratio = min(1.0, max(0.0, current / duration))
                    pct = start_percent + ((end_percent - start_percent) * ratio)
                    stage = (
                        f"{stage_name} • "
                        f"{format_media_time(current)} / {format_media_time(duration)}"
                    )
                else:
                    pct = start_percent
                    stage = f"{stage_name} • {format_media_time(current)} işlendi"

                _safe_report(report, pct, stage)
                last_second = sec

    rc = proc.wait()
    if rc != 0:
        error_tail = "\n".join(tail[-80:])
        raise RuntimeError(
            f"FFmpeg işlemi başarısız (kod {rc}).\n{error_tail}"
        )

    end_stage = (
        f"{stage_name} • {format_media_time(duration)} / {format_media_time(duration)}"
        if duration > 0
        else f"{stage_name} tamamlandı"
    )
    _safe_report(report, end_percent, end_stage)


def make_styled_mkv_no_reencode(
    video_path: str,
    ass_path: str,
    output_dir: str,
    log,
    report,
    progress_start: float = 80.0,
    progress_end: float = 100.0,
) -> str:
    ffmpeg, _ = find_ffmpeg_tools()
    video = Path(video_path).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{video.stem}.STYLED_LOSSLESS.mkv"

    info = ffprobe_info(str(video))
    duration = float(info.get("format", {}).get("duration") or 0.0)

    log("MKV hazırlanıyor: video ve ses COPY, ASS stil korunuyor...")
    _safe_report(report, progress_start, "Kayıpsız MKV hazırlanıyor")

    cmd = [
        ffmpeg, "-y",
        "-i", str(video),
        "-i", str(Path(ass_path).resolve()),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "1:0",
        "-map_metadata", "0",
        "-c:v", "copy",
        "-c:a", "copy",
        "-c:s", "ass",
        "-disposition:s:0", "default",
        str(out),
    ]

    _run_ffmpeg_with_progress(
        cmd,
        duration,
        report,
        progress_start,
        progress_end,
        "Kayıpsız MKV",
    )

    log(f"MKV hazır: {out.name}")
    return str(out)


def make_mp4_soft_no_reencode(
    video_path: str,
    srt_path: str,
    output_dir: str,
    log,
    report,
    progress_start: float = 80.0,
    progress_end: float = 100.0,
) -> str:
    ffmpeg, _ = find_ffmpeg_tools()
    video = Path(video_path).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{video.stem}.SOFTSUB.mp4"

    info = ffprobe_info(str(video))
    duration = float(info.get("format", {}).get("duration") or 0.0)

    log("MP4 soft subtitle hazırlanıyor: video/ses COPY, kalite kaybı 0...")
    _safe_report(report, progress_start, "MP4 soft subtitle hazırlanıyor")

    cmd = [
        ffmpeg, "-y",
        "-i", str(video),
        "-i", str(Path(srt_path).resolve()),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "1:0",
        "-map_metadata", "0",
        "-c:v", "copy",
        "-c:a", "copy",
        "-c:s", "mov_text",
        "-disposition:s:0", "default",
        "-movflags", "+faststart",
        str(out),
    ]

    _run_ffmpeg_with_progress(
        cmd,
        duration,
        report,
        progress_start,
        progress_end,
        "MP4 soft subtitle",
    )

    log(f"MP4 soft subtitle hazır: {out.name}")
    return str(out)


def make_mp4_burn_crf(
    video_path: str,
    ass_path: str,
    output_dir: str,
    log,
    report,
    crf: int = 14,
    progress_start: float = 80.0,
    progress_end: float = 100.0,
) -> str:
    ffmpeg, _ = find_ffmpeg_tools()
    video = Path(video_path).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    info = ffprobe_info(str(video))
    duration = float(info.get("format", {}).get("duration") or 0.0)
    encoder = _encoder_for_source(info)

    out = out_dir / f"{video.stem}.SUBBED_HQ.mp4"
    safe_ass = _safe_ass_for_filter(ass_path, out_dir)

    try:
        log(
            f"MP4 burn-in başlıyor: H.264/libx264, CRF {crf}, "
            "preset slow, audio COPY..."
        )
        _safe_report(report, progress_start, f"H.264 CRF {crf} encode başlıyor")

        cmd = [
            ffmpeg, "-y",
            "-i", str(video),
            "-vf", f"ass='{safe_ass.name}'",
            "-map", "0:v:0",
            "-map", "0:a?",
            "-map_metadata", "0",
            "-c:v", encoder,
            "-preset", "slow",
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(out),
        ]

        _run_ffmpeg_with_progress(
            cmd,
            duration,
            report,
            progress_start,
            progress_end,
            f"H.264 CRF {crf} encode",
            cwd=str(out_dir),
        )
    finally:
        try:
            safe_ass.unlink()
        except Exception:
            pass

    log(f"Yüksek kaliteli MP4 hazır: {out.name}")
    return str(out)


def make_mp4_burn_same_size(
    video_path: str,
    ass_path: str,
    output_dir: str,
    log,
    report,
    progress_start: float = 80.0,
    progress_end: float = 100.0,
) -> str:
    """
    Burn-in requires re-encoding. This mode targets approximately the source file size
    by calculating a target video bitrate from source bytes/duration and preserving audio.
    Two-pass encoding improves size accuracy.
    """
    ffmpeg, _ = find_ffmpeg_tools()
    video = Path(video_path).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    info = ffprobe_info(str(video))
    encoder = _encoder_for_source(info)

    fmt = info.get("format", {})
    try:
        duration = float(fmt.get("duration") or 0)
    except Exception:
        duration = 0.0

    try:
        source_size = int(fmt.get("size") or video.stat().st_size)
    except Exception:
        source_size = video.stat().st_size

    if duration <= 0:
        raise RuntimeError(
            "Kaynak video süresi okunamadı; kaynak boyutuna yakın MP4 üretilemedi."
        )

    audio_bps = _audio_bitrate_from_probe(info)
    desired_total_bps = (source_size * 8.0 / duration) * 0.995
    target_video_bps = int(desired_total_bps - audio_bps)
    target_video_bps = max(target_video_bps, 500_000)

    out = out_dir / f"{video.stem}.SUBBED_SAMESIZE.mp4"
    safe_ass = _safe_ass_for_filter(ass_path, out_dir)
    passlog = out_dir / "__autosub_2pass"
    null_sink = "NUL" if os.name == "nt" else "/dev/null"

    split = progress_start + ((progress_end - progress_start) / 2.0)

    try:
        log(
            f"Kaynak boyutuna yakın H.264 MP4: 2-pass {encoder}; "
            f"hedef video bitrate ≈ {target_video_bps/1_000_000:.2f} Mbps"
        )

        common = [
            "-vf", f"ass='{safe_ass.name}'",
            "-map", "0:v:0",
            "-c:v", encoder,
            "-preset", "slow",
            "-b:v", str(target_video_bps),
            "-pix_fmt", "yuv420p",
            "-passlogfile", str(passlog),
        ]

        log("2-pass encode: 1/2 analiz...")
        cmd1 = [
            ffmpeg, "-y",
            "-i", str(video),
            *common,
            "-pass", "1",
            "-an",
            "-f", "mp4",
            null_sink,
        ]

        _run_ffmpeg_with_progress(
            cmd1,
            duration,
            report,
            progress_start,
            split,
            "H.264 2-pass • Pass 1/2",
            cwd=str(out_dir),
        )

        log("2-pass encode: 2/2 final MP4...")
        cmd2 = [
            ffmpeg, "-y",
            "-i", str(video),
            *common,
            "-pass", "2",
            "-map", "0:a?",
            "-map_metadata", "0",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(out),
        ]

        _run_ffmpeg_with_progress(
            cmd2,
            duration,
            report,
            split,
            progress_end,
            "H.264 2-pass • Pass 2/2",
            cwd=str(out_dir),
        )

    finally:
        try:
            safe_ass.unlink()
        except Exception:
            pass

        for p in out_dir.glob("__autosub_2pass*"):
            try:
                p.unlink()
            except Exception:
                pass

    final_mb = out.stat().st_size / (1024 * 1024)
    source_mb = source_size / (1024 * 1024)
    log(
        f"Final MP4 hazır: {final_mb:.1f} MB "
        f"(kaynak: {source_mb:.1f} MB)"
    )
    return str(out)

class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("AutoSub Studio")
        self.geometry("900x820")
        self.minsize(840, 720)

        self.video_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.source_var = tk.StringVar(value="Turkish")
        self.target_var = tk.StringVar(value="English")
        self.model_var = tk.StringVar(value="large-v3")
        self.device_var = tk.StringVar(value="auto")
        self.vad_var = tk.BooleanVar(value=False)
        self.final_mode_var = tk.StringVar(
            value="MP4 sabit stil - kaynak boyutuna yakın (2-pass)"
        )
        self.archive_mkv_var = tk.BooleanVar(value=True)

        self.q = queue.Queue()

        self.progress_value = tk.DoubleVar(value=0.0)
        self.progress_text = tk.StringVar(value="%0.0  •  Kalan %100.0")
        self.progress_stage = tk.StringVar(value="Bekliyor")

        self._build()
        self.after(100, self._pump)

    def _build(self):
        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)

        ttk.Label(
            f,
            text="AutoSub Studio",
            font=("Segoe UI", 19, "bold"),
        ).grid(
            row=0,
            column=0,
            columnspan=3,
            sticky="w",
        )

        ttk.Label(
            f,
            text="Video → ms hassasiyetli kelime zamanları → caption → çeviri → SRT + ASS",
        ).grid(
            row=1,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(2, 18),
        )

        ttk.Label(f, text="Video").grid(row=2, column=0, sticky="w")

        ttk.Entry(
            f,
            textvariable=self.video_var,
        ).grid(
            row=3,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=(0, 8),
        )

        ttk.Button(
            f,
            text="Video seç",
            command=self._video,
        ).grid(row=3, column=2)

        ttk.Label(
            f,
            text="Çıktı klasörü (boşsa videonun yanına /output)",
        ).grid(
            row=4,
            column=0,
            sticky="w",
            pady=(14, 0),
        )

        ttk.Entry(
            f,
            textvariable=self.output_var,
        ).grid(
            row=5,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=(0, 8),
        )

        ttk.Button(
            f,
            text="Klasör seç",
            command=self._output,
        ).grid(row=5, column=2)

        names = list(LANGUAGES.keys())

        ttk.Label(f, text="Kaynak dil").grid(row=6, column=0, sticky="w", pady=(18, 4))
        ttk.Label(f, text="Hedef dil").grid(row=6, column=1, sticky="w", pady=(18, 4))
        ttk.Label(f, text="Whisper modeli").grid(row=6, column=2, sticky="w", pady=(18, 4))

        ttk.Combobox(
            f,
            values=names,
            textvariable=self.source_var,
            state="readonly",
        ).grid(row=7, column=0, sticky="ew", padx=(0, 8))

        ttk.Combobox(
            f,
            values=[x for x in names if x != "Auto detect"],
            textvariable=self.target_var,
            state="readonly",
        ).grid(row=7, column=1, sticky="ew", padx=(0, 8))

        ttk.Combobox(
            f,
            values=["small", "medium", "large-v3"],
            textvariable=self.model_var,
            state="readonly",
        ).grid(row=7, column=2, sticky="ew")

        ttk.Label(f, text="Cihaz").grid(row=8, column=0, sticky="w", pady=(18, 4))

        ttk.Combobox(
            f,
            values=["auto", "cuda", "cpu"],
            textvariable=self.device_var,
            state="readonly",
        ).grid(row=9, column=0, sticky="ew", padx=(0, 8))

        ttk.Checkbutton(
            f,
            text="VAD filtresi (varsayılan kapalı)",
            variable=self.vad_var,
        ).grid(row=9, column=1, columnspan=2, sticky="w")

        preset = (
            "ASS preset: Arial 15 • Box per line • Outline 0.5 • Shadow 0 • "
            "Bottom-center • L/R 10 • Vertical 5 • max 2 satır • 42 karakter"
        )

        ttk.Label(
            f,
            text=preset,
            wraplength=840,
        ).grid(
            row=10,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(18, 8),
        )

        ttk.Label(
            f,
            text="Final video çıktısı",
        ).grid(
            row=11,
            column=0,
            sticky="w",
            pady=(10, 4),
        )

        final_modes = [
            "Sadece SRT + ASS",
            "MP4 sabit stil - kaynak boyutuna yakın (2-pass)",
            "MP4 sabit stil - maksimum görsel kalite (CRF 14)",
            "MKV sabit ASS stil - kalite kaybı 0",
            "MP4 soft subtitle - kalite kaybı 0 (stil sınırlı)",
        ]

        ttk.Combobox(
            f,
            values=final_modes,
            textvariable=self.final_mode_var,
            state="readonly",
        ).grid(
            row=12,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=(0, 8),
        )

        ttk.Checkbutton(
            f,
            text="Ayrıca kalite kayıpsız MKV + ASS arşivi oluştur",
            variable=self.archive_mkv_var,
        ).grid(row=12, column=2, sticky="w")

        ttk.Label(
            f,
            text=(
                "Not: MP4 içinde stil görüntüye sabit basılacaksa video mutlaka "
                "yeniden encode edilir. 2-pass modu final dosya boyutunu kaynağa "
                "yaklaştırır; birebir bit-kalitesi garantisi vermez."
            ),
            wraplength=840,
        ).grid(
            row=13,
            column=0,
            columnspan=3,
            sticky="w",
            pady=(8, 10),
        )

        self.start_btn = ttk.Button(
            f,
            text="BAŞLAT",
            command=self._start,
        )
        self.start_btn.grid(
            row=14,
            column=0,
            columnspan=3,
            sticky="ew",
            ipady=8,
            pady=(6, 14),
        )

        ttk.Label(
            f,
            textvariable=self.progress_stage,
            font=("Segoe UI", 10, "bold"),
        ).grid(
            row=15,
            column=0,
            columnspan=2,
            sticky="w",
            pady=(4, 2),
        )

        ttk.Label(
            f,
            textvariable=self.progress_text,
        ).grid(
            row=15,
            column=2,
            sticky="e",
            pady=(4, 2),
        )

        self.progress_bar = ttk.Progressbar(
            f,
            orient="horizontal",
            mode="determinate",
            maximum=100,
            variable=self.progress_value,
        )
        self.progress_bar.grid(
            row=16,
            column=0,
            columnspan=3,
            sticky="ew",
            pady=(0, 10),
        )

        ttk.Label(f, text="Durum").grid(row=17, column=0, sticky="w")

        self.log = tk.Text(
            f,
            height=17,
            state="disabled",
            wrap="word",
        )
        self.log.grid(
            row=18,
            column=0,
            columnspan=3,
            sticky="nsew",
            pady=(5, 0),
        )

        for c in range(3):
            f.columnconfigure(c, weight=1)

        f.rowconfigure(18, weight=1)

    def _video(self):
        p = filedialog.askopenfilename(
            filetypes=[
                ("Video", "*.mp4 *.mov *.mkv *.avi *.m4v *.webm"),
                ("All", "*.*"),
            ]
        )
        if p:
            self.video_var.set(p)

    def _output(self):
        p = filedialog.askdirectory()
        if p:
            self.output_var.set(p)

    def _append(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", str(msg) + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _start(self):
        video = self.video_var.get().strip()

        if not video or not Path(video).exists():
            messagebox.showerror("Hata", "Geçerli bir video seç.")
            return

        self.start_btn.configure(state="disabled")
        self._append("Başlatıldı...")

        self.progress_value.set(0.0)
        self.progress_stage.set("Hazırlanıyor")
        self.progress_text.set("%0.0  •  Kalan %100.0")

        source_name = self.source_var.get()
        target_name = self.target_var.get()
        model_size = self.model_var.get()
        device_request = self.device_var.get()
        vad_filter = self.vad_var.get()
        output_dir = self.output_var.get().strip() or None
        mode = self.final_mode_var.get()
        archive_mkv = self.archive_mkv_var.get()

        def work():
            try:
                log = lambda m: self.q.put(("log", str(m)))
                report = lambda pct, stage: self.q.put(
                    ("progress", (float(pct), str(stage)))
                )

                r = run_pipeline(
                    video,
                    source_name,
                    target_name,
                    model_size,
                    device_request,
                    vad_filter,
                    output_dir,
                    log,
                    report,
                )

                final_outputs = {}

                main_video_requested = mode != "Sadece SRT + ASS"
                archive_needed = (
                    archive_mkv
                    and mode != "MKV sabit ASS stil - kalite kaybı 0"
                )

                if archive_needed:
                    archive_end = 83.0 if main_video_requested else 100.0

                    final_outputs["archive_mkv"] = make_styled_mkv_no_reencode(
                        video,
                        r["target_ass"],
                        r["output_dir"],
                        log,
                        report,
                        80.0,
                        archive_end,
                    )
                    final_start = archive_end
                else:
                    final_start = 80.0

                if mode == "MP4 sabit stil - kaynak boyutuna yakın (2-pass)":
                    final_outputs["final_mp4"] = make_mp4_burn_same_size(
                        video,
                        r["target_ass"],
                        r["output_dir"],
                        log,
                        report,
                        final_start,
                        100.0,
                    )

                elif mode == "MP4 sabit stil - maksimum görsel kalite (CRF 14)":
                    final_outputs["final_mp4"] = make_mp4_burn_crf(
                        video,
                        r["target_ass"],
                        r["output_dir"],
                        log,
                        report,
                        crf=14,
                        progress_start=final_start,
                        progress_end=100.0,
                    )

                elif mode == "MKV sabit ASS stil - kalite kaybı 0":
                    final_outputs["final_mkv"] = make_styled_mkv_no_reencode(
                        video,
                        r["target_ass"],
                        r["output_dir"],
                        log,
                        report,
                        80.0,
                        100.0,
                    )

                elif mode == "MP4 soft subtitle - kalite kaybı 0 (stil sınırlı)":
                    final_outputs["soft_mp4"] = make_mp4_soft_no_reencode(
                        video,
                        r["target_srt"],
                        r["output_dir"],
                        log,
                        report,
                        final_start,
                        100.0,
                    )

                elif mode == "Sadece SRT + ASS" and not archive_needed:
                    report(100.0, "SRT + ASS hazır")

                r["final_outputs"] = final_outputs
                self.q.put(("done", r))

            except Exception as e:
                self.q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()

                if kind == "log":
                    self._append(payload)

                elif kind == "progress":
                    percent, stage = payload
                    percent = max(0.0, min(100.0, float(percent)))

                    self.progress_value.set(percent)
                    self.progress_stage.set(stage)
                    self.progress_text.set(
                        f"%{percent:.1f}  •  Kalan %{100.0 - percent:.1f}"
                    )

                elif kind == "error":
                    self.start_btn.configure(state="normal")
                    self.progress_stage.set("Hata")
                    self.progress_text.set("İşlem durduruldu")
                    self._append("HATA: " + payload)
                    messagebox.showerror("Hata", payload)

                elif kind == "done":
                    self.progress_value.set(100.0)
                    self.progress_stage.set("Tamamlandı")
                    self.progress_text.set("%100.0  •  Kalan %0.0")
                    self.start_btn.configure(state="normal")

                    self._append("Kaynak SRT: " + payload["source_srt"])
                    self._append("Hedef SRT: " + payload["target_srt"])
                    self._append("Hedef ASS: " + payload["target_ass"])
                    self._append("Kelime JSON: " + payload["words_json"])
                    self._append("Timeline JSON: " + payload["timeline_json"])

                    for key, value in payload.get("final_outputs", {}).items():
                        self._append(f"{key}: {value}")

                    messagebox.showinfo(
                        "Tamamlandı",
                        f"{payload['caption_count']} caption üretildi.\n"
                        f"Dil: {payload['detected_language']}",
                    )

                    try:
                        os.startfile(payload["output_dir"])
                    except Exception:
                        pass

        except queue.Empty:
            pass

        self.after(100, self._pump)


if __name__ == "__main__":
    App().mainloop()
