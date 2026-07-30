#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["opencv-python-headless>=4.10,<5", "numpy>=1.26,<2.3"]
# ///
"""Render sludge content: 50/50 vertical split, head-locked talking head on top,
random clip on the bottom, word-highlighted live captions synced to the speaker.

    ./sludge.py --head head.mp4 --clip clip.mp4 --message "what they say" --out out.mp4

The top pane is stabilised on the speaker's face: every output frame is an affine
warp that pins the face to a fixed point at a fixed size, so the head sits rock
still and the background moves around it.

Run `./sludge.py --help` for the full option list.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

OUT_W = 1080
OUT_H = 1920
HALF_H = OUT_H // 2  # 960 — each pane is exactly half the canvas
PANE_AR = OUT_W / HALF_H  # 1.125
DETECT_W = 480  # width of the analysis pass; plenty for Haar, cheap to search

# Broadcast-style vocal chain for the head's audio. Phone and camera mics record a
# thin, uneven voice; on a phone speaker that reads as amateur, which undercuts the
# message no matter how well the picture is cut. In order: rumble out, chest weight
# in, a dip through the boxy midrange, presence and air up for intelligibility on a
# small speaker, then two compressors — a fast one to even out syllables and a slow
# one to even out the take — and a limiter before the loudness pass.
VOICE_POLISH = (
    "highpass=f=30:p=2,"
    "lowshelf=f=800:g=7.5:t=q:w=0.35,"
    "equalizer=f=140:t=q:w=1.0:g=3,"
    "equalizer=f=1150:t=q:w=1.4:g=-2.5,"
    "equalizer=f=4200:t=q:w=1.2:g=3,"
    "highshelf=f=3400:g=5.5:t=q:w=0.55,"
    "equalizer=f=8500:t=q:w=1.2:g=2,"
    "acompressor=threshold=-22dB:ratio=3:attack=12:release=160:knee=6:makeup=2,"
    "acompressor=threshold=-26dB:ratio=2.5:attack=200:release=1500:knee=8:makeup=2,"
    "alimiter=limit=-1.5dB:attack=5:release=50"
)
CLIP_VOLUME_SOLO = 0.4  # clip level with no bed to match — it is the only background


def log(msg: str) -> None:
    print(f"[sludge] {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> "None":
    print(f"[sludge] error: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def run(cmd: list[str], quiet: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.PIPE if quiet else None,
        text=True,
        cwd=str(cwd) if cwd else None,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-2500:]
        die(f"command failed ({proc.returncode}): {' '.join(cmd[:3])} ...\n{tail}")
    return proc


# --------------------------------------------------------------------------- probe


@dataclass
class Media:
    path: Path
    width: int
    height: int
    fps: float
    duration: float
    has_audio: bool


def probe_audio(path: Path) -> float:
    """Duration of an audio file (or the audio of a video). Dies if it has none."""
    if not path.exists():
        die(f"input not found: {path}")
    data = json.loads(
        run([
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]).stdout
    )
    audio = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None
    )
    if audio is None:
        die(f"no audio stream in {path}")
    for candidate in (audio.get("duration"), data.get("format", {}).get("duration")):
        try:
            value = float(candidate)
            if value > 0:
                return value
        except (TypeError, ValueError):
            continue
    die(f"could not determine duration of {path}")
    return 0.0


def probe(path: Path) -> Media:
    if not path.exists():
        die(f"input not found: {path}")
    out = run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ]
    ).stdout
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        die(f"no video stream in {path}")
    audio = any(s.get("codec_type") == "audio" for s in streams)

    num, _, den = (video.get("avg_frame_rate") or "0/0").partition("/")
    try:
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    if fps <= 0:
        num, _, den = (video.get("r_frame_rate") or "30/1").partition("/")
        try:
            fps = float(num) / float(den) if float(den) else 30.0
        except (ValueError, ZeroDivisionError):
            fps = 30.0

    duration = 0.0
    for candidate in (video.get("duration"), data.get("format", {}).get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    if duration <= 0:
        die(f"could not determine duration of {path}")

    width, height = int(video["width"]), int(video["height"])
    # Rotation metadata (every phone video) is applied by ffmpeg on decode, so the
    # analysis and warp passes see swapped dimensions. Report them that way too.
    rotation = 0
    for side in video.get("side_data_list", []) or []:
        try:
            rotation = int(abs(float(side.get("rotation", 0)))) % 360
        except (TypeError, ValueError):
            rotation = 0
        if rotation:
            break
    if rotation in (90, 270):
        width, height = height, width

    return Media(
        path=path,
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        has_audio=audio,
    )


# --------------------------------------------------------------------- frame pipes


class FrameReader:
    """Decoded, rotation-corrected, constant-rate BGR frames straight from ffmpeg.

    Going through ffmpeg rather than cv2.VideoCapture matters twice over: it honours
    rotation side data (OpenCV silently ignores it, which would put every tracked
    coordinate in the wrong space for phone footage) and `fps=` forces CFR, so frame
    index i is exactly time i/fps in both the analysis and warp passes.
    """

    def __init__(self, media: Media, start: float, duration: float, fps: float, width: int):
        self.width = width // 2 * 2
        self.height = int(round(self.width * media.height / media.width)) // 2 * 2
        self.frame_bytes = self.width * self.height * 3
        self._closed = False
        self._err = tempfile.TemporaryFile()
        self._proc = subprocess.Popen(
            [
                "ffmpeg", "-v", "error",
                "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(media.path),
                "-an", "-sn", "-map", "0:v:0",
                "-vf", f"fps={fps},scale={self.width}:{self.height}",
                "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=self._err,
        )

    def __iter__(self):
        import numpy as np

        assert self._proc.stdout is not None
        while True:
            buf = self._proc.stdout.read(self.frame_bytes)
            if not buf:
                break
            if len(buf) < self.frame_bytes:  # truncated tail frame
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(self.height, self.width, 3)
        self.close()

    def close(self, check: bool = True) -> None:
        """Idempotent. Callers that stop reading early pass check=False, since
        killing ffmpeg mid-decode is an expected exit, not a failure."""
        if self._closed:
            return
        self._closed = True
        if not check:
            self._proc.kill()
        if self._proc.stdout:
            self._proc.stdout.close()
        code = self._proc.wait()
        if check and code not in (0, None):
            self._err.seek(0)
            tail = self._err.read().decode(errors="replace")[-1500:]
            die(f"ffmpeg decode failed ({code}):\n{tail}")
        self._err.close()


# ----------------------------------------------------------------- transcription


WORD_RE = re.compile(r"[^\W_]+(?:'[^\W_]+)*", re.UNICODE)


def norm(token: str) -> str:
    return "".join(WORD_RE.findall(token.lower()))


@dataclass
class Word:
    text: str
    start: float
    end: float
    emphasis: bool = False


def cache_key(path: Path, *parts: str) -> str:
    st = path.stat()
    raw = "|".join([str(path.resolve()), str(st.st_size), str(int(st.st_mtime)), *parts])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def voice_wav(head: Path, work: Path) -> Path:
    """16 kHz mono copy of the voice, for whisper and for onset refinement."""
    wav = work / "speech.wav"
    if not wav.exists():
        run([
            "ffmpeg", "-y", "-v", "error", "-i", str(head),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
        ])
    return wav


def measure_lufs(wav: Path) -> float | None:
    """Integrated loudness of a file, or None if ffmpeg won't say."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(wav),
         "-af", "aresample=48000,ebur128=framelog=quiet", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    m = re.search(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", out)
    return float(m.group(1)) if m else None


def matched_clip_volume(
    music_volume: float,
    duck_ratio: float,
    clip_duck_ratio: float,
    duck_threshold: float,
    voice_lufs: float,
) -> float:
    """Clip level that lands on the ducked bed while someone is talking.

    Both branches are pulled down by the same key through the same threshold, so
    the only thing separating them is the ratio: a compressor takes a signal
    overshoot*(1 - 1/ratio) dB down, where the overshoot is how far the voice sits
    above the threshold. The gentler ratio on the clip means it keeps more of its
    level — hand that difference back as attenuation and the two land on top of
    each other. With --no-duck both ratios are 1, the difference is zero, and this
    falls out to simply matching the bed's level.

    This is only reliable because the vocal chain pins the voice to a known
    loudness; against a raw take the overshoot is whatever the recording happened
    to be, which is why the caller measures it in that case.
    """
    thresh_db = 20 * math.log10(max(duck_threshold, 1e-6))
    overshoot = max(voice_lufs - thresh_db, 0.0)
    # 1/r is what survives the compressor, so the gap is the difference of those.
    gap_db = overshoot * (
        1.0 / max(clip_duck_ratio, 1.0) - 1.0 / max(duck_ratio, 1.0)
    )
    return music_volume * 10 ** (-max(gap_db, 0.0) / 20)


def energy_db(wav: Path, hop: float = 0.01):
    """Short-time RMS in dB over `hop`-second frames."""
    import numpy as np
    import wave as wavelib

    with wavelib.open(str(wav)) as w:
        sr = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    if x.size == 0:
        return np.zeros(0, dtype=np.float32)
    x /= 32768.0
    step = max(int(sr * hop), 1)
    win = step * 3
    n = max((len(x) - win) // step + 1, 1)
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[:: step][:n]
    rms = np.sqrt((frames**2).mean(axis=1) + 1e-12)
    return 20 * np.log10(rms)


def speech_mask(wav: Path, hop: float = 0.01):
    """Boolean speech/silence mask over 10 ms frames, plus the hop size.

    A plain energy gate with an adaptive threshold. Good enough for finding where
    speech actually starts, which is all this is used for.
    """
    import numpy as np

    db = energy_db(wav, hop)
    if db.size == 0:
        return np.zeros(0, dtype=bool), hop
    floor, peak = np.percentile(db, 10), np.percentile(db, 90)
    threshold = floor + max(6.0, 0.25 * (peak - floor))
    mask = db > threshold
    # Drop one-frame blips so a click can't read as a speech onset.
    if len(mask) > 2:
        smoothed = mask.copy()
        smoothed[1:-1] = mask[:-2] & mask[1:-1] | mask[1:-1] & mask[2:]
        mask = smoothed
    return mask, hop


def find_drop(music: Path, work: Path, hop: float = 0.02) -> float | None:
    """Time of the biggest sustained loudness jump in the bed — the drop.

    Scores every position by how much louder the second after it is than the two
    seconds before it, which is what a drop is. Returns None when nothing in the
    track stands out, rather than inventing a beat to sync to.
    """
    import numpy as np

    db = energy_db(bed_analysis_wav(music, work), hop)
    pre, post = int(2.0 / hop), int(1.0 / hop)
    if db.size < pre + post + 1:
        return None
    scores = np.full(db.size, -np.inf)
    for i in range(pre, db.size - post):
        scores[i] = db[i : i + post].mean() - db[i - pre : i].mean()
    best = int(np.argmax(scores))
    if not np.isfinite(scores[best]) or scores[best] < 3.0:
        log("no clear drop found in the bed — leaving the music where it is")
        return None
    log(f"drop detected at {best * hop:.2f}s in the bed (+{scores[best]:.1f} dB)")
    return best * hop


def bed_analysis_wav(music: Path, work: Path) -> Path:
    wav = work / "bed-analysis.wav"
    if not wav.exists():
        run([
            "ffmpeg", "-y", "-v", "error", "-i", str(music),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
        ])
    return wav


def detect_tempo(music: Path, work: Path, hop: float = 0.01) -> tuple[float, float] | None:
    """Estimate the bed's tempo and beat phase: (bpm, first beat time in seconds).

    Onset envelope -> autocorrelation for the period -> pulse-train correlation for the
    phase. Crude next to a real beat tracker, but it only has to be good enough to put
    one cut on one beat, and it needs no extra dependency.
    """
    import numpy as np

    db = energy_db(bed_analysis_wav(music, work), hop)
    if db.size < int(4 / hop):
        return None
    # Rectified difference of the loudness envelope approximates an onset function.
    flux = np.diff(db, prepend=db[0])
    flux[flux < 0] = 0.0
    flux -= flux.mean()
    if not np.any(flux):
        return None

    lo, hi = int(60 / 180 / hop), int(60 / 60 / hop)  # 180 down to 60 BPM
    corr = np.correlate(flux, flux, mode="full")[flux.size - 1 :]
    if corr.size <= hi:
        return None
    band = corr[lo : hi + 1]
    period = int(np.argmax(band)) + lo
    strength = float(band.max() / (abs(corr[0]) + 1e-9))
    bpm = 60.0 / (period * hop)
    if strength < 0.05:
        log("no steady tempo found in the bed — skipping beat matching")
        return None

    # Phase: slide a one-beat-spaced pulse train and keep the best-scoring offset.
    best_phase, best_score = 0, -np.inf
    for phase in range(period):
        idx = np.arange(phase, flux.size, period)
        score = float(flux[idx].sum())
        if score > best_score:
            best_score, best_phase = score, phase
    log(f"tempo: {bpm:.1f} BPM, first beat at {best_phase * hop:.3f}s (strength {strength:.2f})")
    return bpm, best_phase * hop


def snap_to_grid(t: float, bpm: float, phase: float) -> float:
    """Nearest beat time to t, on the grid defined by bpm and phase."""
    beat = 60.0 / bpm
    k = round((t - phase) / beat)
    return phase + k * beat


def refine_timings(words: list[Word], wav: Path, search: float = 0.6) -> list[Word]:
    """Put word boundaries back on the real speech, at both ends.

    Whisper is biased early on both sides. Starts: it hands a segment's leading
    silence to the first word, running 150-400 ms ahead of the actual onset, and
    highlighting a word before it is audible is the most visible caption fault there
    is. Ends: measured against known audio, its word ends land 260-355 ms early, and
    since the jump-cut logic cuts on word ends, that clips the tail off every closing
    word. So starts walk forward to where sound begins, and ends walk forward through
    continuous speech to where it actually stops.
    """
    import numpy as np

    mask, hop = speech_mask(wav)
    if mask.size == 0 or not words:
        return words

    starts: list[float] = []
    ends: list[float] = []
    limit = int(search / hop)
    for i, w in enumerate(words):
        idx = int(round(w.start / hop))
        if 0 <= idx < len(mask) and not mask[idx]:
            window = mask[idx : min(idx + limit, len(mask))]
            hits = np.flatnonzero(window)
            if hits.size:
                new_start = (idx + int(hits[0])) * hop
                new_start = max(new_start, words[i - 1].start + 0.03 if i else 0.0)
                if new_start < w.end:
                    starts.append(new_start - w.start)
                    w.start = new_start

        # Extend the end through speech that is still going. Bounded by the next
        # word's start, so mid-sentence words just meet their neighbour and only
        # utterance-final words actually grow.
        idx = int(round(w.end / hop))
        ceiling = min(
            words[i + 1].start if i + 1 < len(words) else w.end + search,
            w.end + search,
        )
        stop = int(round(ceiling / hop))
        j = idx
        while j < min(stop, len(mask)) and mask[j]:
            j += 1
        new_end = j * hop
        if new_end > w.end:
            ends.append(new_end - w.end)
            w.end = new_end

    if starts or ends:
        parts = []
        if starts:
            parts.append(
                f"{len(starts)} start(s) +{float(np.median(starts)) * 1000:.0f}ms"
            )
        if ends:
            parts.append(f"{len(ends)} end(s) +{float(np.median(ends)) * 1000:.0f}ms")
        log(f"timing refinement: {', '.join(parts)} (whisper runs early on both)")
    return words


def build_prompt(message: str, vocab: str, limit: int = 700) -> str:
    """Vocabulary hint for whisper, from the message plus any extra terms.

    Whisper conditions on this text, so feeding it the words we already know are
    coming fixes exactly the errors that matter: proper nouns, tickers and jargon.
    Measured on hard audio, `small` alone heard "Kuda Mode" for "CUDA moat" and
    "FX-AX" for "FXAIX"; with the prompt the same model got every word right, at the
    same speed. The window is 224 tokens, so this stays short.
    """
    parts = [p for p in (vocab.strip(), message.strip()) if p]
    prompt = " ".join(parts)
    return prompt[:limit].strip()


def transcribe(
    head: Path, work: Path, cache: Path, model: str, language: str, prompt: str = ""
) -> list[Word]:
    """Word-level timestamps for the talking head audio, cached per input file."""
    # The prompt changes the transcript, so it has to change the cache key too.
    key = cache_key(head, model, language, prompt)
    cached = cache / f"words-{key}.json"
    if cached.exists():
        log(f"using cached transcript {cached.name}")
        raw = json.loads(cached.read_text())
    else:
        whisper = shutil.which("whisper")
        wav = voice_wav(head, work)
        cmd = [whisper] if whisper else ["uvx", "--from", "openai-whisper", "whisper"]
        cmd += [
            str(wav),
            "--model", model,
            "--word_timestamps", "True",
            "--output_format", "json",
            "--output_dir", str(work),
            "--fp16", "False",
            "--verbose", "False",
        ]
        if language and language.lower() != "auto":
            cmd += ["--language", language]
        if prompt:
            cmd += ["--initial_prompt", prompt]
        log(
            f"transcribing with whisper ({model})"
            + (f", primed with {len(prompt)} chars of vocabulary" if prompt else "")
            + " — this is the slow step"
        )
        run(cmd)
        produced = work / "speech.json"
        if not produced.exists():
            die("whisper produced no JSON output")
        raw = json.loads(produced.read_text())
        cache.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(raw))

    words: list[Word] = []
    for seg in raw.get("segments", []):
        for w in seg.get("words", []) or []:
            text = (w.get("word") or "").strip()
            if not text:
                continue
            if not norm(text):
                # Whisper splits off bare symbols ("90" + "%"); glue them back on
                # rather than dropping them, which would change what the caption says.
                if words:
                    words[-1].text += text
                    try:
                        words[-1].end = max(words[-1].end, float(w["end"]))
                    except (KeyError, TypeError, ValueError):
                        pass
                continue
            try:
                start, end = float(w["start"]), float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if end <= start:
                end = start + 0.12
            words.append(Word(text, start, end))
    words.sort(key=lambda w: w.start)
    log(f"transcript: {len(words)} words")
    return words


def spread_evenly(tokens: list[str], start: float, end: float) -> list[Word]:
    if not tokens:
        return []
    span = max(end - start, 0.4 * len(tokens))
    step = span / len(tokens)
    return [
        Word(tok, start + i * step, start + (i + 1) * step)
        for i, tok in enumerate(tokens)
    ]


def align_message(
    message: str, spoken: list[Word], duration: float, force: bool = False
) -> list[Word]:
    """Time the user's message text against the spoken word timestamps.

    Matched runs take whisper's timings verbatim; unmatched runs are interpolated
    between their surrounding anchors, so a message that paraphrases the audio
    still tracks the speaker.
    """
    tokens = [t for t in re.split(r"\s+", message.strip()) if t]
    if not tokens:
        return spoken
    if not spoken:
        log("no speech detected — spreading the message evenly across the clip")
        return spread_evenly(tokens, 0.0, duration)

    a = [norm(t) for t in tokens]
    b = [norm(w.text) for w in spoken]
    timed: list[Word | None] = [None] * len(tokens)
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for ai, bi, size in matcher.get_matching_blocks():
        for k in range(size):
            src = spoken[bi + k]
            timed[ai + k] = Word(tokens[ai + k], src.start, src.end)

    matched = sum(1 for t in timed if t is not None)
    ratio = matched / len(tokens)
    log(f"aligned {matched}/{len(tokens)} message words to the audio ({ratio:.0%})")
    if ratio < 0.6:
        # A paraphrase cannot be word-synced to speech that does not contain it. Any
        # timing we invent here would drift against the voice, so caption what was
        # actually said instead and say so.
        if force:
            log("message does not match the audio — pacing it evenly, which will not sync")
            return spread_evenly(tokens, spoken[0].start, spoken[-1].end)
        log(
            "message does not match the audio closely enough to sync word-for-word "
            "— captioning the transcript instead (use --caption-source message to "
            "override, at the cost of sync)"
        )
        return list(spoken)

    # Fill unmatched gaps by interpolating between the nearest timed anchors.
    i = 0
    while i < len(timed):
        if timed[i] is not None:
            i += 1
            continue
        j = i
        while j < len(timed) and timed[j] is None:
            j += 1
        left = timed[i - 1].end if i > 0 else spoken[0].start
        right = timed[j].start if j < len(timed) else spoken[-1].end
        if right <= left:
            right = left + 0.25 * (j - i)
        filler = spread_evenly(tokens[i:j], left, right)
        for k, w in enumerate(filler):
            timed[i + k] = w
        i = j

    return [w for w in timed if w is not None]


EMPHASIS_RE = re.compile(r"[*_]{1,2}([^*_]+)[*_]{1,2}")
NUMBERISH_RE = re.compile(r"[\d]|[%$€£]")


def strip_emphasis(message: str) -> tuple[str, set[str]]:
    """Pull *emphasis* markup out of the message, returning clean text + marked words."""
    marked: set[str] = set()

    def take(match: re.Match) -> str:
        inner = match.group(1)
        for tok in re.split(r"\s+", inner):
            if norm(tok):
                marked.add(norm(tok))
        return inner

    return EMPHASIS_RE.sub(take, message), marked


def mark_emphasis(
    words: list[Word], marked: set[str], auto_numbers: bool
) -> list[Word]:
    """Flag the words that get the solo treatment.

    Explicit *markup* always wins. Numbers, money and percentages are flagged too
    unless disabled: they are the lines people screenshot, and they read instantly
    even when the viewer has the sound off.
    """
    count = 0
    for w in words:
        token = norm(w.text)
        hit = bool(token) and token in marked
        if not hit and auto_numbers and NUMBERISH_RE.search(w.text):
            hit = True
        if hit:
            w.emphasis = True
            count += 1
    if count:
        log(f"emphasis: {count} word(s) get the solo shake treatment")
    return words


def enforce_min_step(words: list[Word], min_step: float = 0.1) -> list[Word]:
    """Guarantee every word gets a visible slice of the timeline.

    Whisper (and gap interpolation) can hand back zero-length or out-of-order
    words; without this a highlight frame would collapse and the word would never
    light up.
    """
    for i, w in enumerate(words):
        if i:
            prev = words[i - 1]
            if w.start < prev.start + min_step:
                w.start = prev.start + min_step
        if w.end < w.start + min_step:
            w.end = w.start + min_step
    return words


# ------------------------------------------------------------------------- cuts


FILLER = {
    "um", "uh", "erm", "hmm", "like", "basically", "actually", "literally",
    "honestly", "obviously", "anyway", "so", "well", "right", "okay", "ok",
}
FILLER_PHRASES = ("you know", "i mean", "sort of", "kind of", "i guess", "or whatever")


@dataclass
class Cut:
    """One piece of the head video that survives into the output."""

    start: float  # source seconds
    frames: int  # frame-aligned length; the authority for audio and captions too
    fps: float
    framing: float = 1.0  # face-height multiplier, for punch-in variety
    label: str = ""

    @property
    def duration(self) -> float:
        return self.frames / self.fps

    @property
    def end(self) -> float:
        return self.start + self.duration


def sentences(words: list[Word], gap: float = 0.6, max_len: float = 12.0) -> list[list[Word]]:
    """Group words into sentence-ish spans on punctuation, pauses and length."""
    groups: list[list[Word]] = []
    current: list[Word] = []
    for w in words:
        if current and (
            w.start - current[-1].end > gap
            or bool(re.search(r"[.!?…]$", current[-1].text))
            or w.end - current[0].start > max_len
        ):
            groups.append(current)
            current = []
        current.append(w)
    if current:
        groups.append(current)
    return groups


def filler_ratio(text: str, tokens: list[str]) -> float:
    if not tokens:
        return 1.0
    hits = sum(1 for t in tokens if t in FILLER)
    low = text.lower()
    hits += sum(2 * low.count(p) for p in FILLER_PHRASES)
    return min(hits / len(tokens), 1.0)


def score_segment(group: list[Word], message_tokens: set[str]) -> tuple[float, dict]:
    """How well a span carries the message, and how watchable it is on its own.

    Deliberately crude: this ranks candidates for a human (or Claude) to choose
    from, it does not decide the edit.
    """
    text = " ".join(w.text for w in group).strip()
    tokens = [norm(w.text) for w in group]
    tokens = [t for t in tokens if t]
    span = max(group[-1].end - group[0].start, 0.1)
    overlap = len(set(tokens) & message_tokens) / (len(tokens) ** 0.5 or 1)
    density = len(tokens) / span
    fill = filler_ratio(text, tokens)
    numbers = sum(1 for t in tokens if any(ch.isdigit() for ch in t))
    score = 1.6 * overlap + 0.25 * min(density / 3.5, 1.0) + 0.15 * min(numbers, 2) - 0.6 * fill
    return score, {
        "overlap": round(overlap, 3),
        "words_per_sec": round(density, 2),
        "filler": round(fill, 3),
    }


def build_plan(words: list[Word], message: str, target: float) -> dict:
    """Scored, timestamped segment candidates plus one suggested selection."""
    message_tokens = {t for t in (norm(x) for x in re.split(r"\s+", message)) if t}
    groups = sentences(words)
    segments = []
    for i, group in enumerate(groups):
        score, detail = score_segment(group, message_tokens)
        segments.append({
            "index": i,
            "start": round(group[0].start, 2),
            "end": round(group[-1].end, 2),
            "duration": round(group[-1].end - group[0].start, 2),
            "words": len(group),
            "score": round(score, 3),
            "text": " ".join(w.text for w in group).strip(),
            **detail,
        })

    picked: list[dict] = []
    total = 0.0
    for seg in sorted(segments, key=lambda s: -s["score"]):
        if total >= target:
            break
        if seg["duration"] < 0.6:
            continue
        picked.append(seg)
        total += seg["duration"]
    hook = max(picked, key=lambda s: s["score"]) if picked else None
    rest = sorted((s for s in picked if s is not hook), key=lambda s: s["start"])
    suggested = [
        {"start": s["start"], "end": s["end"], "label": label, "index": s["index"]}
        for label, s in ([("hook", hook)] if hook else []) + [("", r) for r in rest]
    ]
    return {
        "message": message,
        "transcript_duration": round(words[-1].end, 2) if words else 0.0,
        "target_duration": target,
        "segments": segments,
        "suggested": {"total_duration": round(total, 2), "cuts": suggested},
    }


def parse_edl(path: Path, limit: float) -> list[tuple[float, float, str]]:
    """Read an edit decision list: [{start, end, label?}, ...] in source seconds.

    Order is honoured as given — segments may be reordered relative to the source,
    which is how a hook gets pulled to the front.
    """
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = data.get("cuts", data.get("suggested", {}).get("cuts", []))
    if not isinstance(data, list) or not data:
        die(f"{path} must be a JSON list of cuts, or an object with a 'cuts' list")
    ranges: list[tuple[float, float, str]] = []
    for entry in data:
        try:
            s, e = float(entry["start"]), float(entry["end"])
        except (KeyError, TypeError, ValueError):
            die(f"{path}: every cut needs numeric 'start' and 'end' fields")
        s = max(0.0, min(s, limit))
        e = max(0.0, min(e, limit))
        if e - s < 0.15:
            log(f"skipping cut {s:.2f}–{e:.2f}s: too short to see")
            continue
        ranges.append((s, e, str(entry.get("label", ""))))
    if not ranges:
        die(f"{path}: no usable cuts")
    return ranges


def apply_jump_cuts(
    ranges: list[tuple[float, float, str]],
    words: list[Word],
    max_gap: float,
    pad: float,
    min_len: float,
) -> list[tuple[float, float, str]]:
    """Split each range on silences longer than max_gap, keeping a little air.

    This is the pacing edit: dead space between sentences is what makes a talking
    head feel slow, and cutting it is what makes the result feel fast.
    """
    if not words:
        log("no word timings available — skipping silence-based jump cuts")
        return ranges

    out: list[tuple[float, float, str]] = []
    removed = 0.0
    for start, end, label in ranges:
        inside = [w for w in words if w.end > start and w.start < end]
        if not inside:
            out.append((start, end, label))
            continue
        runs: list[list[float]] = []
        for w in inside:
            ws, we = max(w.start, start), min(w.end, end)
            if runs and ws - runs[-1][1] <= max_gap:
                runs[-1][1] = we
            else:
                runs.append([ws, we])
        for i, (rs, re_) in enumerate(runs):
            rs = max(rs - pad, start if i == 0 else runs[i - 1][1])
            re_ = min(re_ + pad, end)
            if re_ - rs >= min_len:
                out.append((rs, re_, label if i == 0 else ""))
            else:
                removed += re_ - rs
        removed += (end - start) - sum(r[1] - r[0] for r in runs)
    if removed > 0.05:
        log(f"jump cuts: removed {removed:.1f}s of dead air, {len(out)} segments remain")
    return out


PUNCH_IN_CYCLE = (1.0, 1.16, 0.93, 1.24, 1.08)


def build_cuts(
    ranges: list[tuple[float, float, str]],
    fps: float,
    punch_in: bool,
    seed: int | None,
) -> list[Cut]:
    """Frame-align every range and hand each one a framing.

    Alternating the framing across cuts is what sells a jump cut — an identical
    reframe on both sides of a cut just reads as a glitch.
    """
    rng = random.Random(seed if seed is not None else 0)
    cuts: list[Cut] = []
    offset = rng.randrange(len(PUNCH_IN_CYCLE))
    for i, (start, end, label) in enumerate(ranges):
        frames = int(round((end - start) * fps))
        if frames < 2:
            continue
        framing = PUNCH_IN_CYCLE[(i + offset) % len(PUNCH_IN_CYCLE)] if punch_in else 1.0
        cuts.append(Cut(start=start, frames=frames, fps=fps, framing=framing, label=label))
    if not cuts:
        die("nothing left to render after cutting")
    return cuts


def protect_tail(
    cuts: list[Cut],
    words: list[Word],
    fps: float,
    tail_pad: float,
    source_end: float,
) -> float:
    """Make sure every cut runs past the end of its own last word, plus breathing room.

    Without this the closing word of a cut gets clipped: the cut boundary comes from a
    word end, and a hair of imprecision there is audible as a chopped-off word. Returns
    the protected end time of the final cut, for the beat matcher to respect.
    """
    protected = 0.0
    for cut in cuts:
        inside = [w for w in words if w.start < cut.end and w.end > cut.start]
        if not inside:
            continue
        need = min(max(w.end for w in inside) + tail_pad, source_end)
        if need <= cut.end:
            protected = cut.end
            continue
        frames = int(round((need - cut.start) * fps))
        if frames > cut.frames:
            grew = frames - cut.frames
            cut.frames = frames
            log(
                f"tail guard: extended the cut at {cut.start:.2f}s by {grew} frame(s) "
                f"({grew / fps * 1000:.0f}ms) so its last word finishes"
            )
        protected = cut.end
    return protected


def remap_words(words: list[Word], cuts: list[Cut]) -> list[Word]:
    """Move word timings from source time onto the cut output timeline."""
    out: list[Word] = []
    t = 0.0
    for cut in cuts:
        for w in words:
            if w.end <= cut.start or w.start >= cut.end:
                continue
            s = max(w.start, cut.start) - cut.start + t
            e = min(w.end, cut.end) - cut.start + t
            if e - s > 0.01:
                out.append(Word(w.text, s, e))
        t += cut.duration
    out.sort(key=lambda w: w.start)
    return out


# ------------------------------------------------------------------ face tracking


@dataclass
class Sample:
    cx: float  # face centre, in source pixels
    cy: float
    h: float  # face box height, in source pixels
    origin: str  # detect | template


@dataclass
class Detection:
    cx: float  # anchor point, in analysis pixels
    cy: float
    h: float  # face height, in analysis pixels
    box: tuple[float, float, float, float]  # x, y, w, h — for preview and templates


YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)


def fetch_yunet(cache: Path) -> Path | None:
    """The YuNet ONNX weights, downloaded once and cached (~230 KB)."""
    import urllib.request

    model = cache / "face_detection_yunet_2023mar.onnx"
    if model.exists() and model.stat().st_size > 100_000:
        return model
    cache.mkdir(parents=True, exist_ok=True)
    tmp = model.with_suffix(".part")
    try:
        log("fetching the YuNet face model (one time, ~230 KB)")
        with urllib.request.urlopen(YUNET_URL, timeout=20) as resp, tmp.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
        if tmp.stat().st_size < 100_000:
            raise OSError(f"model download too small ({tmp.stat().st_size} bytes)")
        tmp.replace(model)
        return model
    except Exception as exc:  # offline, blocked, moved — Haar still works
        tmp.unlink(missing_ok=True)
        log(f"could not fetch the YuNet model ({exc}) — falling back to Haar cascades")
        return None


class YuNetFinder:
    """DNN face detector: stable boxes plus five landmarks, per frame.

    Landmarks are the reason to prefer this over Haar for a lock — an anchor derived
    from the eyes and mouth barely moves relative to the face, whereas a cascade's
    bounding box wobbles by tens of pixels between frames on identical input.
    """

    name = "yunet"

    def __init__(self, model: Path, width: int, height: int):
        import cv2

        self.det = cv2.FaceDetectorYN.create(str(model), "", (width, height), 0.6, 0.3, 5000)
        self.det.setInputSize((width, height))
        self._shape = (height, width)

    def find(self, frame, last: Detection | None) -> Detection | None:
        if frame.shape[:2] != self._shape:
            self._shape = frame.shape[:2]
            self.det.setInputSize((frame.shape[1], frame.shape[0]))
        _, faces = self.det.detect(frame)
        if faces is None or len(faces) == 0:
            return None
        best = None
        best_score = -1.0
        for f in faces:
            x, y, w, h = (float(v) for v in f[:4])
            cx, cy = x + w / 2, y + h / 2
            score = float(f[14]) * w * h
            if last is not None:
                dist = ((cx - last.cx) ** 2 + (cy - last.cy) ** 2) ** 0.5
                score /= 1.0 + dist / 120.0
            if score > best_score:
                best_score, best = score, f
        x, y, w, h = (float(v) for v in best[:4])
        pts = [(float(best[4 + 2 * i]), float(best[5 + 2 * i])) for i in range(5)]
        eye = ((pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2)
        mouth = ((pts[3][0] + pts[4][0]) / 2, (pts[3][1] + pts[4][1]) / 2)
        # Sit the anchor between the eyes and the mouth: the most stable point on a
        # face under nodding, turning and talking.
        anchor = (eye[0] + 0.4 * (mouth[0] - eye[0]), eye[1] + 0.4 * (mouth[1] - eye[1]))
        return Detection(cx=anchor[0], cy=anchor[1], h=h, box=(x, y, w, h))


class HaarFinder:
    """Offline fallback: Haar cascades, searched around the last hit when possible."""

    name = "haar"

    def __init__(self):
        self.cascades = load_cascades()

    def find(self, frame, last: Detection | None) -> Detection | None:
        import cv2

        gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        if last is not None:
            lx, ly, lw, lh = last.box
            rx0, ry0 = max(int(lx - lw), 0), max(int(ly - lh), 0)
            rx1 = min(int(lx + 2 * lw), gray.shape[1])
            ry1 = min(int(ly + 2 * lh), gray.shape[0])
            roi = gray[ry0:ry1, rx0:rx1]
            if roi.size and min(roi.shape[:2]) > 24:
                found = detect_in(
                    self.cascades, roi, max(int(lh * 0.55), 20), max(int(lh * 1.9), 40)
                )
                if found:
                    x, y, w, h = max(found, key=lambda b: b[2] * b[3])
                    return self._detection(x + rx0, y + ry0, w, h)

        found = detect_in(self.cascades, gray, max(24, int(min(gray.shape[:2]) * 0.10)), None)
        if not found:
            return None
        if last is None:
            x, y, w, h = max(found, key=lambda b: b[2] * b[3])
        else:
            x, y, w, h = min(
                found,
                key=lambda b: (b[0] + b[2] / 2 - last.cx) ** 2 + (b[1] + b[3] / 2 - last.cy) ** 2,
            )
        return self._detection(x, y, w, h)

    @staticmethod
    def _detection(x, y, w, h) -> Detection:
        # Aim a little above box centre — eyes and mouth read better as the anchor
        # point than the chin does.
        return Detection(cx=x + w / 2, cy=y + h / 2 - 0.05 * h, h=h, box=(x, y, w, h))


def build_finder(detector: str, cache: Path, width: int, height: int, model: Path | None):
    if detector in ("auto", "yunet"):
        path = model or fetch_yunet(cache)
        if path:
            try:
                finder = YuNetFinder(path, width, height)
                log("face detector: YuNet (DNN, with landmarks)")
                return finder
            except Exception as exc:
                log(f"YuNet failed to load ({exc}) — falling back to Haar cascades")
        if detector == "yunet":
            die("YuNet was requested but its model could not be loaded")
    log("face detector: Haar cascades")
    return HaarFinder()


def load_cascades():
    import cv2

    haar = cv2.data.haarcascades
    cascades = []
    for name, flip in (
        ("haarcascade_frontalface_default.xml", False),
        ("haarcascade_frontalface_alt2.xml", False),
        ("haarcascade_profileface.xml", False),
        ("haarcascade_profileface.xml", True),
    ):
        cascade = cv2.CascadeClassifier(os.path.join(haar, name))
        if not cascade.empty():
            cascades.append((cascade, flip))
    if not cascades:
        die("no OpenCV Haar cascades available for face detection")
    return cascades


def detect_in(cascades, gray, min_side: int, max_side: int | None):
    import cv2

    kwargs = {"scaleFactor": 1.1, "minNeighbors": 5, "minSize": (min_side, min_side)}
    if max_side:
        kwargs["maxSize"] = (max_side, max_side)
    for cascade, flip in cascades:
        img = cv2.flip(gray, 1) if flip else gray
        found = cascade.detectMultiScale(img, **kwargs)
        if len(found) == 0:
            continue
        boxes = []
        for (x, y, w, h) in found:
            if flip:
                x = gray.shape[1] - x - w
            boxes.append((float(x), float(y), float(w), float(h)))
        return boxes
    return []


TEMPLATE_RUN_LIMIT = 8  # frames of template-only tracking before we admit we lost it


def track_faces(
    media: Media,
    start: float,
    duration: float,
    fps: float,
    finder,
    preview: Path | None,
) -> tuple[list[Sample | None], int]:
    """Per-output-frame face samples, in source pixel coordinates.

    Detection runs on every frame — a lock is only as tight as its worst frame.
    Detector dropouts are bridged by template matching, but only for a bounded run:
    a template carries the previous face size forward unchanged, so leaning on it
    for long would quietly freeze the zoom and let the position drift.
    """
    import cv2

    reader = FrameReader(media, start, duration, fps, DETECT_W)
    scale = reader.width / media.width  # analysis px -> source px is 1/scale
    writer = None
    if preview:
        writer = cv2.VideoWriter(
            str(preview), cv2.VideoWriter_fourcc(*"mp4v"), fps,
            (reader.width, reader.height),
        )

    samples: list[Sample | None] = []
    last: Detection | None = None
    prev_gray = None
    pending = 0
    template_run = 0
    detects = templates = 0

    for frame in reader:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        found = finder.find(frame, last)
        origin = ""

        if found is not None and last is not None:
            jump = ((found.cx - last.cx) ** 2 + (found.cy - last.cy) ** 2) ** 0.5
            if jump > 0.25 * reader.width and template_run == 0:
                # A far-away hit is a false positive until it proves itself over
                # consecutive frames.
                pending += 1
                if pending < 3:
                    found = None
            else:
                pending = 0
        if found is not None:
            origin = "detect"
            pending = 0
            template_run = 0
        elif last is not None and prev_gray is not None and template_run < TEMPLATE_RUN_LIMIT:
            box = match_template(prev_gray, gray, last.box)
            if box is not None:
                x, y, w, h = box
                dx, dy = x - last.box[0], y - last.box[1]
                found = Detection(cx=last.cx + dx, cy=last.cy + dy, h=last.h, box=box)
                origin = "template"
                template_run += 1

        if found is not None:
            last = found
            samples.append(
                Sample(cx=found.cx / scale, cy=found.cy / scale, h=found.h / scale, origin=origin)
            )
            if origin == "detect":
                detects += 1
            else:
                templates += 1
        else:
            samples.append(None)
            if template_run >= TEMPLATE_RUN_LIMIT:
                last = None  # stop steering the search with a stale position
                template_run = 0

        if writer is not None:
            annotated = frame.copy()
            if found is not None:
                x, y, w, h = (int(v) for v in found.box)
                color = (0, 255, 0) if origin == "detect" else (0, 200, 255)
                cv2.rectangle(annotated, (x, y), (x + w, y + h), color, 2)
                cv2.drawMarker(
                    annotated, (int(found.cx), int(found.cy)), (0, 0, 255),
                    cv2.MARKER_CROSS, 14, 2,
                )
            writer.write(annotated)
        prev_gray = gray

    if writer is not None:
        writer.release()
        log(f"wrote face-detection preview {preview}")

    total = len(samples)
    if total:
        log(
            f"face samples: {detects} detected + {templates} bridged "
            f"of {total} frames ({(detects + templates) / total:.0%} coverage)"
        )
    return samples, total


def load_track_json(path: Path, media: Media, fps: float, n_frames: int) -> tuple[list[Sample | None], int]:
    """Load an externally supplied track: [{frame|t, cx, cy, h}, ...] in source pixels.

    Exists so the warp geometry can be exercised against a known-correct track,
    independent of whatever the detector does.
    """
    entries = json.loads(path.read_text())
    if not isinstance(entries, list) or not entries:
        die(f"{path} must be a non-empty JSON list of samples")
    samples: list[Sample | None] = [None] * n_frames
    for entry in entries:
        if "frame" in entry:
            idx = int(entry["frame"])
        elif "t" in entry:
            idx = int(round(float(entry["t"]) * fps))
        else:
            die(f"{path}: every sample needs a 'frame' or 't' field")
        if 0 <= idx < n_frames:
            samples[idx] = Sample(
                cx=float(entry["cx"]), cy=float(entry["cy"]),
                h=float(entry.get("h", media.height * 0.25)), origin="detect",
            )
    filled = sum(1 for s in samples if s is not None)
    log(f"external track: {filled}/{n_frames} frames from {path.name}")
    return samples, n_frames


def match_template(prev_gray, gray, box) -> tuple[float, float, float, float] | None:
    """Follow the face through a detector dropout by matching the previous patch."""
    import cv2

    x, y, w, h = (int(v) for v in box)
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + w, prev_gray.shape[1]), min(y + h, prev_gray.shape[0])
    if x1 - x0 < 16 or y1 - y0 < 16:
        return None
    template = prev_gray[y0:y1, x0:x1]
    pad_x, pad_y = int(0.5 * (x1 - x0)), int(0.5 * (y1 - y0))
    sx0, sy0 = max(x0 - pad_x, 0), max(y0 - pad_y, 0)
    sx1 = min(x1 + pad_x, gray.shape[1])
    sy1 = min(y1 + pad_y, gray.shape[0])
    region = gray[sy0:sy1, sx0:sx1]
    if region.shape[0] < template.shape[0] or region.shape[1] < template.shape[1]:
        return None
    res = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
    _, best, _, loc = cv2.minMaxLoc(res)
    if best < 0.55:
        return None
    return (float(sx0 + loc[0]), float(sy0 + loc[1]), float(x1 - x0), float(y1 - y0))


def median_filter(values, k: int):
    import numpy as np

    if len(values) < k or k < 3:
        return values
    pad = k // 2
    padded = np.pad(values, pad, mode="edge")
    return np.array([np.median(padded[i : i + k]) for i in range(len(values))])


def ema_zero_phase(values, alpha: float):
    """Forward then backward exponential smoothing — smooth without lag.

    Lag matters here: a one-directional filter would leave the head trailing behind
    the crop on every fast move, which reads as slop, not as a lock.
    """
    import numpy as np

    out = np.array(values, dtype=float)
    if alpha >= 1.0 or len(out) < 2:
        return out
    for _ in range(2):
        acc = out[0]
        for i in range(len(out)):
            acc += alpha * (out[i] - acc)
            out[i] = acc
        out = out[::-1].copy()
    return out


def fill_gaps(values, valid, fallback: float):
    """Interpolate across frames with no sample; hold at the ends."""
    import numpy as np

    values = np.asarray(values, dtype=float)
    valid = np.asarray(valid, dtype=bool)
    if not valid.any():
        return np.full(len(values), fallback)
    idx = np.arange(len(values))
    return np.interp(idx, idx[valid], values[valid])


def crop_window(media: Media, zoom: float) -> tuple[float, float]:
    """Largest pane-aspect window that fits the source, tightened by zoom."""
    h = min(media.height, media.width / PANE_AR)
    h = h / max(zoom, 1.0)
    w = h * PANE_AR
    if w > media.width:
        w = media.width
        h = w / PANE_AR
    return w, h


@dataclass
class LockTrack:
    """Per-frame warp parameters: face centre and source->pane scale."""

    cx: list[float]
    cy: list[float]
    scale: list[float]
    mode: str


def build_lock(
    samples: list[Sample | None],
    media: Media,
    n_frames: int,
    *,
    mode: str,
    zoom: float,
    face_height: float,
    face_y: float,
    tightness: float,
    scale_smooth: float,
    pan_smooth: float,
    headroom: float,
    edge: str,
) -> LockTrack:
    import numpy as np

    have = [s for s in samples if s is not None]
    if mode != "none" and not have:
        log("no face found anywhere in the head video — falling back to a static crop")
        mode = "none"

    # A window can never be larger than the source, or the warp would have to invent
    # pixels; in mirror mode we allow it and reflect instead.
    scale_floor = max(OUT_W / media.width, HALF_H / media.height)

    if mode == "none":
        win_w, win_h = crop_window(media, zoom)
        scale = OUT_W / win_w
        cx = np.full(n_frames, media.width / 2.0)
        cy = np.full(n_frames, media.height * face_y if have else media.height / 2.0)
        scale_track = np.full(n_frames, max(scale, scale_floor))
    else:
        valid = [s is not None for s in samples]
        cx_raw = [s.cx if s else 0.0 for s in samples]
        cy_raw = [s.cy if s else 0.0 for s in samples]
        h_raw = [s.h if s else 0.0 for s in samples]
        cx = fill_gaps(cx_raw, valid, media.width / 2.0)
        cy = fill_gaps(cy_raw, valid, media.height / 2.0)
        face_h = fill_gaps(h_raw, valid, media.height * 0.25)

        if mode == "tight":
            # Translation barely filtered — only enough to kill detector jitter, so
            # the head stays pinned. Box size is much noisier than box position, so
            # scale gets heavy filtering or the pane would visibly pulse.
            cx = ema_zero_phase(median_filter(cx, 3), tightness)
            cy = ema_zero_phase(median_filter(cy, 3), tightness)
            face_h = ema_zero_phase(median_filter(face_h, 9), scale_smooth)
            target = max(face_height, 0.05) * HALF_H
            scale_track = np.clip(target / np.maximum(face_h, 1e-3), 0.05, 12.0)
            if edge == "clamp":
                # A window that exactly fits the frame has nowhere to pan, so the
                # lock would break the moment the head moves. Zoom in past the fit
                # scale to buy margin on every side.
                scale_track = np.maximum(scale_track, scale_floor * max(headroom, 1.0))
            else:
                scale_track = np.maximum(scale_track, 0.05)
        else:  # smooth: classic lazy pan at a fixed framing
            cx = ema_zero_phase(median_filter(cx, 5), pan_smooth)
            cy = ema_zero_phase(median_filter(cy, 5), pan_smooth)
            win_w, _ = crop_window(media, zoom)
            scale_track = np.full(n_frames, max(OUT_W / win_w, scale_floor))

    if edge == "clamp":
        # Keep the sampled window inside the frame. The window is not centred on the
        # face vertically (face_y biases it), so each axis clamps against its own
        # asymmetric margins.
        win_w = OUT_W / scale_track
        win_h = HALF_H / scale_track
        cx = np.clip(cx, win_w / 2.0, np.maximum(media.width - win_w / 2.0, win_w / 2.0))
        cy = np.clip(
            cy,
            face_y * win_h,
            np.maximum(media.height - (1.0 - face_y) * win_h, face_y * win_h),
        )

    log(
        f"lock mode {mode}: scale {float(np.min(scale_track)):.2f}–"
        f"{float(np.max(scale_track)):.2f}x, "
        f"drift {float(np.max(cx) - np.min(cx)):.0f}x{float(np.max(cy) - np.min(cy)):.0f}px"
    )
    return LockTrack(list(cx), list(cy), list(scale_track), mode)


# ---------------------------------------------------------------------- captions


def ass_color(hexcolor: str) -> str:
    h = hexcolor.strip().lstrip("#")
    if len(h) != 6:
        die(f"colour must be #RRGGBB, got {hexcolor!r}")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H00{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    t = max(t, 0.0)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def group_words(words: list[Word], max_words: int, max_chars: int, gap: float) -> list[list[Word]]:
    groups: list[list[Word]] = []
    current: list[Word] = []
    chars = 0
    for w in words:
        breaks = False
        if current:
            breaks = (
                len(current) >= max_words
                or chars + 1 + len(w.text) > max_chars
                or w.start - current[-1].end > gap
                or bool(re.search(r"[.!?…]$", current[-1].text))
                # An emphasised word stands alone, so it always starts a new group
                # and the one before it always ends here.
                or w.emphasis
                or current[-1].emphasis
            )
        if breaks:
            groups.append(current)
            current, chars = [], 0
        current.append(w)
        chars += len(w.text) + (1 if chars else 0)
    if current:
        groups.append(current)
    return groups


def shake_events(
    word: Word,
    end: float,
    *,
    anchor: tuple[int, int],
    scale: int,
    amount: float,
    hz: float,
    color: str,
    rng: random.Random,
) -> list[str]:
    """One emphasised word, alone on screen, jittering in place.

    ASS has no shake primitive, so the word is re-emitted every ~1/hz seconds with a
    fresh \\pos and rotation. Offsets come from a seeded RNG, so a re-render of the
    same input shakes identically.
    """
    events: list[str] = []
    span = max(end - word.start, 1 / hz)
    steps = max(int(span * hz), 2)
    step = span / steps
    token = ass_escape(word.text)
    ax, ay = anchor
    for i in range(steps):
        t0 = word.start + i * step
        t1 = min(t0 + step, end)
        if t1 <= t0:
            continue
        # Decay the shake so it hits hard and settles, instead of buzzing throughout.
        decay = 1.0 - (i / steps) * 0.65
        dx = rng.uniform(-amount, amount) * decay
        dy = rng.uniform(-amount, amount) * decay
        rot = rng.uniform(-3.5, 3.5) * decay
        pop = scale + (8 if i == 0 else 0)
        events.append(
            f"Dialogue: 1,{ass_time(t0)},{ass_time(t1)},Sludge,,0,0,0,,"
            f"{{\\an5\\pos({ax + dx:.0f},{ay + dy:.0f})\\frz{rot:.1f}"
            f"\\c{color}\\fscx{pop}\\fscy{pop}}}{token}"
        )
    return events


def write_ass(
    words: list[Word],
    path: Path,
    duration: float,
    *,
    font: str,
    size: int,
    base_color: str,
    highlight: str,
    outline_color: str,
    outline: int,
    position: str,
    uppercase: bool,
    max_words: int,
    max_chars: int,
    pop: int,
    emphasis_scale: float = 1.45,
    shake: bool = True,
    shake_px: float = 16.0,
    shake_hz: float = 22.0,
    seed: int | None = None,
) -> None:
    total = len(words)
    words = [w for w in words if w.start < duration]
    if len(words) < total:
        log(f"{total - len(words)} caption word(s) fell past the {duration:.1f}s cut")
    for w in words:
        w.end = min(w.end, duration)
        if uppercase:
            w.text = w.text.upper()

    align = {"center": 5, "top": 8, "bottom": 2}.get(position, 5)
    margin_v = {"center": 0, "top": 160, "bottom": 320}.get(position, 0)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.601

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sludge,{font},{size},{ass_color(base_color)},{ass_color(highlight)},{ass_color(outline_color)},&H64000000,-1,0,0,0,100,100,0,0,1,{outline},3,{align},60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    hi = ass_color(highlight)
    base = ass_color(base_color)
    anchor = {
        "center": (OUT_W // 2, OUT_H // 2),
        "top": (OUT_W // 2, 300),
        "bottom": (OUT_W // 2, OUT_H - 420),
    }.get(position, (OUT_W // 2, OUT_H // 2))
    rng = random.Random(seed if seed is not None else 0)
    groups = group_words(words, max_words, max_chars, gap=0.9)
    events: list[str] = []
    for gi, group in enumerate(groups):
        group_end = group[-1].end
        if gi + 1 < len(groups):
            group_end = max(group_end, min(groups[gi + 1][0].start, group[-1].end + 1.2))
        else:
            group_end = min(group[-1].end + 0.5, duration)

        if len(group) == 1 and group[0].emphasis:
            # Solo word: no karaoke pass needed, it is the whole caption.
            solo = group[0]
            hold = min(group_end, solo.end + 0.25)
            if shake:
                events.extend(
                    shake_events(
                        solo, hold, anchor=anchor,
                        scale=int(emphasis_scale * 100), amount=shake_px,
                        hz=shake_hz, color=hi, rng=rng,
                    )
                )
            else:
                scale = int(emphasis_scale * 100)
                events.append(
                    f"Dialogue: 1,{ass_time(solo.start)},{ass_time(hold)},Sludge,,0,0,0,,"
                    f"{{\\c{hi}\\fscx{scale}\\fscy{scale}}}{ass_escape(solo.text)}"
                )
            continue

        for wi, active in enumerate(group):
            start = active.start if wi else group[0].start
            end = group[wi + 1].start if wi + 1 < len(group) else group_end
            if end <= start:
                continue
            parts = []
            for k, w in enumerate(group):
                token = ass_escape(w.text)
                if k == wi:
                    parts.append(
                        f"{{\\c{hi}\\fscx{100 + pop}\\fscy{100 + pop}}}{token}"
                        f"{{\\c{base}\\fscx100\\fscy100}}"
                    )
                else:
                    parts.append(token)
            events.append(
                f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Sludge,,0,0,0,,"
                + " ".join(parts)
            )
    path.write_text(header + "\n".join(events) + "\n")
    log(f"captions: {len(groups)} groups / {len(events)} highlight frames")


# ------------------------------------------------------------------------ render


def ff_escape(path: Path) -> str:
    """Filtergraph-safe reference to a sidecar file.

    ffmpeg's filtergraph parser chokes on quotes, colons and commas in option
    values no matter how they are escaped, so sidecars are always referenced by
    bare filename and the render runs with cwd set to their directory.
    """
    name = path.name
    if re.search(r"[',:\[\]\\;]", name):
        die(f"internal sidecar name is not filtergraph-safe: {name}")
    return name


def blurred_cover(frame):
    """Pane-sized blurred fill, via a downscale/upscale round trip (cheap blur)."""
    import cv2

    h, w = frame.shape[:2]
    cover = max(OUT_W / w, HALF_H / h)
    small = cv2.resize(
        frame, (max(int(w * cover / 12), 16), max(int(h * cover / 12), 16)),
        interpolation=cv2.INTER_AREA,
    )
    small = cv2.GaussianBlur(small, (0, 0), 3)
    big = cv2.resize(small, (int(w * cover) + 2, int(h * cover) + 2), interpolation=cv2.INTER_LINEAR)
    x = max((big.shape[1] - OUT_W) // 2, 0)
    y = max((big.shape[0] - HALF_H) // 2, 0)
    return big[y : y + HALF_H, x : x + OUT_W].copy()


def render_cta(cta: Media, work: Path, fps: float, limit: float | None) -> tuple[Path, float]:
    """Normalise the CTA to a full-frame 1080x1920 tail so it can concat cleanly."""
    out = work / "cta.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(cta.path)]
    if limit:
        cmd += ["-t", f"{limit:.3f}"]
    cmd += [
        "-an", "-filter_complex",
        f"[0:v]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{OUT_H},fps={fps},setsar=1,format=yuv420p[v]",
        "-map", "[v]", "-c:v", "libx264", "-crf", "17", "-preset", "veryfast",
        str(out),
    ]
    run(cmd)
    duration = probe(out).duration
    log(f"CTA tail: {cta.path.name} -> {duration:.2f}s full-frame")
    return out, duration


def render_bottom(clip: Media, work: Path, duration: float, fps: float, clip_offset: float) -> Path:
    bottom = work / "bottom.mp4"
    log(f"pre-rendering bottom pane from {clip.path.name} @ {clip_offset:.2f}s")
    run([
        "ffmpeg", "-y", "-v", "error",
        "-stream_loop", "-1", "-i", str(clip.path),
        "-ss", f"{clip_offset:.3f}", "-t", f"{duration:.3f}",
        "-an", "-filter_complex",
        f"[0:v]scale={OUT_W}:{HALF_H}:force_original_aspect_ratio=increase,"
        f"crop={OUT_W}:{HALF_H},fps={fps},setsar=1,format=yuv420p[v]",
        "-map", "[v]", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
        str(bottom),
    ])
    return bottom


def render(
    head: Media,
    clip: Media,
    out: Path,
    work: Path,
    timeline: list[tuple[Cut, LockTrack]],
    *,
    duration: float,
    assfile: Path,
    fps: float,
    face_y: float,
    edge: str,
    clip_offset: float,
    clip_mute: bool,
    clip_volume: float,
    clip_duck_ratio: float,
    voice_polish: bool,
    voice_lufs: float,
    music: Path | None,
    music_offset: float,
    music_delay: float,
    music_volume: float,
    music_fade: float,
    duck_ratio: float,
    duck_threshold: float,
    cta: Media | None,
    cta_limit: float | None,
    cta_volume: float,
    echo_start: float | None,
    echo_delay: float,
    echo_repeats: int,
    echo_decay: float,
    echo_mix: float,
    seam: int,
    crf: int,
    preset: str,
) -> None:
    import cv2
    import numpy as np

    bottom = render_bottom(clip, work, duration, fps, clip_offset)
    body = duration  # the split-screen section; the CTA is appended after it
    cta_file, cta_duration = (
        render_cta(cta, work, fps, cta_limit) if cta else (None, 0.0)
    )
    total = body + cta_duration

    # Decoding a 4K source full-size only to shrink it in the warp is wasted work;
    # decode just enough that the widest sampled window still oversamples the pane.
    all_scales = [s for _, track in timeline for s in track.scale] or [1.0]
    decode_scale = min(1.0, max(0.25, min(all_scales) * 1.4))
    decode_w = max(320, int(round(head.width * decode_scale)) // 2 * 2)
    decode_scale = decode_w / head.width
    if decode_scale < 1.0:
        log(f"decoding head at {decode_scale:.2f}x ({decode_w}px wide) for the warp")

    seam_filter = (
        f",drawbox=x=0:y={HALF_H - seam // 2}:w={OUT_W}:h={seam}:color=white@0.85:t=fill"
        if seam > 0
        else ""
    )
    # Captions are burned before the concat, so they can never bleed onto the CTA.
    filtergraph = (
        f"[0:v]format=yuv420p[top];"
        f"[1:v]fps={fps},setsar=1,format=yuv420p[bot];"
        f"[top][bot]vstack=inputs=2[stk];"
        f"[stk]ass=f={ff_escape(assfile)}{seam_filter}[body]"
    )

    cmd = [
        "ffmpeg", "-y", "-v", "error", "-stats",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{OUT_W}x{HALF_H}",
        "-r", f"{fps}", "-i", "pipe:0",
        "-i", str(bottom),
    ]
    next_input = 2
    if cta_file:
        cmd += ["-i", str(cta_file)]
        filtergraph += (
            f";[{next_input}:v]fps={fps},setsar=1,format=yuv420p[ctav]"
            f";[body][ctav]concat=n=2:v=1:a=0[v]"
        )
        next_input += 1
    else:
        filtergraph += ";[body]null[v]"
    # apad + atrim pin every audio branch to the exact output duration, so a voice
    # track shorter than the video can never truncate the render. The voice covers
    # only the body, so padding is also what silences it under the CTA — which is
    # what makes the ducking stop there.
    fit = f"aresample=48000,apad,atrim=0:{total:.3f},asetpts=PTS-STARTPTS"
    chains: list[str] = []
    branches: list[str] = []  # everything to be mixed down
    key: str | None = None  # voice copy that triggers the ducking
    audio_label: str | None = None
    if head.has_audio:
        # The voice is cut to the same frame-aligned boundaries as the picture, then
        # concatenated. Each piece gets a 12 ms fade so hard cuts don't click.
        cmd += ["-i", str(head.path)]
        voice_in = next_input
        next_input += 1
        n = len(timeline)
        # EQ and dynamics run on the uncut take, ahead of the trims: the two
        # compressors need continuous context to settle, and running them per-cut
        # would let each piece land at its own level and pump at every seam. The
        # loudness pass is the exception and waits until after the concat, below.
        src = f"[{voice_in}:a]"
        if voice_polish:
            chains.append(f"{src}{VOICE_POLISH}[vpol]")
            src = "[vpol]"
        chains.append(
            f"{src}asplit={n}" + "".join(f"[vs{i}]" for i in range(n))
            if n > 1
            else f"{src}anull[vs0]"
        )
        for i, (cut, _) in enumerate(timeline):
            fade = min(0.012, cut.duration / 4)
            chains.append(
                f"[vs{i}]atrim=start={cut.start:.4f}:end={cut.end:.4f},"
                f"asetpts=PTS-STARTPTS,aresample=48000,"
                f"afade=t=in:st=0:d={fade:.4f},"
                f"afade=t=out:st={max(cut.duration - fade, 0):.4f}:d={fade:.4f}[vc{i}]"
            )
        chains.append(
            "".join(f"[vc{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[vjoined]"
        )
        # Loudness is measured on the cut program, not the raw take, so the moving
        # gain follows what is actually heard. Normalising before the trims would
        # leave a gain step at every cut, since the trajectory was fitted to
        # material that is no longer there.
        norm = (
            f"loudnorm=I={voice_lufs:g}:TP=-1.5:LRA=4,"
            if voice_polish
            else ""
        )
        chains.append(f"[vjoined]{norm}{fit}[voice]")

    # The clip's own sound and the music bed both sit under the voice, but they want
    # different amounts of ducking: the music can drop a long way and still do its job,
    # while the clip is the bottom pane's own noise and has to stay audible or the pane
    # feels dead. They get separate compressors keyed off the same voice, so they still
    # move in lockstep and cannot pump against each other.
    bus: list[tuple[str, float]] = []  # (label, duck ratio)
    if clip.has_audio and not clip_mute:
        cmd += ["-stream_loop", "-1", "-i", str(clip.path)]
        chains.append(
            f"[{next_input}:a]atrim=start={clip_offset:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=48000,volume={clip_volume},aformat=channel_layouts=stereo,"
            f"apad,atrim=0:{total:.3f},asetpts=PTS-STARTPTS[bgclip]"
        )
        next_input += 1
        bus.append(("[bgclip]", clip_duck_ratio))
    if music:
        # The bed runs continuously across the cuts — unlike the voice, which is cut
        # with the picture. That continuity is what stops jump cuts feeling stuttery.
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        delay = (
            f"adelay={int(music_delay * 1000)}|{int(music_delay * 1000)},"
            if music_delay > 0.001
            else ""
        )
        chains.append(
            f"[{next_input}:a]atrim=start={music_offset:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=48000,{delay}volume={music_volume},"
            f"aformat=channel_layouts=stereo,apad,atrim=0:{total:.3f},"
            f"asetpts=PTS-STARTPTS[bgmusic]"
        )
        next_input += 1
        bus.append(("[bgmusic]", duck_ratio))

    wants_echo = head.has_audio and echo_start is not None and echo_delay > 0
    # Only chains that actually duck get a key copy — an unconsumed asplit output is a
    # dangling pad and ffmpeg refuses the whole graph.
    keys: dict[int, str] = {}
    if head.has_audio:
        ducking = [i for i, (_, ratio) in enumerate(bus) if ratio > 1.0]
        keys = {idx: f"[key{idx}]" for idx in ducking}
        # Copies needed: one to mix, one per ducking chain, and one to feed the echo.
        labels = ["[voicemix]"] + [keys[i] for i in ducking]
        if wants_echo:
            labels.append("[voicetail]")
        if len(labels) > 1:
            chains.append(f"[voice]asplit={len(labels)}" + "".join(labels))
        else:
            chains.append("[voice]anull[voicemix]")
        branches.append("[voicemix]")
        key = keys.get(ducking[0]) if ducking else None
    if wants_echo:
        # Only the closing words are delayed, and each repeat is built explicitly:
        # aecho would pass the dry signal through as well, which would quietly make the
        # closing words louder than the rest of the take. This branch is repeats only.
        n_rep = max(echo_repeats, 1)
        chains.append(
            f"[voicetail]atrim=start={echo_start:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo,"
            f"asplit={n_rep}" + "".join(f"[tail{i}]" for i in range(n_rep))
            if n_rep > 1
            else f"[voicetail]atrim=start={echo_start:.3f},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo,anull[tail0]"
        )
        for i in range(n_rep):
            # Repeat i lands (i+1) delay steps after the word, and the whole branch is
            # pushed back to where the tail started. Nothing trims it to the body, so
            # the repeats keep ringing across the CTA cut.
            offset_ms = int((echo_start + echo_delay * (i + 1)) * 1000)
            gain = echo_mix * (echo_decay ** i)
            chains.append(
                f"[tail{i}]volume={gain:.4f},adelay={offset_ms}|{offset_ms}[rep{i}]"
            )
        if n_rep > 1:
            chains.append(
                "".join(f"[rep{i}]" for i in range(n_rep))
                + f"amix=inputs={n_rep}:normalize=0:duration=longest[echo0]"
            )
        else:
            chains.append("[rep0]anull[echo0]")
        chains.append(f"[echo0]{fit}[echo]")
        branches.append("[echo]")

    if bus:
        ducked: list[str] = []
        for i, (label, ratio) in enumerate(bus):
            if i in keys:
                # Real ducking, not a fixed level: the compressor is driven by the
                # voice, so it drops only while someone is talking and comes back up
                # between — including under the CTA, where the voice is silent by
                # construction.
                chains.append(f"{keys[i]}aformat=channel_layouts=stereo[bk{i}]")
                chains.append(
                    f"{label}[bk{i}]sidechaincompress=threshold={duck_threshold}:"
                    f"ratio={ratio}:attack=15:release=350:makeup=1:knee=4[duck{i}]"
                )
            else:
                chains.append(f"{label}anull[duck{i}]")
            ducked.append(f"[duck{i}]")
        if len(ducked) > 1:
            chains.append(
                "".join(ducked)
                + f"amix=inputs={len(ducked)}:normalize=0:duration=first[bus0]"
            )
        else:
            chains.append(f"{ducked[0]}anull[bus0]")
        fade_out = max(total - music_fade, 0.0)
        chains.append(
            f"[bus0]{fit},afade=t=in:st=0:d={min(music_fade, total / 4):.3f},"
            f"afade=t=out:st={fade_out:.3f}:d={music_fade:.3f}[bus]"
        )
        branches.append("[bus]")
    if cta_file and cta.has_audio and cta_volume > 0:
        # The CTA's own audio starts exactly where the tail does.
        cmd += ["-i", str(cta.path)]
        chains.append(
            f"[{next_input}:a]aresample=48000,volume={cta_volume},"
            f"adelay={int(body * 1000)}|{int(body * 1000)},"
            f"aformat=channel_layouts=stereo,{fit}[ctaa]"
        )
        next_input += 1
        branches.append("[ctaa]")

    if len(branches) > 1:
        # normalize=0 keeps the voice at unity; alimiter catches the sum clipping.
        chains.append(
            "".join(branches)
            + f"amix=inputs={len(branches)}:normalize=0:duration=first[amixed]"
        )
        chains.append("[amixed]alimiter=limit=0.97:level=disabled[a]")
        audio_label = "[a]"
    elif branches:
        audio_label = branches[0]
    for chain in chains:
        filtergraph += ";" + chain

    cmd += ["-filter_complex", filtergraph, "-map", "[v]"]
    if audio_label:
        cmd += ["-map", audio_label]
    cmd += [
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
        "-r", f"{fps}", "-g", f"{int(fps * 2)}",
    ]
    if audio_label:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
    cmd += ["-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]

    log(
        "rendering final 1080x1920 composite"
        + (f" ({body:.2f}s body + {cta_duration:.2f}s CTA)" if cta_file else "")
    )
    err = tempfile.TemporaryFile()
    # cwd = work dir so the ass sidecar resolves as a bare, unescaped name.
    encoder = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=err, cwd=str(work))
    border = {"mirror": cv2.BORDER_REFLECT_101}.get(edge, cv2.BORDER_REPLICATE)
    written = 0
    try:
        for cut, track in timeline:
            reader = FrameReader(head, cut.start, cut.duration + 0.2, fps, decode_w)
            n = len(track.scale)
            pane = None
            emitted = 0
            for frame in reader:
                if emitted >= cut.frames:
                    reader.close(check=False)
                    break
                k = min(emitted, n - 1) if n else 0
                s = track.scale[k] / decode_scale
                tx = OUT_W / 2.0 - s * track.cx[k] * decode_scale
                ty = face_y * HALF_H - s * track.cy[k] * decode_scale
                matrix = np.array([[s, 0.0, tx], [0.0, s, ty]], dtype=np.float64)
                interp = cv2.INTER_AREA if s < 1.0 else cv2.INTER_CUBIC
                if edge == "blur":
                    # Hold the lock past the frame edge by sitting the warped pane on
                    # a blurred, pane-filling copy of the same frame.
                    pane = blurred_cover(frame)
                    warped = cv2.warpAffine(
                        frame, matrix, (OUT_W, HALF_H), flags=interp,
                        borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0),
                    )
                    mask = cv2.warpAffine(
                        np.full(frame.shape[:2], 255, np.uint8), matrix, (OUT_W, HALF_H),
                        flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
                    )
                    np.copyto(pane, warped, where=mask[:, :, None].astype(bool))
                else:
                    pane = cv2.warpAffine(
                        frame, matrix, (OUT_W, HALF_H), flags=interp, borderMode=border,
                    )
                encoder.stdin.write(pane.tobytes())
                emitted += 1
                written += 1
            # Every cut must contribute exactly cut.frames, or the picture drifts out
            # of sync with the audio concat that used the same boundaries.
            while emitted < cut.frames and pane is not None:
                encoder.stdin.write(pane.tobytes())
                emitted += 1
                written += 1
    except BrokenPipeError:
        pass
    finally:
        if encoder.stdin:
            try:
                encoder.stdin.close()
            except BrokenPipeError:
                pass
        code = encoder.wait()
    if code != 0:
        err.seek(0)
        die(f"ffmpeg encode failed ({code}):\n{err.read().decode(errors='replace')[-2500:]}")
    err.close()
    log(f"warped {written} frames into the top pane")


# -------------------------------------------------------------------------- main


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sludge.py",
        description="Render sludge content: a head-locked talking head over a random clip, "
        "with word-highlighted captions synced to the speaker.",
    )
    p.add_argument("--head", required=True, type=Path, help="talking head video (drives audio + captions)")
    p.add_argument("--clip", required=True, type=Path, help="bottom filler clip")
    p.add_argument(
        "--music", "--audio", dest="music", type=Path,
        help="background music bed, ducked under the voice; looped if short",
    )
    p.add_argument(
        "--cta", type=Path,
        help="call-to-action video appended full-frame after the split-screen body",
    )
    p.add_argument("--message", default="", help="caption text; defaults to the transcript")
    p.add_argument("--out", type=Path, help="output mp4 (default sludge-<timestamp>.mp4)")

    t = p.add_argument_group("timing")
    t.add_argument("--start", type=float, default=0.0, help="start offset into the head video")
    t.add_argument("--duration", type=float, help="output duration (default: rest of head video)")
    t.add_argument("--fps", type=float, default=30.0)
    t.add_argument("--clip-offset", type=float, help="start offset into the bottom clip (default: random)")
    t.add_argument("--seed", type=int, help="seed for the random bottom-clip offset and framing cycle")

    e = p.add_argument_group("edit")
    e.add_argument(
        "--plan", action="store_true",
        help="print scored, timestamped transcript segments as JSON and exit — "
        "the input for choosing which parts to keep",
    )
    e.add_argument(
        "--target-duration", type=float, default=25.0,
        help="--plan: how long the suggested selection should add up to",
    )
    e.add_argument(
        "--edl", type=Path,
        help="edit decision list to render: JSON [{start, end, label?}, ...] in source "
        "seconds. Order is honoured, so cuts may be reordered",
    )
    e.add_argument(
        "--jump-cuts", action="store_true", default=True,
        help="cut silences longer than --max-gap out of every segment (default)",
    )
    e.add_argument("--no-jump-cuts", dest="jump_cuts", action="store_false")
    e.add_argument("--max-gap", type=float, default=0.35, help="longest silence to keep, in seconds")
    e.add_argument("--cut-pad", type=float, default=0.07, help="air left around each speech run")
    e.add_argument("--min-cut", type=float, default=0.4, help="drop segments shorter than this")
    e.add_argument(
        "--tail-pad", type=float, default=0.25,
        help="air kept after the last word of every cut, so it is never clipped",
    )
    e.add_argument(
        "--punch-in", action="store_true", default=True,
        help="vary the framing from cut to cut so the jump cuts read (default)",
    )
    e.add_argument("--no-punch-in", dest="punch_in", action="store_false")

    f = p.add_argument_group("head lock")
    f.add_argument(
        "--lock", choices=["tight", "smooth", "none"], default="tight",
        help="tight = pin the head at a fixed point and size (default); "
        "smooth = lazy pan at fixed framing; none = static crop",
    )
    f.add_argument(
        "--face-height", type=float, default=0.45,
        help="tight lock: face height as a fraction of the pane (bigger = closer)",
    )
    f.add_argument("--face-y", type=float, default=0.42, help="where the face sits in the pane, 0=top 1=bottom")
    f.add_argument(
        "--tightness", type=float, default=0.6,
        help="tight lock: 1.0 pins the head frame-for-frame, lower lets it float",
    )
    f.add_argument(
        "--scale-smooth", type=float,
        help="tight lock: zoom stability (default 0.2 with YuNet, 0.08 with Haar, whose "
        "box sizes are far noisier and need heavier filtering)",
    )
    f.add_argument(
        "--headroom", type=float, default=1.25,
        help="tight lock: minimum zoom past frame-fit, which is what buys room to pan "
        "(1.0 = none, and the lock breaks as soon as the head moves)",
    )
    f.add_argument(
        "--edge", choices=["blur", "clamp", "mirror"], default="blur",
        help="what happens when the lock wants pixels outside the frame: blur (default) "
        "holds the lock over a blurred fill; clamp keeps the window inside the frame and "
        "lets the lock loosen instead; mirror holds it and reflects",
    )
    f.add_argument("--zoom", type=float, default=1.25, help="smooth/none modes: crop tightness")
    f.add_argument("--smooth", type=float, default=0.12, help="smooth mode: pan smoothing")
    f.add_argument(
        "--detector", choices=["auto", "yunet", "haar"], default="auto",
        help="auto prefers YuNet (downloads a 230 KB model once) and falls back to Haar",
    )
    f.add_argument("--face-model", type=Path, help="path to a YuNet ONNX model, instead of downloading")
    f.add_argument("--preview-track", action="store_true", help="also write a face-detection preview mp4")
    f.add_argument(
        "--track-json", type=Path,
        help="use an external face track instead of detecting: "
        "JSON list of {frame|t, cx, cy, h} in source pixels",
    )

    c = p.add_argument_group("captions")
    c.add_argument(
        "--model", default="large-v3-turbo",
        help="whisper model: large-v3-turbo (default), medium, small, base, tiny, "
        "large-v3, and .en variants. Drop to small for a ~3x faster pass on easy audio; "
        "note the .en models measured worse than multilingual on hard proper nouns",
    )
    c.add_argument("--language", default="en", help="spoken language, or 'auto'")
    c.add_argument(
        "--vocab", default="",
        help="extra proper nouns, tickers or jargon to prime the transcriber with, "
        "on top of the message",
    )
    c.add_argument(
        "--no-prime", dest="prime", action="store_false", default=True,
        help="do not feed the message to whisper as a vocabulary hint",
    )
    c.add_argument("--font", default="Arial Black")
    c.add_argument("--font-size", type=int, default=86)
    c.add_argument("--caption-color", default="#FFFFFF")
    c.add_argument("--highlight-color", default="#FFF000")
    c.add_argument("--outline-color", default="#000000")
    c.add_argument("--outline", type=int, default=7)
    c.add_argument("--caption-pos", choices=["center", "top", "bottom"], default="center")
    c.add_argument("--caption-words", type=int, default=3, help="max words on screen at once")
    c.add_argument("--caption-chars", type=int, default=24, help="max characters on screen at once")
    c.add_argument("--caption-pop", type=int, default=12, help="%% scale bump on the active word")
    c.add_argument("--no-uppercase", action="store_true")
    c.add_argument("--no-captions", action="store_true")
    c.add_argument(
        "--caption-source", choices=["auto", "transcript", "message"], default="auto",
        help="auto (default) uses the message when it matches the audio and falls back "
        "to the transcript when it does not; transcript always captions what was said; "
        "message forces the message text even if it cannot be word-synced",
    )
    c.add_argument(
        "--caption-offset", type=float, default=0.0,
        help="shift every caption by this many seconds (+ = later)",
    )
    c.add_argument(
        "--no-refine", action="store_true",
        help="skip snapping word starts to measured speech onsets",
    )

    m = p.add_argument_group("emphasis")
    m.add_argument(
        "--emphasize", default="",
        help="comma-separated words to emphasise; *asterisks* in the message work too",
    )
    m.add_argument(
        "--no-auto-emphasis", dest="auto_emphasis", action="store_false", default=True,
        help="stop auto-emphasising numbers, money and percentages",
    )
    m.add_argument("--emphasis-scale", type=float, default=1.45, help="size of a solo word")
    m.add_argument("--no-shake", dest="shake", action="store_false", default=True)
    m.add_argument("--shake-px", type=float, default=16.0, help="shake amplitude in pixels")
    m.add_argument("--shake-hz", type=float, default=22.0, help="shake rate")

    v = p.add_argument_group("voice")
    v.add_argument(
        "--voice-polish", action="store_true", default=True,
        help="EQ, compress and loudness-normalise the head's audio so the voice "
        "carries on a phone speaker (default)",
    )
    v.add_argument(
        "--no-voice-polish", dest="voice_polish", action="store_false",
        help="use the head's audio exactly as recorded",
    )
    v.add_argument(
        "--voice-lufs", type=float, default=-14.0,
        help="loudness target for the voice, in LUFS. -14 is the streaming standard; "
        "the bed and clip mix in above it, so the finished file sits a little hotter",
    )

    a = p.add_argument_group("music bed")
    a.add_argument(
        "--music-volume", "--audio-volume", dest="music_volume", type=float, default=0.45,
        help="bed level before ducking",
    )
    a.add_argument(
        "--duck", action="store_true", default=True,
        help="duck the bed with a voice-keyed sidechain compressor (default)",
    )
    a.add_argument(
        "--no-duck", dest="duck", action="store_false",
        help="hold the bed at a fixed level instead",
    )
    a.add_argument("--duck-ratio", type=float, default=9.0, help="how hard to duck, 1-20")
    a.add_argument(
        "--duck-threshold", type=float, default=0.05,
        help="voice level that starts the ducking, 0.001-1 (lower = more sensitive)",
    )
    a.add_argument(
        "--music-offset", "--audio-offset", dest="music_offset", type=float,
        help="start offset into the bed (default: random, or chosen to sync the drop)",
    )
    a.add_argument(
        "--music-fade", "--audio-fade", dest="music_fade", type=float, default=0.6,
        help="bed fade in/out, in seconds",
    )
    a.add_argument(
        "--sync-drop", action="store_true", default=True,
        help="with --cta: line the bed's drop up with the CTA cut (default)",
    )
    a.add_argument("--no-sync-drop", dest="sync_drop", action="store_false")
    a.add_argument(
        "--drop-at", type=float,
        help="the drop's time in the bed, in seconds, instead of detecting it",
    )
    a.add_argument(
        "--beat-match", action="store_true", default=True,
        help="with --cta: land the CTA cut on a beat of the music (default)",
    )
    a.add_argument("--no-beat-match", dest="beat_match", action="store_false")
    a.add_argument("--bpm", type=float, help="the bed's tempo, instead of detecting it")

    h = p.add_argument_group("tail echo")
    h.add_argument(
        "--echo", action="store_true", default=None,
        help="beat-matched delay on the last few words, ringing out over the CTA "
        "(on by default when there is music)",
    )
    h.add_argument("--no-echo", dest="echo", action="store_false")
    h.add_argument("--echo-words", type=int, default=3, help="how many closing words get it")
    h.add_argument(
        "--echo-note", choices=["1/2", "1/4", "1/8.", "1/8", "1/16"], default="1/8",
        help="delay length as a note value at the bed's tempo",
    )
    h.add_argument("--echo-repeats", type=int, default=4, help="number of repeats")
    h.add_argument("--echo-decay", type=float, default=0.62, help="how fast repeats fade, 0-1")
    h.add_argument("--echo-mix", type=float, default=0.6, help="level of the first repeat")

    x = p.add_argument_group("call to action")
    x.add_argument("--cta-duration", type=float, help="trim the CTA to this many seconds")
    x.add_argument(
        "--cta-volume", type=float, default=1.0,
        help="level for the CTA's own audio (0 drops it)",
    )

    o = p.add_argument_group("output")
    o.add_argument("--seam", type=int, default=0, help="divider line thickness in px (0 = none)")
    o.add_argument(
        "--clip-volume", type=float,
        help="bottom clip audio level before ducking. Default: with a bed, whatever "
        "lands the clip on the ducked bed's level while the voice is running; "
        f"without one, {CLIP_VOLUME_SOLO:g}",
    )
    o.add_argument(
        "--clip-duck-ratio", type=float, default=3.0,
        help="how hard the clip's audio ducks under the voice. Deliberately gentler "
        "than the music's --duck-ratio so the bottom pane stays audible; 1 = no ducking",
    )
    o.add_argument(
        "--mute-clip", action="store_true", default=False,
        help="drop the clip's audio entirely; by default it is kept and ducked "
        "under the voice along with the music bed",
    )
    o.add_argument("--keep-clip-audio", dest="mute_clip", action="store_false")
    o.add_argument("--crf", type=int, default=19)
    o.add_argument("--preset", default="medium")
    o.add_argument("--keep-work", action="store_true", help="keep intermediates for debugging")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"{tool} not found — install it with `brew install ffmpeg`")

    # Everything downstream is absolute: the final render runs with cwd set to the
    # work directory so filtergraph sidecars need no escaping.
    head = probe(args.head.expanduser().resolve())
    clip = probe(args.clip.expanduser().resolve())
    log(f"head {head.width}x{head.height} {head.fps:.2f}fps {head.duration:.2f}s audio={head.has_audio}")
    log(f"clip {clip.width}x{clip.height} {clip.fps:.2f}fps {clip.duration:.2f}s audio={clip.has_audio}")
    if head.has_audio:
        log(
            f"voice: EQ + 2-stage compression, normalised to {args.voice_lufs:g} LUFS"
            if args.voice_polish
            else "voice: using the head's audio as recorded (--no-voice-polish)"
        )
    music = args.music.expanduser().resolve() if args.music else None
    music_duration = 0.0
    if music:
        music_duration = probe_audio(music)
        log(f"bed  {music.name} {music_duration:.2f}s")

    # The clip and the bed are two different kinds of background and the ear reads
    # them as one layer, so a mismatch between them just sounds like a mistake. With
    # a bed to match, the clip's level is solved for rather than fixed: it is set to
    # whatever puts it on the ducked bed while the voice is running, which is nearly
    # the whole body once the jump cuts are in.
    clip_volume = args.clip_volume
    matched = False
    if clip_volume is None:
        if music:
            key_lufs = args.voice_lufs
            if not args.voice_polish and head.has_audio:
                # No loudness pass to pin the voice, so the overshoot is whatever
                # this particular recording happens to be. Measure it.
                key_lufs = measure_lufs(head.path) or args.voice_lufs
            clip_volume = matched_clip_volume(
                args.music_volume,
                args.duck_ratio if args.duck else 1.0,
                args.clip_duck_ratio if args.duck else 1.0,
                args.duck_threshold,
                key_lufs,
            )
            matched = True
        else:
            clip_volume = CLIP_VOLUME_SOLO
    if not clip.has_audio:
        log(
            "clip has NO audio stream — there is nothing from the bottom pane to hear. "
            "Use a clip with sound, or pass --music for a bed."
        )
    elif args.mute_clip:
        log("clip audio muted by --mute-clip")
    else:
        log(
            f"clip audio: on at {clip_volume:.3g}"
            + (
                f" (matched to the bed's ducked level, {args.music_volume:g} at "
                f"ratio {args.duck_ratio:g})"
                if matched and args.duck
                else " (matched to the bed)"
                if matched
                else ""
            )
            + (
                f", ducked gently under the voice (ratio {args.clip_duck_ratio:g})"
                if args.duck
                else ", not ducked"
            )
        )
    cta = probe(args.cta.expanduser().resolve()) if args.cta else None
    if cta:
        log(f"cta  {cta.path.name} {cta.width}x{cta.height} {cta.duration:.2f}s audio={cta.has_audio}")

    start = max(0.0, min(args.start, max(head.duration - 0.5, 0.0)))
    duration = args.duration if args.duration else head.duration - start
    duration = max(0.5, min(duration, head.duration - start))

    out = args.out or Path(f"sludge-{time.strftime('%Y%m%d-%H%M%S')}.mp4")
    out = out.expanduser().absolute()
    out.parent.mkdir(parents=True, exist_ok=True)

    work_root = Path(tempfile.mkdtemp(prefix="sludge-"))
    cache = Path.home() / ".cache" / "b-sludge"
    try:
        # Word timings drive three things: caption highlighting, silence-based jump
        # cuts, and segment scoring — so transcribe whenever any of them is in play.
        needs_words = not args.no_captions or args.jump_cuts or args.plan
        prompt = (
            build_prompt(strip_emphasis(args.message)[0], args.vocab) if args.prime else ""
        )
        spoken = (
            transcribe(head.path, work_root, cache, args.model, args.language, prompt)
            if needs_words and head.has_audio
            else []
        )
        if spoken and not args.no_refine:
            # Whisper's starts run early; put them back on the real onsets before any
            # of this is used for captions or for cutting.
            spoken = refine_timings(spoken, voice_wav(head.path, work_root))

        if args.plan:
            if not spoken:
                die("--plan needs speech in the head video to have anything to plan with")
            print(json.dumps(build_plan(spoken, args.message, args.target_duration), indent=2))
            return 0

        selected = (
            parse_edl(args.edl, head.duration)
            if args.edl
            else [(start, start + duration, "")]
        )
        span = sum(e - s for s, e, _ in selected)
        ranges = (
            apply_jump_cuts(selected, spoken, args.max_gap, args.cut_pad, args.min_cut)
            if args.jump_cuts
            else selected
        )
        cuts = build_cuts(ranges, args.fps, args.punch_in, args.seed)
        protected_end = protect_tail(
            cuts, spoken, args.fps, args.tail_pad, head.duration
        )

        detector = "none"
        finder = None
        if args.lock != "none" and not args.track_json:
            detect_h = int(round(DETECT_W * head.height / head.width)) // 2 * 2
            finder = build_finder(args.detector, cache, DETECT_W, detect_h, args.face_model)
            detector = finder.name
        if args.scale_smooth is None:
            args.scale_smooth = 0.08 if detector == "haar" else 0.2

        timeline: list[tuple[Cut, LockTrack]] = []
        for i, cut in enumerate(cuts):
            if args.lock == "none":
                samples: list[Sample | None] = [None] * cut.frames
            elif args.track_json:
                samples, _ = load_track_json(args.track_json, head, args.fps, cut.frames)
            else:
                preview = (
                    out.with_suffix(f".track{i if len(cuts) > 1 else ''}.mp4")
                    if args.preview_track
                    else None
                )
                samples, counted = track_faces(
                    head, cut.start, cut.duration, args.fps, finder, preview
                )
                if counted:
                    cut.frames = counted
            timeline.append((
                cut,
                build_lock(
                    samples, head, cut.frames,
                    mode=args.lock, zoom=args.zoom,
                    # Each cut gets its own framing so consecutive cuts differ.
                    face_height=args.face_height * cut.framing,
                    face_y=args.face_y, tightness=args.tightness,
                    scale_smooth=args.scale_smooth, pan_smooth=args.smooth,
                    headroom=args.headroom, edge=args.edge,
                ),
            ))
        # Detection may have adjusted frame counts, so the body length is only final
        # here. Everything timed against it — the clip offset, and especially the
        # drop-to-CTA alignment — has to be computed after this point, not before.
        duration = sum(c.frames for c, _ in timeline) / args.fps
        log(
            f"timeline: {len(cuts)} cut(s), {duration:.2f}s from {span:.2f}s selected "
            f"of {head.duration:.2f}s source"
            + (f"; framings {sorted({c.framing for c in cuts})}" if args.punch_in else "")
        )

        rng = random.Random(args.seed)
        if args.clip_offset is not None:
            clip_offset = max(0.0, args.clip_offset)
        elif clip.duration > duration + 1.0:
            clip_offset = rng.uniform(0.0, clip.duration - duration)
        else:
            clip_offset = 0.0
        music_delay = 0.0
        music_offset = 0.0
        tempo: tuple[float, float] | None = None
        if music and cta and (args.beat_match or args.sync_drop):
            if args.bpm:
                tempo = (args.bpm, 0.0)
            elif args.beat_match:
                tempo = detect_tempo(music, work_root)

            drop = None
            if args.sync_drop:
                drop = args.drop_at if args.drop_at is not None else find_drop(music, work_root)
                if drop is not None and tempo:
                    # A drop lands on a beat in real music; nudge the detected time
                    # onto the grid so the whole alignment inherits the grid.
                    snapped = snap_to_grid(drop, *tempo)
                    if abs(snapped - drop) > 0.001:
                        log(f"drop snapped to the beat grid: {drop:.3f}s -> {snapped:.3f}s")
                    drop = max(snapped, 0.0)

            if tempo:
                # Move the CTA cut onto a beat by trimming or extending the last cut.
                # Half a beat of movement at most, and shrinking is preferred over
                # growing because the source may simply not have more frames.
                bpm, phase = tempo
                beat = 60.0 / bpm
                grid_phase = phase - (drop - phase) % beat if drop is not None else phase
                last, last_track = timeline[-1]
                room_up = max(len(last_track.scale) - last.frames, 0) + 6
                # The nearest beat is within half a beat by definition; the beat before
                # it is the fallback when moving forward would need frames we don't have.
                nearest = snap_to_grid(duration, bpm, grid_phase % beat)
                options = [nearest, nearest - beat]
                delta_frames = None
                for candidate in options:
                    delta = int(round((candidate - duration) * args.fps))
                    if delta > room_up or last.frames + delta < 2:
                        continue
                    if abs(delta) > int(beat * args.fps / 2) + 1:
                        continue
                    # Never shorten past the tail guard — landing on a beat is not
                    # worth clipping the closing word.
                    if delta < 0 and last.start + (last.frames + delta) / args.fps < protected_end:
                        continue
                    delta_frames = delta
                    break
                if delta_frames is not None:
                    last.frames += delta_frames
                    duration = sum(c.frames for c, _ in timeline) / args.fps
                    log(
                        f"beat match: CTA cut moved {delta_frames:+d} frame(s) to land on "
                        f"a beat at {duration:.3f}s ({bpm:.1f} BPM)"
                    )
                else:
                    log("beat match: no usable beat within half a beat of the CTA cut")

            if drop is not None:
                if drop >= duration:
                    music_offset = drop - duration
                else:
                    music_delay = duration - drop
                log(
                    f"syncing the drop to the CTA cut at {duration:.3f}s "
                    f"(bed offset {music_offset:.2f}s, delay {music_delay:.2f}s)"
                )

        if args.music_offset is not None:
            music_offset, music_delay = max(0.0, args.music_offset), 0.0
        elif not (music and cta) and music_duration > duration + 1.0:
            music_offset = rng.uniform(0.0, music_duration - duration)
        if music:
            how = (
                f"ducked (ratio {args.duck_ratio:g})"
                + (", full level under the CTA" if cta else "")
                if args.duck and head.has_audio
                else "at a fixed level"
            )
            if args.duck and not head.has_audio:
                log("no voice to key the ducking — holding the bed at a fixed level")
            log(f"music bed @ {music_offset:.2f}s, volume {args.music_volume:g}, {how}")

        if tempo is None and args.bpm:
            tempo = (args.bpm, 0.0)
        elif tempo is None and music and args.echo is not False:
            tempo = detect_tempo(music, work_root)

        assfile = work_root / "captions.ass"
        caption_words: list[Word] = []
        if args.no_captions:
            assfile.write_text(
                f"[Script Info]\nScriptType: v4.00+\nPlayResX: {OUT_W}\nPlayResY: {OUT_H}\n\n"
                "[V4+ Styles]\nFormat: Name\n\n[Events]\nFormat: Layer, Start, End, Text\n"
            )
        else:
            # Captions are timed on the cut timeline, not the source, so highlights
            # stay on the speaker across every jump cut and reorder.
            heard = remap_words(spoken, [c for c, _ in timeline])
            clean_message, marked = strip_emphasis(args.message)
            marked |= {norm(t) for t in args.emphasize.split(",") if norm(t)}
            if args.caption_source == "transcript" or not clean_message.strip():
                words = heard
            else:
                words = align_message(
                    clean_message, heard, duration,
                    force=args.caption_source == "message",
                )
            words = enforce_min_step(words)
            words = mark_emphasis(words, marked, args.auto_emphasis)
            if args.caption_offset:
                for w in words:
                    w.start += args.caption_offset
                    w.end += args.caption_offset
                words = [w for w in words if w.end > 0]
                for w in words:
                    w.start = max(w.start, 0.0)
            if not words:
                log("no caption words — rendering without captions")
            caption_words = words
            write_ass(
                words, assfile, duration,
                font=args.font, size=args.font_size,
                base_color=args.caption_color, highlight=args.highlight_color,
                outline_color=args.outline_color, outline=args.outline,
                position=args.caption_pos, uppercase=not args.no_uppercase,
                max_words=args.caption_words, max_chars=args.caption_chars,
                pop=args.caption_pop,
                emphasis_scale=args.emphasis_scale, shake=args.shake,
                shake_px=args.shake_px, shake_hz=args.shake_hz, seed=args.seed,
            )

        # Tail echo: delay the closing words so they ring out over the cut. The delay
        # is a note value at the bed's tempo, so the repeats land on the beat grid the
        # CTA cut was just aligned to.
        echo_on = args.echo if args.echo is not None else bool(music)
        echo_start: float | None = None
        echo_delay = 0.0
        if echo_on and head.has_audio:
            note = {"1/2": 2.0, "1/4": 1.0, "1/8.": 0.75, "1/8": 0.5, "1/16": 0.25}[
                args.echo_note
            ]
            if tempo:
                beat = 60.0 / tempo[0]
                echo_delay = beat * note
            else:
                echo_delay = 0.3 * (note / 0.5)
                log("no tempo for the echo — using a fixed delay")
            tail = [w for w in caption_words if w.start < duration]
            if tail:
                echo_start = tail[max(len(tail) - args.echo_words, 0)].start
            else:
                echo_start = max(duration - 1.2, 0.0)
            if tempo:
                # Start the effect on a beat, counting back from the beat-aligned cut.
                beat = 60.0 / tempo[0]
                k = max(round((duration - echo_start) / beat), 1)
                echo_start = max(duration - k * beat, 0.0)
            # Snapping must never land on (or past) the end of the voice, or the tail
            # would be empty and the effect would silently do nothing.
            echo_start = min(echo_start, max(duration - 0.15, 0.0))
            words_note = (
                f"last {min(args.echo_words, len(tail))} word(s)" if tail else "last 1.2s"
            )
            last_word_end = max((w.end for w in tail), default=duration)
            rings_to = last_word_end + echo_delay * args.echo_repeats
            log(
                f"tail echo: {words_note} from {echo_start:.3f}s, "
                f"{args.echo_note} = {echo_delay * 1000:.0f}ms x{args.echo_repeats}, "
                f"ringing to {rings_to:.2f}s"
                + (
                    f" ({rings_to - duration:.2f}s over the CTA)"
                    if cta and rings_to > duration
                    else ""
                )
            )

        render(
            head, clip, out, work_root, timeline,
            duration=duration, assfile=assfile, fps=args.fps,
            face_y=args.face_y, edge=args.edge,
            clip_offset=clip_offset, clip_mute=args.mute_clip,
            clip_volume=clip_volume,
            clip_duck_ratio=args.clip_duck_ratio if args.duck else 1.0,
            voice_polish=args.voice_polish, voice_lufs=args.voice_lufs,
            music=music, music_offset=music_offset, music_delay=music_delay,
            music_volume=args.music_volume, music_fade=args.music_fade,
            duck_ratio=args.duck_ratio if args.duck else 1.0,
            duck_threshold=args.duck_threshold,
            cta=cta, cta_limit=args.cta_duration, cta_volume=args.cta_volume,
            echo_start=echo_start, echo_delay=echo_delay,
            echo_repeats=args.echo_repeats, echo_decay=args.echo_decay,
            echo_mix=args.echo_mix,
            seam=args.seam, crf=args.crf, preset=args.preset,
        )
    finally:
        if args.keep_work:
            log(f"intermediates kept in {work_root}")
        else:
            shutil.rmtree(work_root, ignore_errors=True)

    result = probe(out)
    log(f"done: {out} — {result.width}x{result.height} {result.duration:.2f}s "
        f"({out.stat().st_size / 1e6:.1f} MB)")
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
