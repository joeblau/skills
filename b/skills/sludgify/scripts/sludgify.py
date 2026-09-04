#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["opencv-python-headless>=4.10,<5", "numpy>=1.26,<2.3"]
# ///
"""Mine a long video for sludge-able spans, score them against the house corpus,
and hand the survivors to sludge.py.

    ./sludgify.py "https://youtu.be/..." --plan
    ./sludgify.py podcast.mp4 --render --clip filler.mp4 --music bed.mp3 --cta cta.mp4

sludgify decides WHAT to clip; sludge renders it. Everything it knows about what
"clippable" means comes from ../corpus-profile.json — the measured profile of 76
hand-picked clips. Change the profile, change the taste; this script holds no
editorial opinions of its own.

The pipeline is a cache, not a program: every stage writes one file into
~/.cache/b-sludgify/<source-key>/ and is skipped when that file already exists, so
a re-run with different mining knobs costs seconds, not minutes.

    meta.json        resolved url/title/duration
    source.mp4       the download (or a symlink to a local file)
    audio.wav        16 kHz mono
    transcript.json  normalised {segments:[{start,end,text}]}
    cuts.json        scene-cut times + scores, for span barriers and shot safety
    candidates.json  the last emitted result
    clips/NN-slug.mp4  extracted spans, ready for sludge.py

WHAT THIS CANNOT DO, stated plainly: the shot probe proves the CAMERA did not
change. It cannot prove the RIGHT PERSON is talking. A locked 2-shot where the host
speaks over the guest passes every check — that is exactly the failure that once
shipped 24 seconds of the wrong man's face. Read the frame strip before you ship.

Run `./sludgify.py --help` for the full option list.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import textwrap
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = SKILL_DIR / "corpus-profile.json"
DEFAULT_SLUDGE = Path.home() / ".claude" / "skills" / "sludge" / "scripts" / "sludge.py"

CACHE = Path.home() / ".cache" / "b-sludgify"
MODEL_DIR = CACHE / "models"
SLUDGE_CACHE = Path.home() / ".cache" / "b-sludge"  # shared with sludge.py — same YuNet weights
YUNET_NAME = "face_detection_yunet_2023mar.onnx"
YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/"
    "face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
GGML_BASE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
SUPERWHISPER = Path.home() / "Library" / "Application Support" / "superwhisper"

# yt-dlp's DEFAULT clients are the happy path. Measured today, forcing a client list
# triggers DRM/SABR skips on YouTube and collapses the DASH ladder to HLS-only. The
# ladder below is walked only on failure, and is deliberately ordered least-exotic
# first. Expect this to rot: YouTube ships extractor-hostile experiments constantly.
DOWNLOAD_FALLBACKS = [
    [],
    ["--extractor-args", "youtube:player_client=default,web_safari,tv"],
    ["--extractor-args", "youtube:player_client=web_safari,tv,web"],
]
# The head pane is 1080x960, so 1080p is the ceiling and 4K is wasted bandwidth.
# avc1 first because OpenCV and the head-lock decode path handle H.264 fastest.
DEFAULT_FORMAT = "bv*[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]"

# scene-score calibration, measured against a ground-truth multicam:
#   punch-in reframe / jump cut   0.105 - 0.268
#   real camera cut / speaker chg 0.931 - 1.000
# 0.4 sits in the two-orders gap. Crossfades ramp slowly and will be MISSED — the
# scan keeps every score above SCAN_FLOOR so the threshold can be re-fit per source.
SCENE_THRESHOLD = 0.4
SCAN_FLOOR = 0.1

TERMINAL_RE = re.compile(r"[.!?]['\"”]?\s*$")
# The profile's `interviewer` veto catches stock question stems. A real podcast opens
# a turn a hundred other ways, and a span that starts on the HOST is the single most
# expensive mistake this tool can make — the speaker is wrong before a frame renders.
# So this is a second, structural net: an interrogative aimed at "you" in the opening
# sentence. It is a FLAG, never a drop; a speaker restating his own question is a
# shipped corpus opening (3/76) and only a human can tell the two apart.
HOST_TURN_RE = re.compile(
    r"^\W*(?:(?:i|we)\s+(?:want|have|need|had|wanna|gotta)\s+to\s+ask\b"
    r"|let\s+me\s+ask\b|my\s+question\b|(?:so\s+|and\s+|but\s+)?"
    r"(?:what|how|why|when|where|which|who|do|does|did|would|could|can|are|is|was|have|has|will)"
    r"\s+(?:do\s+)?you(?:r|'ve|'d|'ll)?\b)",
    re.I,
)
DANGLING_RE = re.compile(r"^\W*(and|but|so|because|which|that|or|then|also|plus|cause)\b", re.I)
WORD_RE = re.compile(r"[a-z0-9'$%&]+")
# Mirrors build_profile.score_span. These two live in the scorer, not the profile
# JSON, because they are grammar, not taste — keep them identical or --self-test
# will tell you the two scorers have drifted apart.
COPULAR_RE = re.compile(r"\b(is|are|'s|'re|was|were)\b [^.?!]{6,}")
MID_CLAUSE_END_RE = re.compile(r"\b(and|but|so|because|that|the|a|to|of|with|it's|like)\s*$")

STOPWORDS = {
    "a", "about", "all", "an", "and", "are", "as", "at", "be", "been", "but", "by", "can",
    "do", "does", "for", "from", "get", "go", "had", "has", "have", "he", "her", "him",
    "his", "how", "i", "if", "in", "into", "is", "it", "its", "just", "like", "me", "my",
    "not", "of", "on", "or", "our", "out", "she", "so", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "to", "up", "us", "was", "we",
    "were", "what", "when", "which", "who", "will", "with", "you", "your", "am", "very",
    "really", "gonna", "going", "kind", "sort", "know", "mean", "yeah", "okay", "well",
}
# A slug may open on a pronoun — `i-just-want-raw-pnl`, `my-mom-could-understand` are
# the user's own — but never on an article, preposition or copula.
BAD_OPENERS = {
    "a", "an", "the", "of", "to", "in", "on", "at", "for", "and", "but", "or", "so",
    "that", "which", "is", "are", "was", "were", "be", "been", "as", "than", "with",
    "from", "by", "about", "into", "just", "like", "then", "there",
}

VERBOSE = True


# --------------------------------------------------------------------------- shell


def log(msg: str) -> None:
    if VERBOSE:
        print(f"[sludgify] {msg}", file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(f"[sludgify] warning: {msg}", file=sys.stderr, flush=True)


def die(msg: str) -> "None":
    print(f"[sludgify] error: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def run(cmd: list[str], quiet: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if quiet else sys.stderr,
        stderr=subprocess.PIPE if quiet else sys.stderr,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if check and proc.returncode != 0:
        tail = (proc.stderr or "")[-2500:]
        die(f"command failed ({proc.returncode}): {' '.join(cmd[:4])} ...\n{tail}")
    return proc


def need(tool: str, hint: str = "") -> str:
    path = shutil.which(tool)
    if not path:
        die(f"{tool} not found{(' — ' + hint) if hint else ''}")
    return path


def ffprobe_duration(path: Path) -> float:
    out = run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(path),
    ]).stdout.strip()
    try:
        return float(out)
    except ValueError:
        die(f"could not read a duration from {path}")
        return 0.0


def slugify(text: str, max_words: int = 5, max_chars: int = 38) -> str:
    # apostrophes close up rather than split, the way the user's own slugs do:
    # `if-youre-wrong-youre-crushed`, `users-dont-care-about-decentralization`
    text = text.lower().replace("'", "").replace("’", "")
    words = [w for w in re.findall(r"[a-z0-9]+", text) if w][:max_words]
    slug = "-".join(words)
    while len(slug) > max_chars and "-" in slug:
        slug = slug.rsplit("-", 1)[0]
    return slug or "clip"


# --------------------------------------------------------------------------- profile


class Profile:
    """corpus-profile.json, compiled.

    This is a faithful port of build_profile.score_span, driven entirely by the JSON
    instead of the constants in that script — so regenerating the profile changes the
    scoring without touching this file. `--self-test` re-scores the corpus and diffs
    against the profile's own per_clip_scores, which is what keeps the port honest.
    """

    def __init__(self, path: Path):
        if not path.exists():
            die(f"corpus profile not found: {path}\n"
                f"          build it with scripts/build_profile.py --corpus <dir> --manifest <corpus.json>")
        self.path = path
        self.data = json.loads(path.read_text())
        s = self.data["scoring"]
        self.weights = s["weights"]
        self.themes = self.data["themes"]
        self.stance = {k: [re.compile(p) for p in v] for k, v in s["stance_patterns"].items()}
        self.entities = s["entities"]
        self.num_re = re.compile(s["number_regex"])
        self.filler_re = re.compile(s["filler_regex"])
        self.you_re = re.compile(s["second_person_regex"])
        self.claim_res = [re.compile(p) for p in s["claim_evidence_regexes"]]
        self.auto_emphasis_re = re.compile(s["auto_emphasis_regex"])
        self.vetoes = {
            r["id"]: (re.compile(r["pattern"]), r["severity"])
            for r in self.data["reject_list"] if r.get("pattern")
        }
        self.veto_mult = s["veto_multipliers"]
        self.gates = s["gates"]
        self.thresholds = s["thresholds"]
        self.editorial = self.data["editorial"]
        self.sludge_invocation = self.data.get("sludge_invocation", {})
        # the union lexicons the off_topic gate scores against
        self.all_core = sorted({t for lx in self.themes.values() for t in lx["core"]})
        self.all_support = sorted({t for lx in self.themes.values() for t in lx["support"]})

    @property
    def version(self) -> str:
        return self.data.get("profile_version", "?")

    @property
    def fingerprint(self) -> str:
        return self.data.get("provenance", {}).get("corpus_fingerprint", "?")

    # -- scoring ------------------------------------------------------------

    @staticmethod
    def _term_hits(text: str, terms: list[str]) -> tuple[int, list[str]]:
        n, found = 0, []
        for t in terms:
            c = text.count(t) if " " in t else len(re.findall(r"\b" + re.escape(t) + r"\b", text))
            if c:
                n += c
                found.append(t)
        return n, found

    @staticmethod
    def _rx_hits(text: str, pats: list[re.Pattern]) -> int:
        return sum(len(p.findall(text)) for p in pats)

    def duration_points(self, dur: float) -> float:
        d = self.weights["duration_fit"]
        for band in d["bands"]:
            lo, hi = band["range_s"]
            if lo <= dur <= hi:
                return float(band["pts"])
        return float(d.get("else_pts", 0.0))

    def score_span(self, text: str, dur: float | None = None) -> tuple[float, float, str, dict]:
        """Return (score, base, primary_theme, debug). Plain regex, no models."""
        t = re.sub(r"\s+", " ", text.lower()).strip()
        W = max(len(WORD_RE.findall(t)), 1)
        dur = dur or W / 3.13
        R = 100.0 / W
        dbg: dict = {}

        tf = self.weights["theme_fit"]
        pts = {}
        for name, lex in self.themes.items():
            c, _ = self._term_hits(t, lex["core"])
            sp, _ = self._term_hits(t, lex["support"])
            raw = tf["core_weight"] * c + tf["support_weight"] * sp
            pts[name] = min(raw * R * tf["per_100_words_scale"], tf["per_theme_cap"])
        # Ties are common on short spans. Break by curated rank so the primary_theme
        # LABEL — and therefore the per-video diversity cap — never depends on dict order.
        ranked = sorted(pts.items(), key=lambda kv: (-kv[1], self.themes[kv[0]]["rank"]))
        theme_fit = min(ranked[0][1] + tf["second_theme_bonus"] * ranked[1][1], float(tf["max"]))
        dbg["themes"] = [[n, round(v, 1)] for n, v in ranked[:3]]

        rev = self._rx_hits(t, self.stance["reversal"])
        nc = self._rx_hits(t, self.stance["named_contrast"])
        dis = self._rx_hits(t, self.stance["dismissal"])
        con = self._rx_hits(t, self.stance["confession"])
        stance = min(6.0 * min(rev, 2) + 3.0 * min(nc, 2) + 3.0 * min(dis, 2) + 4.0 * min(con, 1), 20.0)
        dbg["stance"] = {"reversal": rev, "contrast": nc, "dismissal": dis, "confession": con}

        you = len(self.you_re.findall(t))
        imp = self._rx_hits(t, self.stance["imperative"])
        addr = min(you * R * 1.6, 8.0) + min(2.0 * imp, 4.0)
        dbg["address"] = {"you": you, "imperative": imp}

        nums = len(self.num_re.findall(t))
        ents, entf = self._term_hits(t, self.entities)
        conc = min(2.0 * nums, 6.0) + min(2.0 * ents, 6.0)
        dbg["concrete"] = {"numbers": nums, "entities": entf}

        pr = self._rx_hits(t, self.stance["prediction"])
        pg = self._rx_hits(t, self.stance["progression"])
        mc = self._rx_hits(t, self.stance["mechanism"])
        shape = (3.0 if pr else 0.0) + (3.0 if pg else 0.0) + (3.0 if mc else 0.0)
        if COPULAR_RE.search(t):
            shape += 1.5
        if not MID_CLAUSE_END_RE.search(t):
            shape += 1.5
        shape = min(shape, 12.0)
        dbg["shape"] = {"prediction": pr, "progression": pg, "mechanism": mc}

        wps = W / dur if dur else 3.13
        d = 8.0
        if wps < 2.0 or wps > 4.4:
            d -= 4.0
        elif wps < 2.4 or wps > 4.1:
            d -= 1.5
        fill = len(self.filler_re.findall(t)) * R
        if fill > 12:
            d -= 3.0
        elif fill > 8:
            d -= 1.5
        delivery = max(d, 0.0)
        dbg["wps"] = round(wps, 2)
        dbg["filler_per_100w"] = round(fill, 1)
        dbg["words"] = W

        dur_pts = self.duration_points(dur)
        base = theme_fit + stance + addr + conc + shape + delivery + dur_pts

        vetoes = [k for k, (rx, _sev) in self.vetoes.items() if rx.search(t)]
        mult = 1.0
        for v in vetoes:
            mult *= self.veto_mult["hard" if self.vetoes[v][1] == "hard" else "soft"]
        dbg["veto"] = vetoes

        gates = []
        dc = sum(1 for x in self.all_core
                 if (x in t if " " in x else re.search(r"\b" + re.escape(x) + r"\b", t)))
        ds = sum(1 for x in self.all_support
                 if (x in t if " " in x else re.search(r"\b" + re.escape(x) + r"\b", t)))
        if not (dc >= 2 or (dc >= 1 and ds >= 3)):
            mult *= self.gates["off_topic"]["multiplier"]
            gates.append("off_topic")
        claim = sum(len(p.findall(t)) for p in self.claim_res)
        claim += rev + nc + dis + con + pr + pg + mc + imp
        if claim == 0:
            mult *= self.gates["no_claim"]["multiplier"]
            gates.append("no_claim")
        dbg["gates"] = gates
        dbg["core_terms"] = dc
        dbg["support_terms"] = ds
        dbg["claim_evidence"] = claim

        return round(base * mult, 1), round(base, 1), ranked[0][0], dbg

    def band(self, score: float) -> str:
        th = self.thresholds
        if score < th["reject"]["below"]:
            return "reject"
        if score < th["shortlist"]["to"]:
            return "shortlist"
        if score < th["strong"]["to"]:
            return "strong"
        return "flagship"


# --------------------------------------------------------------------------- workspace


@dataclass
class Workspace:
    root: Path
    key: str

    @property
    def meta(self) -> Path: return self.root / "meta.json"

    @property
    def source(self) -> Path: return self.root / "source.mp4"

    @property
    def audio(self) -> Path: return self.root / "audio.wav"

    @property
    def transcript(self) -> Path: return self.root / "transcript.json"

    @property
    def cuts(self) -> Path: return self.root / "cuts.json"

    @property
    def candidates(self) -> Path: return self.root / "candidates.json"

    @property
    def clips(self) -> Path: return self.root / "clips"

    @property
    def verify(self) -> Path: return self.root / "verify"


def source_key(source: str) -> tuple[str, bool]:
    """(cache key, is_url). Local files key on IDENTITY, not path, so moving a file
    still hits the cache and re-downloading is never triggered by a rename."""
    if re.match(r"^[a-z][a-z0-9+.-]*://", source, re.I):
        return hashlib.sha1(source.encode()).hexdigest()[:16], True
    p = Path(source).expanduser()
    if not p.exists():
        die(f"source not found and not a URL: {source}")
    st = p.resolve().stat()
    ident = f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(ident.encode()).hexdigest()[:16], False


# --------------------------------------------------------------------------- stage 1: fetch


def stage_fetch(ws: Workspace, source: str, is_url: bool, args) -> dict:
    if ws.meta.exists() and ws.source.exists() and "download" not in args.force:
        meta = json.loads(ws.meta.read_text())
        log(f"source cached: {meta.get('title') or ws.source.name} ({meta['duration']:.0f}s)")
        return meta

    if not is_url:
        src = Path(source).expanduser().resolve()
        if ws.source.exists() or ws.source.is_symlink():
            ws.source.unlink()
        ws.source.symlink_to(src)
        meta = {"input": str(src), "url": None, "title": src.stem,
                "duration": ffprobe_duration(ws.source), "local": True}
    else:
        need("yt-dlp", "brew install yt-dlp")
        last_err = ""
        for i, extra in enumerate(DOWNLOAD_FALLBACKS):
            cmd = [
                "yt-dlp", "-f", args.format, "--merge-output-format", "mp4",
                "-N", "4", "--retries", "10", "--fragment-retries", "10",
                "--no-playlist", "--write-info-json",
                "-o", str(ws.root / "source.%(ext)s"), source,
            ] + extra
            if args.cookies_from_browser:
                cmd += ["--cookies-from-browser", args.cookies_from_browser]
            log(f"downloading (attempt {i + 1}/{len(DOWNLOAD_FALLBACKS)}"
                f"{', ' + ' '.join(extra) if extra else ', default clients'})")
            proc = run(cmd, quiet=False, check=False)
            if proc.returncode == 0 and ws.source.exists():
                break
            last_err = f"yt-dlp exited {proc.returncode}"
        else:
            die(f"download failed after {len(DOWNLOAD_FALLBACKS)} attempts ({last_err}).\n"
                f"          For age-gated / members-only / authed X posts add "
                f"--cookies-from-browser chrome")
        info_path = ws.root / "source.info.json"
        info = json.loads(info_path.read_text()) if info_path.exists() else {}
        meta = {
            "input": source, "url": info.get("webpage_url", source),
            "title": info.get("title"), "uploader": info.get("uploader") or info.get("channel"),
            "extractor": info.get("extractor_key"), "id": info.get("id"),
            "duration": ffprobe_duration(ws.source), "local": False,
        }
    ws.meta.write_text(json.dumps(meta, indent=2))
    log(f"source ready: {meta.get('title')} ({meta['duration']:.0f}s)")
    return meta


# --------------------------------------------------------------------------- stage 2: audio


def stage_audio(ws: Workspace, args) -> Path:
    if ws.audio.exists() and "audio" not in args.force:
        return ws.audio
    log("extracting 16 kHz mono audio")
    run(["ffmpeg", "-v", "error", "-y", "-i", str(ws.source),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(ws.audio)])
    return ws.audio


# --------------------------------------------------------------------------- stage 3: transcribe


def ggml_model(args) -> Path:
    """Provision the whisper.cpp model. Prefers an explicit path, then our own cache,
    then superwhisper's copy (a third-party app dir — it can vanish on uninstall,
    which is why we copy rather than reference), then HuggingFace."""
    if args.ggml:
        p = Path(args.ggml).expanduser()
        if not p.exists():
            die(f"--ggml model not found: {p}")
        return p
    name = args.ggml_name
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    dest = MODEL_DIR / name
    if dest.exists() and dest.stat().st_size > 10_000_000:
        return dest
    donor = SUPERWHISPER / name
    if donor.exists() and donor.stat().st_size > 10_000_000:
        log(f"copying {name} from superwhisper's cache")
        shutil.copy2(donor, dest)
        return dest
    url = GGML_BASE + name
    log(f"fetching {name} (one time) from huggingface")
    tmp = dest.with_suffix(".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
        tmp.replace(dest)
        return dest
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        die(f"could not provision {name} ({exc}). Pass --ggml PATH, or "
            f"--whisper openai to use the slower fallback.")
        raise


def read_transcript_json(path: Path) -> list[dict]:
    """Normalise whisper.cpp or openai-whisper JSON to [{start,end,text}]."""
    d = json.loads(Path(path).read_text())
    if "segments" in d and isinstance(d["segments"], list) and d.get("_sludgify"):
        return d["segments"]
    if "transcription" in d:  # whisper.cpp
        out = []
        for s in d["transcription"]:
            t = s["text"].strip()
            if t:
                out.append({"start": s["offsets"]["from"] / 1000.0,
                            "end": s["offsets"]["to"] / 1000.0, "text": t})
        return out
    if "segments" in d:  # openai-whisper
        return [{"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
                for s in d["segments"] if s["text"].strip()]
    die(f"unrecognised transcript JSON: {path}")
    return []


def transcribe_cpp(audio: Path, out_prefix: Path, model: Path, args) -> list[dict]:
    """whisper.cpp on Metal: measured 30-37x realtime on an M3 Max. Single process,
    no chunking — 4-way parallelism measured 36.0x aggregate against 36.98x for one
    process, because one process already saturates the GPU."""
    cmd = ["whisper-cli", "-m", str(model), "-f", str(audio),
           "-t", str(args.threads), "-oj", "-of", str(out_prefix)]
    if args.language and args.language != "auto" and not model.name.endswith(".en.bin"):
        cmd += ["-l", args.language]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                          stderr=None if VERBOSE else subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        die(f"whisper-cli exited {proc.returncode}")
    return read_transcript_json(out_prefix.with_suffix(".json"))


def silence_boundaries(audio: Path, total: float, jobs: int) -> list[float]:
    """Chunk boundaries that land inside silence, so no word is ever split and the
    merge is a pure offset add — no overlap stitching, no dropped words."""
    proc = run(["ffmpeg", "-hide_banner", "-i", str(audio),
                "-af", "silencedetect=noise=-32dB:d=0.35", "-f", "null", "/dev/null"],
               check=False)
    text = (proc.stderr or "") + (proc.stdout or "")
    starts = [float(x) for x in re.findall(r"silence_start: ([0-9.]+)", text)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", text)]
    mids = sorted((s + e) / 2 for s, e in zip(starts, ends))
    L = max(300.0, min(900.0, total / max(jobs, 1)))
    targets = [k * L for k in range(1, int(total // L) + 1) if k * L < total - 30]
    bounds, snapped = [], 0
    for tgt in targets:
        near = [m for m in mids if abs(m - tgt) <= 60]
        if near:
            bounds.append(min(near, key=lambda m: abs(m - tgt)))
            snapped += 1
        else:
            bounds.append(tgt)
    if targets and snapped < len(targets):
        # Continuous music or a mixed bed defeats silencedetect. Fixed boundaries can
        # split a word, which costs a word or two per chunk edge — acceptable for
        # mining (the message never comes from this transcript), but say so.
        warn(f"{len(targets) - snapped}/{len(targets)} chunk boundaries could not be "
             f"snapped to silence — a word may be split at those edges")
    return sorted(set(round(b, 3) for b in bounds))


def transcribe_openai(audio: Path, work: Path, total: float, args) -> list[dict]:
    """Fallback: openai-whisper, chunked on silence and run in parallel.

    Measured 2.2x realtime single-process against whisper.cpp's 30x, so this path is
    for machines without whisper.cpp or for non-English audio the .en models cannot
    take. Threads are deliberately UNRESTRICTED: pinning OMP_NUM_THREADS=2 measured
    2.2x WORSE than leaving them alone."""
    need("whisper", "pip install -U openai-whisper")
    bounds = silence_boundaries(audio, total, args.jobs)
    edges = [0.0] + bounds + [total]
    chunks = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1) if edges[i + 1] - edges[i] > 1.0]
    cdir = work / "chunks"
    cdir.mkdir(exist_ok=True)
    log(f"openai-whisper fallback: {len(chunks)} chunk(s), {args.jobs} in parallel")

    def one(idx_span):
        i, (a, b) = idx_span
        wav = cdir / f"c{i:03d}.wav"
        if not wav.exists():
            run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
                 "-i", str(audio), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
        js = cdir / f"c{i:03d}.json"
        if not js.exists():
            cmd = ["whisper", str(wav), "--model", args.model, "--output_format", "json",
                   "--output_dir", str(cdir), "--verbose", "False"]
            if args.language and args.language != "auto":
                cmd += ["--language", args.language]
            run(cmd)
        segs = read_transcript_json(js)
        for s in segs:
            s["start"] += a
            s["end"] += a
        return segs

    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        parts = list(pool.map(one, enumerate(chunks)))
    return [s for part in parts for s in part]


def stage_transcribe(ws: Workspace, meta: dict, args) -> tuple[list[dict], dict]:
    if args.transcript:
        segs = read_transcript_json(Path(args.transcript).expanduser())
        return segs, {"backend": "supplied", "path": str(args.transcript)}
    if ws.transcript.exists() and "transcript" not in args.force:
        d = json.loads(ws.transcript.read_text())
        log(f"transcript cached: {len(d['segments'])} segments ({d.get('backend')})")
        return d["segments"], d
    audio = stage_audio(ws, args)
    backend = args.whisper
    if backend == "auto":
        backend = "cpp" if shutil.which("whisper-cli") else "openai"
    t0 = time.time()
    if backend == "cpp":
        model = ggml_model(args)
        log(f"transcribing with whisper.cpp ({model.name}) — expect ~30x realtime")
        segs = transcribe_cpp(audio, ws.root / "whispercpp", model, args)
    else:
        segs = transcribe_openai(audio, ws.root, meta["duration"], args)
    dt = time.time() - t0
    # Health check. Mining needs sentence punctuation SOMEWHERE in the text — not at
    # the segment edges, which resegment() repairs for free. So measure the density of
    # terminal marks per 100 words, not the share of segments ending on one: whisper
    # routinely breaks mid-clause on a time budget (measured: 11% of segments on one
    # source) and warning about that would be a false alarm on every run. What is NOT
    # recoverable is punctuation missing altogether, which happens on noisy or heavily
    # accented audio and silently collapses span growth to the duration clamps.
    words = sum(len(WORD_RE.findall(s["text"].lower())) for s in segs)
    marks = sum(len(re.findall(r"[.!?]", s["text"])) for s in segs)
    per100 = marks * 100.0 / max(words, 1)
    terminal = sum(1 for s in segs if TERMINAL_RE.search(s["text"])) / max(len(segs), 1)
    payload = {
        "_sludgify": True, "backend": backend, "segments": segs,
        "n_segments": len(segs), "words": words,
        "wall_s": round(dt, 1),
        "realtime_x": round(meta["duration"] / dt, 1) if dt else None,
        "terminal_punctuation_rate": round(terminal, 3),
        "sentence_marks_per_100w": round(per100, 2),
        "words_per_second": round(words / max(meta["duration"], 1e-6), 2),
    }
    ws.transcript.write_text(json.dumps(payload, indent=2))
    log(f"transcribed {len(segs)} segments, {words} words in {dt:.0f}s "
        f"({payload['realtime_x']}x realtime)")
    if per100 < 2.0:
        warn(f"only {per100:.1f} sentence marks per 100 words (healthy is ~6) — whisper "
             f"is not punctuating this audio, so span growth will fall back to the "
             f"duration clamps and every candidate will land mid-thought. Try "
             f"--ggml-name ggml-large-v3-turbo.bin.")
    return segs, payload


# --------------------------------------------------------------------------- stage 4: shot cuts


def stage_cuts(ws: Workspace, args) -> dict:
    if args.no_cuts:
        return {"threshold": args.scene_threshold, "times": [], "scores": [], "skipped": True}
    if ws.cuts.exists() and "cuts" not in args.force:
        d = json.loads(ws.cuts.read_text())
        # The scan keeps every score above SCAN_FLOOR, so a different --scene-threshold
        # is re-derived from the cache instead of rescanning the video.
        if d.get("scan_floor") == SCAN_FLOOR and d["threshold"] != args.scene_threshold:
            d["times"] = [t for t, s in d["scores"] if s >= args.scene_threshold]
            d["threshold"] = args.scene_threshold
        log(f"shot-cut scan cached: {len(d['times'])} cut(s) at scene>{args.scene_threshold}")
        return d
    log(f"scanning for camera cuts (scale=320, keeping every score > {SCAN_FLOOR})")
    t0 = time.time()
    proc = run([
        "ffmpeg", "-hide_banner", "-nostdin", "-i", str(ws.source),
        "-filter:v", f"scale=320:-2,select='gt(scene,{SCAN_FLOOR})',metadata=print:file=-",
        "-an", "-f", "null", "/dev/null",
    ], check=False)
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    scored: list[list[float]] = []
    pending = None
    for line in text.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            pending = float(m.group(1))
            continue
        m = re.search(r"lavfi\.scene_score=([0-9.]+)", line)
        if m and pending is not None:
            scored.append([round(pending, 3), round(float(m.group(1)), 4)])
            pending = None
    scored.sort()
    times = [t for t, s in scored if s >= args.scene_threshold]
    d = {
        "threshold": args.scene_threshold, "scan_floor": SCAN_FLOOR,
        "times": times, "scores": scored, "wall_s": round(time.time() - t0, 1),
    }
    ws.cuts.write_text(json.dumps(d))
    log(f"{len(times)} camera cut(s) at scene>{args.scene_threshold} "
        f"({len(scored)} events above {SCAN_FLOOR}) in {d['wall_s']}s")
    # Crossfades ramp gradually and never cross 0.4 — if the source has a big
    # population just under the threshold, the threshold is probably wrong for it.
    near = [s for _t, s in scored if args.scene_threshold * 0.5 <= s < args.scene_threshold]
    if len(near) > max(4, len(times)):
        warn(f"{len(near)} scene events sit just under the {args.scene_threshold} threshold — "
             f"this source may use crossfades. Re-run with --scene-threshold lower "
             f"after eyeballing cuts.json['scores'].")
    return d


# --------------------------------------------------------------------------- stage 5: mining


def resegment(segs: list[dict]) -> list[dict]:
    """Rebuild sentence units out of whatever whisper handed back.

    whisper.cpp segments on a token/time budget, not on sentences. Measured on a real
    podcast: 27 segments, only 11% of them ending on terminal punctuation, every one
    breaking mid-clause at ~5s. Span growth keys off sentence ends, so that collapses
    it to the duration clamps and every candidate lands mid-thought.

    The fix is free: the punctuation is already inside the segment text, it is just not
    at the edges. Concatenate the transcript, split it on terminal punctuation, and map
    each sentence's character range back to a time by interpolating within its source
    segment. Speech is close enough to uniform over five seconds for this to be right
    to a syllable, and sludge.py re-cuts the boundaries at render time anyway."""
    import bisect

    parts, spans, cursor = [], [], 0
    for s in segs:
        t = re.sub(r"\s+", " ", s["text"]).strip()
        if not t:
            continue
        if parts:
            parts.append(" ")
            cursor += 1
        a = cursor
        parts.append(t)
        cursor += len(t)
        spans.append((a, cursor, float(s["start"]), float(s["end"])))
    if not spans:
        return []
    text = "".join(parts)
    ends = [b for _a, b, _t0, _t1 in spans]

    def time_at(i: int, end: bool) -> float:
        k = bisect.bisect_left(ends, i) if end else bisect.bisect_right(ends, i)
        k = min(k, len(spans) - 1)
        a, b, t0, t1 = spans[k]
        if i <= a:
            return t0
        if i >= b:
            return t1
        return t0 + (t1 - t0) * ((i - a) / max(b - a, 1))

    out, i = [], 0
    for m in re.finditer(r"[.!?]+['\"”\)]*(?=\s|$)", text):
        j = m.end()
        chunk = text[i:j].strip()
        if chunk:
            out.append({"start": round(time_at(i, False), 3),
                        "end": round(time_at(j, True), 3), "text": chunk})
        i = j
    tail = text[i:].strip()
    if tail:
        out.append({"start": round(time_at(i, False), 3),
                    "end": round(spans[-1][3], 3), "text": tail})
    return [s for s in out if s["end"] > s["start"]]


@dataclass
class Span:
    a: int
    b: int
    start: float
    end: float
    text: str
    score: float = 0.0
    base: float = 0.0
    theme: str = ""
    dbg: dict = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    @property
    def dur(self) -> float:
        return self.end - self.start


def grow(segs: list[dict], i: int, target: float, lo: float, hi: float,
         cuts: list[float], barrier: bool) -> Span | None:
    """Grow a self-contained thought outward from seed segment i.

    Backward while the span opens on a dangling connective or the previous segment
    did not finish its sentence; forward until it is long enough AND lands on
    terminal punctuation. With `barrier`, a camera cut is a wall in both directions."""
    a = b = i

    def ok(a_: int, b_: int) -> bool:
        s, e = segs[a_]["start"], segs[b_]["end"]
        if e - s > hi:
            return False
        return not (barrier and any(s < c < e for c in cuts))

    while a > 0 and (DANGLING_RE.match(segs[a]["text"]) or not TERMINAL_RE.search(segs[a - 1]["text"])):
        if not ok(a - 1, b):
            break
        a -= 1
    while b < len(segs) - 1:
        if segs[b]["end"] - segs[a]["start"] >= target and TERMINAL_RE.search(segs[b]["text"]):
            break
        if not ok(a, b + 1):
            break
        b += 1

    start, end = segs[a]["start"], segs[b]["end"]
    dur = end - start
    if dur < lo or dur > hi:
        return None
    return annotate_span(span_of(segs, a, b), segs, cuts)


def span_of(segs: list[dict], a: int, b: int) -> Span:
    text = " ".join(s["text"].strip() for s in segs[a:b + 1])
    return Span(a, b, round(segs[a]["start"], 2), round(segs[b]["end"], 2),
                re.sub(r"\s+", " ", text).strip())


def annotate_span(sp: Span, segs: list[dict], cuts: list[float]) -> Span:
    """The flag set every candidate carries, applied identically however the span was
    built — grown by the miner or cut by hand with --trim. Keep this in one place: a
    hand-trimmed span that quietly loses its `ends_mid_clause` warning is exactly the
    broken landing the editorial rules exist to prevent."""
    # Editorial rule B4: mid-clause entry is CORRECT PRACTICE (21% of the corpus), so
    # this is a flag the agent reads, never a filter. The one genuinely bad opening is
    # a dangling connective — which is also on the profile's lead-in trim list.
    if DANGLING_RE.match(sp.text):
        sp.flags.append("opens_on_connective")
    if sp.a > 0 and not TERMINAL_RE.search(segs[sp.a - 1]["text"]):
        sp.flags.append("opens_mid_clause")
    first = re.split(r"(?<=[.!?])\s+", sp.text.strip(), maxsplit=1)[0]
    if HOST_TURN_RE.match(first) or "?" in first:
        # Either the host is talking, or the speaker is restating the question himself
        # (3/76 corpus clips do exactly that and are fine). Read it and decide.
        sp.flags.append("opens_on_question_check_speaker")
    if not TERMINAL_RE.search(sp.text):
        sp.flags.append("ends_mid_clause")
    inside = [c for c in cuts if sp.start < c < sp.end]
    if inside:
        sp.flags.append(f"crosses_{len(inside)}_shot_cut" + ("s" if len(inside) > 1 else ""))
    return sp


def score_span_obj(sp: Span, profile: Profile) -> Span:
    sp.score, sp.base, sp.theme, sp.dbg = profile.score_span(sp.text, sp.dur)
    if sp.dbg["words"] < profile.editorial["duration"]["words_floor"]:
        sp.flags.append("under_word_floor")
    wlo, whi = profile.editorial["duration"]["wps_sanity"]
    if not (wlo <= sp.dbg["wps"] <= whi):
        sp.flags.append("wps_out_of_range")
    return sp


def span_from_window(segs: list[dict], t0: float, t1: float, cuts: list[float],
                     profile: Profile) -> Span:
    """Build ONE span from the sentence units lying inside [t0, t1].

    The editorial rules tell you to trim — "trim the span back to the last complete
    clause", "if the best line is at 60% through, trim so it ends there". Without this
    there is no way to act on that and stay in the pipeline: you drop out to ffmpeg and
    lose the verbatim message, the slug, the strip and the output naming. Units are kept
    only if they fall WHOLLY inside the window, so a trim can never re-admit the half
    sentence you were trying to cut off."""
    idx = [i for i, s in enumerate(segs)
           if s["start"] >= t0 - 0.15 and s["end"] <= t1 + 0.15]
    if not idx:
        near = ", ".join(f"{s['start']:.1f}-{s['end']:.1f}" for s in segs[:6])
        die(f"--trim {t0:.2f}-{t1:.2f} contains no whole sentence unit. "
            f"Units start at: {near} ... — widen the window.")
    sp = score_span_obj(annotate_span(span_of(segs, idx[0], idx[-1]), segs, cuts), profile)
    log(f"--trim {t0:.2f}-{t1:.2f} snapped to {sp.start:.2f}-{sp.end:.2f} "
        f"({sp.dur:.2f}s, {sp.dbg['words']}w, score {sp.score})")
    return sp


def mine(segs: list[dict], cuts: list[float], profile: Profile, args) -> tuple[list[Span], dict]:
    lo, hi = args.body_min, args.body_max
    target = args.body_target
    stats = {"raw_segments": len(segs), "barrier": True}
    if not args.no_resegment:
        rebuilt = resegment(segs)
        if rebuilt:
            before = sum(1 for s in segs if TERMINAL_RE.search(s["text"])) / max(len(segs), 1)
            after = sum(1 for s in rebuilt if TERMINAL_RE.search(s["text"])) / max(len(rebuilt), 1)
            log(f"re-segmented {len(segs)} whisper windows into {len(rebuilt)} sentence "
                f"units (terminal punctuation {before:.0%} -> {after:.0%})")
            segs = rebuilt
    stats["n_segments"] = len(segs)
    stats["units"] = segs  # --trim snaps to these; not serialised into the output

    def pass_(barrier: bool) -> dict[tuple[int, int], Span]:
        out: dict[tuple[int, int], Span] = {}
        for i in range(len(segs)):
            sp = grow(segs, i, target, lo, hi, cuts, barrier)
            if sp and (sp.a, sp.b) not in out:
                out[(sp.a, sp.b)] = sp
        return out

    need_pool = max(args.emit * args.oversample, 8)
    if args.cross_cuts:
        pool, stats["barrier"] = pass_(False), False
    else:
        pool = pass_(True)
        if len(pool) < need_pool:
            # A multicam podcast cuts every few seconds; a hard barrier then makes a
            # 20s span impossible and the pool collapses. Relax rather than return
            # nothing — every survivor carries a crosses_N_shot_cuts flag and is
            # shot-probed, so the agent still sees what it is buying.
            warn(f"only {len(pool)} span(s) fit between camera cuts — relaxing the cut "
                 f"barrier. Candidates that cross a cut are flagged; frame-check them.")
            pool = pass_(False)
            stats["barrier"] = False
    stats["pool"] = len(pool)

    spans = [score_span_obj(sp, profile) for sp in pool.values()]
    spans.sort(key=lambda s: -s.score)
    stats["scored"] = len(spans)
    return spans, stats


def select(spans: list[Span], profile: Profile, args) -> list[Span]:
    """Greedy non-overlapping selection, score first, with a per-theme cap so one
    podcast cannot ship five clips on the same idea."""
    cap = args.max_per_theme * args.oversample if args.max_per_theme else 10 ** 6
    want = args.emit * args.oversample
    picked: list[Span] = []
    counts: dict[str, int] = {}
    for sp in spans:
        if args.min_score is not None and sp.score < args.min_score:
            continue
        if any(sp.end > p.start and sp.start < p.end for p in picked):
            continue
        if counts.get(sp.theme, 0) >= cap:
            continue
        picked.append(sp)
        counts[sp.theme] = counts.get(sp.theme, 0) + 1
        if len(picked) >= want:
            break
    if len(picked) < args.emit and args.min_score is not None:
        warn(f"only {len(picked)} span(s) cleared the score floor of {args.min_score} — "
             f"this source may not be on-narrative. Lower --min-score to see the rest.")
    picked.sort(key=lambda s: s.start)
    return picked


def auto_emit(duration: float) -> int:
    """Roughly one clip per 8-10 minutes, clamped to [1,12]."""
    if duration <= 60:
        return 1
    mins = duration / 60.0
    pts = [(1, 1), (10, 2), (30, 4), (60, 6), (120, 9), (180, 11)]
    if mins <= pts[0][0]:
        return 1
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if mins <= x1:
            return max(1, min(12, round(y0 + (y1 - y0) * (mins - x0) / (x1 - x0))))
    return 12


# --------------------------------------------------------------------------- message / slug


def trim_lead_in(text: str, tokens: list[str], max_trim: int) -> tuple[str, int]:
    """Strip the corpus's lead-in tokens off the front of a message. Budget is 4 words
    (corpus median is 1); after that the trim would start eating the claim."""
    toks = sorted(tokens, key=lambda t: -len(t))
    out, n = text.lstrip(), 0
    while n < max_trim:
        low = out.lower()
        hit = next((t for t in toks if low.startswith(t + " ") or low.startswith(t + ",")), None)
        if not hit:
            break
        out = out[len(hit):].lstrip(" ,")
        n += len(hit.split())
    return out[:1].upper() + out[1:] if out else text, n


def last_sentence(text: str) -> str:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    return parts[-1] if parts else text


def make_slug(text: str, theme: str, profile: Profile, taken: set[str]) -> str:
    """Slug the way the user does: a near-verbatim lift of the payoff phrase out of the
    CLOSING line (57% of corpus slugs come from the last sentence), not a summary.

    Every contiguous 3-5 word window of that sentence is scored on content density,
    with a nudge toward the end of the line and toward the theme's own vocabulary. The
    naive "last few words" rule loses to function words — it named a dedication clip
    `you-are-doing` — and the corpus never does that: `shitty-wage-cucking-job`,
    `most-stocks-are-shitcoins`, `traders-are-the-new-celebrities`."""
    nm = profile.editorial["naming"]
    lo, hi = nm["slug_words"]
    core = set(profile.themes.get(theme, {}).get("core", []))

    def windows(sentence: str, weight: float) -> list[tuple[float, list[str]]]:
        words = re.findall(r"[a-z0-9']+", sentence.lower())
        out = []
        for n in range(lo, hi + 1):
            for i in range(len(words) - n + 1):
                w = words[i:i + n]
                if w[-1] in STOPWORDS or w[0] in BAD_OPENERS:
                    continue
                if len(slugify(" ".join(w), hi, 999)) > nm["slug_chars_max"]:
                    continue
                density = sum(1 for x in w if x not in STOPWORDS) / n
                pos = (i + n) / max(len(words), 1)
                out.append(((density * 2 + pos * 0.6 + (0.35 if core & set(w) else 0)) * weight, w))
        return out

    # Closing line only. Widening the search to the first sentence as well was measured
    # against the user's own 76 slugs and made agreement WORSE (25/76 clips sharing a
    # word with the human slug, down to 21/76) — the payoff really does live at the end.
    cands = windows(last_sentence(text), 1.0) or windows(text, 0.7)
    window = max(cands)[1] if cands else None
    if not window:
        words = re.findall(r"[a-z0-9']+", text.lower())
        while words and words[-1] in STOPWORDS:
            words.pop()
        window = words[-hi:]
    slug = slugify(" ".join(window), max_words=hi, max_chars=nm["slug_chars_max"]) or "clip"
    base, i = slug, 2
    while slug in taken:
        slug = f"{base}-{i}"
        i += 1
    taken.add(slug)
    return slug


def emphasis_candidate(message: str, theme: str, profile: Profile) -> str | None:
    """One *asterisk* max, on the most screenshot-able word in the CLOSING clause —
    and none at all when a number is present, because auto-emphasis already has it."""
    if profile.auto_emphasis_re.search(message.lower()):
        return None
    tail = last_sentence(message).lower()
    core = set(profile.themes.get(theme, {}).get("core", []))
    words = [w for w in re.findall(r"[a-z']+", tail) if w not in STOPWORDS and len(w) >= 4]
    if not words:
        return None
    hits = [w for w in words if w in core]
    return (hits[-1] if hits else max(words, key=len))


# --------------------------------------------------------------------------- stage 6: shot safety


def grab_frame(video: Path, t: float):
    import cv2
    import numpy as np
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", str(video),
         "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True, stdin=subprocess.DEVNULL)
    if not p.stdout:
        return None
    return cv2.imdecode(np.frombuffer(p.stdout, np.uint8), cv2.IMREAD_COLOR)


def face_signature(crop):
    import cv2
    if crop is None or crop.size == 0:
        return None
    crop = cv2.resize(crop, (64, 64))
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1, 2], None, [8, 8, 8], [0, 180, 0, 256, 0, 256])
    return cv2.normalize(h, h).flatten()


def fetch_yunet() -> Path | None:
    model = SLUDGE_CACHE / YUNET_NAME
    if model.exists() and model.stat().st_size > 100_000:
        return model
    SLUDGE_CACHE.mkdir(parents=True, exist_ok=True)
    tmp = model.with_suffix(".part")
    try:
        log("fetching the YuNet face model (one time, ~230 KB)")
        with urllib.request.urlopen(YUNET_URL, timeout=20) as resp, tmp.open("wb") as fh:
            shutil.copyfileobj(resp, fh)
        tmp.replace(model)
        return model
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        warn(f"could not fetch the YuNet model ({exc}) — shot probe disabled")
        return None


def shot_probe(video: Path, start: float, end: float, n: int, cuts: list[float],
               model: Path) -> dict:
    """Sample n frames across a span and report whether the camera held still.

    Six cheap signals: face missing, face height stepping, face x jumping, appearance
    correlation collapsing, a second face appearing, a scene cut landing inside.

    THIS IS NOT SPEAKER IDENTITY. All six prove the CAMERA did not change; none of
    them can tell who is talking. A locked single-camera 2-shot where the host speaks
    over the guest passes every one — which is precisely the failure that shipped 24
    seconds of the wrong man's face. Diarization is out of scope. Read the strip."""
    import cv2
    import numpy as np

    times = list(np.linspace(start, end, n))
    det = None
    samples = []
    for t in times:
        fr = grab_frame(video, float(t))
        if fr is None:
            samples.append({"t": round(float(t), 2), "faces": 0})
            continue
        H, W = fr.shape[:2]
        if det is None:
            det = cv2.FaceDetectorYN.create(str(model), "", (W, H), 0.6, 0.3, 5000)
        det.setInputSize((W, H))
        _, faces = det.detect(fr)
        rec = {"t": round(float(t), 2), "faces": 0 if faces is None else len(faces)}
        if faces is not None and len(faces):
            f = max(faces, key=lambda f: f[3])
            x, y, w, h = [int(v) for v in f[:4]]
            rec |= {"cx": round((x + w / 2) / W, 4), "cy": round((y + h / 2) / H, 4),
                    "h": round(h / H, 4), "score": round(float(f[-1]), 3)}
            pad = int(h * 0.15)
            crop = fr[max(0, y - pad):min(H, y + h + pad), max(0, x - pad):min(W, x + w + pad)]
            rec["_sig"] = face_signature(crop)
        samples.append(rec)

    seen = [s for s in samples if s.get("faces", 0) > 0 and s.get("_sig") is not None]
    flags, fatal = [], []
    if len(seen) < len(samples) * 0.8:
        msg = (f"face missing in {len(samples) - len(seen)}/{len(samples)} samples "
               f"— b-roll, graphics, slides or a cutaway")
        flags.append(msg)
        # Below half, there is no head to lock onto and the top pane has nothing to
        # show. That is not a judgement call the agent needs to make, so it is graded
        # separately: still reported in full, just not auto-picked.
        if len(seen) < len(samples) * 0.5:
            fatal.append(msg)
    if len(seen) >= 2:
        hs = np.array([s["h"] for s in seen])
        if hs.max() / max(hs.min(), 1e-6) > 1.6:
            flags.append(f"face height varies {hs.min():.3f}->{hs.max():.3f} "
                         f"({hs.max() / hs.min():.2f}x) — likely a camera change")
        cx = np.array([s["cx"] for s in seen])
        if cx.max() - cx.min() > 0.25:
            flags.append(f"face x moves {cx.min():.3f}->{cx.max():.3f} — reframe or cut")
        base = seen[0]["_sig"].astype("float32")
        cors = [float(cv2.compareHist(base, s["_sig"].astype("float32"), cv2.HISTCMP_CORREL))
                for s in seen]
        if min(cors) < 0.5:
            # weak signal: catches "different man, different shirt", not two similarly
            # lit people in similar clothes against the same backdrop
            flags.append(f"face appearance correlation drops to {min(cors):.2f} "
                         f"— possibly a DIFFERENT PERSON")
    counts = [s.get("faces", 0) for s in samples]
    if max(counts, default=0) >= 2:
        flags.append(f"up to {max(counts)} faces on screen — wide or 2-shot inside the span")
    inside = [c for c in cuts if start < c < end]
    if inside:
        flags.append(f"scene cut(s) at {inside} inside the span")

    return {
        "samples": [{k: v for k, v in s.items() if k != "_sig"} for s in samples],
        "scene_cuts": inside, "flags": flags, "fatal": fatal,
        "safe": not flags, "renderable": not fatal,
    }


# --------------------------------------------------------------------------- stage 7: extract


def extract(ws: Workspace, sp: Span, n: int, slug: str, force: bool) -> Path:
    """Frame-exact extraction. Re-encode, never stream-copy: -c copy snaps to the
    nearest keyframe and the span drifts by up to a couple of seconds.

    Extraction is also what makes the sludge.py handoff affordable. sludge.py
    transcribes the WHOLE head file before it parses --edl, and its transcript cache
    key includes the per-clip prompt, so it misses on every clip — handing it a 3h
    source with --edl pays a full-source whisper pass PER EMITTED CLIP. Pre-extracting
    also hands boundary control to sludge's own jump-cut logic, which is why the
    cut-edge warnings disappear."""
    ws.clips.mkdir(parents=True, exist_ok=True)
    out = ws.clips / f"{n:02d}-{slug}.mp4"
    if out.exists() and not force:
        return out
    run(["ffmpeg", "-v", "error", "-y", "-nostdin",
         "-ss", f"{sp.start:.3f}", "-to", f"{sp.end:.3f}", "-i", str(ws.source),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)])
    return out


# --------------------------------------------------------------------------- stage 8: render


def sludge_argv(sludge: Path, head: Path, out: Path, message: str, profile: Profile,
                args) -> list[str]:
    argv = [str(sludge), "--head", str(head), "--clip", str(Path(args.clip).expanduser().resolve())]
    if args.music:
        argv += ["--music", str(Path(args.music).expanduser().resolve())]
    if args.cta:
        argv += ["--cta", str(Path(args.cta).expanduser().resolve())]
    if message:
        argv += ["--message", message]
    for flag, val in profile.sludge_invocation.get("render_defaults", {}).items():
        argv += [flag, str(val)]
    # Necessary but NOT sufficient: a cut from a wider angle still locks at a lower
    # scale and renders letterboxed while coverage reports 100%. The strip is the check.
    argv += ["--headroom", str(args.headroom)]
    if args.no_prime:
        argv += ["--no-prime"]
    if args.vocab:
        argv += ["--vocab", args.vocab]
    argv += list(args.sludge_arg)
    argv += ["--out", str(out)]
    return argv


def rendered_shot_times(out: Path, body: float) -> tuple[list[float], list[float]]:
    """Find the visible cuts in the RENDERED top pane and return (cut_times, sample_times),
    where sample_times is the midpoint of every shot between them.

    The strip's whole job is "is this the same person at every cut". Sampling the body at
    fixed fractions cannot answer that: it lands wherever the clock falls, so a wrong-face
    shot shorter than the sampling stride is invisible. Measured on a real render
    (sludge-chriscamillo-stuff-means-youre-winning): the head lock sat on the HOST from
    6.73s to 14.23s — 7.5s of a 23.5s body — and the fraction sampler caught it only
    because 0.5 x body happened to fall inside. A 2s cutaway would have been missed.

    ffmpeg's `scdet` reports the top pane's own cuts, which is exactly what the viewer
    sees after the head lock has warped and re-scaled each segment. Threshold 8 was picked
    against that render: it returns the two true boundaries (6.73, 14.23) and nothing else,
    while <=4 starts emitting intra-shot noise."""
    p = run(["ffmpeg", "-v", "info", "-nostdin", "-t", f"{body:.3f}", "-i", str(out),
             "-vf", "crop=1080:960:0:0,scale=320:-1,scdet=threshold=8", "-an",
             "-f", "null", "-"], check=False)
    raw = sorted(float(m) for m in
                 re.findall(r"lavfi\.scd\.time:\s*([0-9.]+)", p.stderr or ""))
    # scdet fires on several consecutive frames of one cut; collapse each burst.
    cuts: list[float] = []
    for t in raw:
        if 0.15 < t < body - 0.15 and (not cuts or t - cuts[-1] > 0.40):
            cuts.append(round(t, 2))
    bounds = [0.0] + cuts + [body]
    shots = [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a >= 0.25]
    if not shots:
        return cuts, []
    if len(shots) > 12:  # keep the longest, but say so — every cut is supposed to be seen
        shots = sorted(sorted(shots, key=lambda s: s[1] - s[0])[-12:])
        warn(f"{len(cuts) + 1} shots in the render; the strip samples the 12 longest. "
             f"Check the rest by hand.")
    return cuts, [round((a + b) / 2, 2) for a, b in shots]


def frame_strip(out: Path, dest: Path, times: list[float]) -> Path | None:
    """The mandatory per-clip check. Top pane only (crop=1080:960:0:0) so what you are
    reading is the head lock: same person, same background, same head size across every
    cut. Every temp name carries the clip's own prefix — renders run concurrently and
    generic /tmp/fr_*.png gets clobbered between clips."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    stem = dest.with_suffix("")
    frames = []
    for i, t in enumerate(times):
        f = Path(f"{stem}-fr{i}.png")
        p = run(["ffmpeg", "-v", "error", "-y", "-nostdin", "-ss", f"{t:.2f}", "-i", str(out),
                 "-frames:v", "1", "-vf", "crop=1080:960:0:0,scale=260:-1", str(f)], check=False)
        if p.returncode == 0 and f.exists():
            frames.append(f)
    if not frames:
        return None
    cmd = ["ffmpeg", "-v", "error", "-y", "-nostdin"]
    for f in frames:
        cmd += ["-i", str(f)]
    cmd += ["-filter_complex", f"hstack=inputs={len(frames)}", str(dest)]
    if run(cmd, check=False).returncode != 0:
        return None
    for f in frames:
        f.unlink(missing_ok=True)
    return dest


# --------------------------------------------------------------------------- self test


def self_test(profile: Profile, manifest: Path | None) -> int:
    problems = 0
    print(f"profile {profile.path}  v{profile.version}  fingerprint {profile.fingerprint}",
          file=sys.stderr)
    v = profile.data.get("validation", {})
    print(f"profile validation ok={v.get('ok')} problems={len(v.get('problems', []))}",
          file=sys.stderr)
    for lo, hi, want in [(20.0, 20.0, 6.0), (15.0, 15.0, 6.0), (25.0, 25.0, 6.0),
                         (12.0, 12.0, 4.0), (9.0, 9.0, 2.0), (34.0, 34.0, 2.0),
                         (35.0, 35.0, 0.0), (8.0, 8.0, 0.0)]:
        got = profile.duration_points(lo)
        if got != want:
            print(f"FAIL duration band {lo}s -> {got}, expected {want}", file=sys.stderr)
            problems += 1
    if not manifest:
        print("no --manifest given: skipping the corpus re-score "
              "(pass corpus.json to diff against per_clip_scores)", file=sys.stderr)
        return problems
    rows = json.loads(Path(manifest).read_text())
    want = {r["slug"]: r for r in profile.data["per_clip_scores"]}
    worst, n, bad = 0.0, 0, 0
    for r in rows:
        exp = want.get(r["slug"])
        if not exp:
            continue
        got, base, theme, _ = profile.score_span(r["text"], r["dur"])
        n += 1
        d = abs(got - exp["score"])
        worst = max(worst, d)
        if d > 0.05 or theme != exp["primary_theme"]:
            bad += 1
            print(f"DRIFT {r['slug']}: score {got} vs {exp['score']}, "
                  f"theme {theme} vs {exp['primary_theme']}", file=sys.stderr)
    print(f"re-scored {n}/{len(want)} corpus clips; {bad} mismatch(es); "
          f"max |delta| = {worst}", file=sys.stderr)
    return problems + bad


# --------------------------------------------------------------------------- main


EPILOG = textwrap.dedent("""
    examples:
      # plan only: what would you cut out of this podcast?
      sludgify.py "https://youtu.be/XXXX" --plan | jq '.candidates[] | {n,score,slug,text}'

      # a local file, more candidates, everything scored
      sludgify.py raw/podcast.mp4 --plan --emit 6 --oversample 3 --dump-pool

      # render the shortlist's picks through sludge.py
      sludgify.py raw/podcast.mp4 --render \\
          --clip filler.mp4 --music bed.mp3 --cta cta.mp4 \\
          --speaker hayes --out-dir out/

      # render only candidates 2 and 5, after reading their text
      sludgify.py raw/podcast.mp4 --render --pick 2,5 --clip filler.mp4

    the workflow this is built for:
      1. --plan, then READ the candidate text. The score measures theme fit; it cannot
         tell what is funny, damning or quotable. Overrule it.
      2. Check every candidate's shot.flags. They mean "the camera moved", never
         "the wrong person is talking" — that one is yours to catch.
      3. --render --pick, then READ the frame strip in <work>/verify/. 100% detector
         coverage is not a safety signal; it is what the wrong-man batch reported.
    """).rstrip()


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sludgify.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Mine a video for sludge-able spans, score them against the house "
                    "corpus profile, shot-check them, and hand the survivors to sludge.py.",
        epilog=EPILOG,
    )
    p.add_argument("source", nargs="?", help="YouTube / X URL, or a local video file")

    s = p.add_argument_group("pipeline")
    s.add_argument("--work", type=Path, help="workspace dir (default ~/.cache/b-sludgify/<key>)")
    s.add_argument("--force", default="", help="comma-separated stages to recompute: "
                                               "download,audio,transcript,cuts,clips,all")
    s.add_argument("--profile", type=Path, default=DEFAULT_PROFILE,
                   help="corpus-profile.json (default: the one beside this skill)")
    s.add_argument("--json", type=Path, help="also write the result JSON here")
    s.add_argument("-q", "--quiet", action="store_true", help="silence progress on stderr")

    d = p.add_argument_group("download")
    d.add_argument("--format", default=DEFAULT_FORMAT, help="yt-dlp format selector "
                                                            "(default: <=1080p avc1 + m4a)")
    d.add_argument("--cookies-from-browser", help="e.g. chrome — for age-gated, "
                                                  "members-only or authed X posts")

    t = p.add_argument_group("transcription")
    t.add_argument("--whisper", choices=["auto", "cpp", "openai"], default="auto",
                   help="auto prefers whisper.cpp (measured 30-37x realtime) and falls "
                        "back to chunked openai-whisper (2.2x)")
    t.add_argument("--ggml", help="path to a ggml whisper.cpp model")
    t.add_argument("--ggml-name", default="ggml-small.en.bin",
                   help="model to provision if --ggml is not given; use "
                        "ggml-large-v3-turbo.bin for non-English or noisy audio")
    t.add_argument("--model", default="small", help="openai-whisper fallback model")
    t.add_argument("--language", default="en", help="spoken language, or 'auto'")
    t.add_argument("--threads", type=int, default=8, help="whisper.cpp threads")
    t.add_argument("--jobs", type=int, default=6,
                   help="parallel chunks for the openai-whisper fallback. Do NOT pin "
                        "OMP_NUM_THREADS alongside it — measured 2.2x worse")
    t.add_argument("--transcript", help="use this transcript instead of transcribing "
                                        "(whisper.cpp or openai-whisper JSON)")

    m = p.add_argument_group("mining")
    m.add_argument("--emit", type=int, help="clips to mark as picks "
                                            "(default: ~1 per 8-10 min, clamped 1-12)")
    m.add_argument("--oversample", type=int, default=2,
                   help="shortlist this many times --emit, so there is something to "
                        "choose between (default 2)")
    m.add_argument("--min-score", type=float,
                   help="score floor (default: the profile's reject threshold)")
    m.add_argument("--body-target", type=float, help="target speech seconds (default from profile)")
    m.add_argument("--body-min", type=float, help="hard floor, speech seconds")
    m.add_argument("--body-max", type=float, help="hard ceiling, speech seconds")
    m.add_argument("--max-per-theme", type=int,
                   help="cap clips sharing a primary theme (default from profile)")
    m.add_argument("--scene-threshold", type=float, default=SCENE_THRESHOLD,
                   help="scene score that counts as a camera cut (default 0.4)")
    m.add_argument("--no-cuts", action="store_true", help="skip the shot-cut scan entirely")
    m.add_argument("--cross-cuts", action="store_true",
                   help="let spans cross camera cuts from the start (they are flagged "
                        "either way; sludgify already relaxes this when the pool collapses)")
    m.add_argument("--no-resegment", action="store_true",
                   help="mine whisper's own segments instead of rebuilding sentence "
                        "units from the punctuation inside them")
    m.add_argument("--dump-pool", action="store_true",
                   help="include every scored span's text in the output, for re-fitting "
                        "the thresholds against real rejections")
    m.add_argument("--trim", metavar="START-END",
                   help="ignore the shortlist and cut ONE span from this source-time "
                        "window (seconds), snapped to whole sentence units. This is how "
                        "you act on ends_mid_clause, drop a host turn off the front, or "
                        "land on the thesis when the best line is 60%% through a candidate")

    f = p.add_argument_group("shot safety")
    f.add_argument("--probe-frames", type=int, default=12, help="frames sampled per candidate")
    f.add_argument("--no-probe", action="store_true", help="skip the shot-safety probe")
    f.add_argument("--unsafe", choices=["flag", "demote", "ignore"], default="flag",
                   help="flag (default): report every flag, but do not auto-pick a span "
                        "with no face to lock onto; demote: keep any flagged span out of "
                        "the picks; ignore: pick on score alone. Nothing is ever dropped "
                        "from the shortlist — force one back with --pick N")

    r = p.add_argument_group("output")
    r.add_argument("--plan", action="store_true",
                   help="mine and print candidates, render nothing (the default)")
    r.add_argument("--extract", action="store_true",
                   help="also cut each pick out of the source into <work>/clips/")
    r.add_argument("--render", action="store_true", help="extract, then run sludge.py per pick")
    r.add_argument("--pick", help="render exactly these candidate numbers, e.g. 2,5. "
                                  "Numbers are shortlist positions from a --plan run at "
                                  "the same --emit/--oversample; the shortlist is widened "
                                  "automatically to reach the highest one asked for")
    r.add_argument("--out-dir", type=Path, default=Path.cwd(), help="where rendered mp4s land")
    r.add_argument("--speaker", help="speaker key for sludge-{speaker}-{slug}.mp4")
    r.add_argument("--dry-run", action="store_true",
                   help="with --render, print the sludge.py argv instead of running it")

    g = p.add_argument_group("render (passed through to sludge.py)")
    g.add_argument("--sludge", type=Path, default=DEFAULT_SLUDGE, help="path to sludge.py")
    g.add_argument("--clip", help="bottom filler video — REQUIRED by sludge.py")
    g.add_argument("--music", help="music bed")
    g.add_argument("--cta", help="call-to-action video appended after the body")
    g.add_argument("--headroom", type=float, default=1.4,
                   help="sludge --headroom (default 1.4: multicam angles otherwise lock "
                        "low and render letterboxed)")
    g.add_argument("--no-prime", action="store_true",
                   help="pass --no-prime to sludge.py — use when the primed transcript "
                        "comes back with implausible words/sec")
    g.add_argument("--vocab", default="", help="sludge --vocab: tickers, handles, protocols")
    g.add_argument("--no-message", action="store_true",
                   help="render without --message; captions then come from the transcript")
    g.add_argument("--sludge-arg", action="append", default=[],
                   help="extra argument passed straight to sludge.py (repeatable)")
    g.add_argument("--no-strip", action="store_true",
                   help="skip the mandatory per-clip frame strip. Do not.")
    g.add_argument("--render-jobs", type=int, default=1, help="renders to run at once")

    x = p.add_argument_group("diagnostics")
    x.add_argument("--self-test", action="store_true",
                   help="re-score the corpus with this file's scorer and diff against "
                        "the profile's own per_clip_scores")
    x.add_argument("--manifest", type=Path, help="--self-test: corpus.json to re-score")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    global VERBOSE
    args = parse_args(argv)
    VERBOSE = not args.quiet
    profile = Profile(args.profile)

    if args.self_test:
        return 1 if self_test(profile, args.manifest) else 0
    if not args.source:
        die("a source is required (a URL or a local video file). See --help.")

    ed = profile.editorial
    args.body_target = args.body_target or float(ed["duration"]["sludge_target_duration_flag"])
    if args.body_min is None:
        args.body_min = float(ed["duration"]["body_hard_s"][0])
    if args.body_max is None:
        args.body_max = float(ed["duration"]["body_hard_s"][1])
    if args.max_per_theme is None:
        args.max_per_theme = int(ed["selection_policy"]["max_clips_per_primary_theme_per_video"])
    if args.min_score is None:
        args.min_score = float(profile.thresholds["reject"]["below"])
    args.force = {s.strip() for s in args.force.split(",") if s.strip()}
    if "all" in args.force:
        args.force |= {"download", "audio", "transcript", "cuts", "clips"}

    for tool in ("ffmpeg", "ffprobe"):
        need(tool, "brew install ffmpeg")
    if args.render and args.plan:
        warn("--plan and --render together: --plan wins, nothing will be rendered")
    # Fail on a missing --clip before spending minutes transcribing, not after.
    if args.render and not args.plan:
        if not args.clip:
            die("--render needs --clip (sludge.py requires a bottom filler video)")
        for label, val in (("--clip", args.clip), ("--music", args.music), ("--cta", args.cta)):
            if val and not Path(val).expanduser().exists():
                die(f"{label} not found: {val}")
        if not args.sludge.expanduser().exists():
            die(f"sludge.py not found at {args.sludge} — pass --sludge PATH")

    key, is_url = source_key(args.source)
    ws = Workspace(args.work or (CACHE / key), key)
    ws.root.mkdir(parents=True, exist_ok=True)
    log(f"workspace {ws.root}")

    meta = stage_fetch(ws, args.source, is_url, args)
    segs, tinfo = stage_transcribe(ws, meta, args)
    if not segs:
        die("the transcript is empty — nothing to mine")
    cuts = stage_cuts(ws, args)

    if args.emit is None:
        args.emit = auto_emit(meta["duration"])
    wanted: set[int] = set()
    if args.pick:
        wanted = {int(x) for x in re.split(r"[,\s]+", args.pick) if x.strip()}
        # Candidate numbers are positions in the shortlist, so they only mean the same
        # thing at the same --emit/--oversample. Grow the shortlist to cover whatever
        # was asked for rather than silently rendering nothing.
        while args.emit * args.oversample < max(wanted):
            args.oversample += 1
    log(f"emitting {args.emit} pick(s) for {meta['duration'] / 60:.0f} min of source "
        f"(shortlisting {args.emit * args.oversample})")

    spans, stats = mine(segs, cuts["times"], profile, args)
    units = stats.pop("units", segs)
    if args.trim:
        m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*[-:,]\s*([0-9]*\.?[0-9]+)\s*$", args.trim)
        if not m:
            die(f"--trim wants START-END in seconds, e.g. --trim 4.2-25.6 (got {args.trim!r})")
        t0, t1 = float(m.group(1)), float(m.group(2))
        if t1 <= t0:
            die(f"--trim {t0}-{t1}: END must be after START")
        shortlist = [span_from_window(units, t0, t1, cuts["times"], profile)]
        args.emit = 1  # a trim is one deliberate clip; it is always the pick
        if shortlist[0].score < args.min_score:
            warn(f"the trimmed span scores {shortlist[0].score}, below the reject floor "
                 f"of {args.min_score}. You asked for it explicitly, so it is kept — but "
                 f"read it again.")
    else:
        shortlist = select(spans, profile, args)
    log(f"{stats['pool']} span(s) grown, {len(shortlist)} shortlisted "
        f"(scores {min((s.score for s in shortlist), default=0)}-"
        f"{max((s.score for s in shortlist), default=0)}; "
        f"reject floor {args.min_score})")

    # shot safety
    yunet = None if args.no_probe else fetch_yunet()
    probes: dict[int, dict] = {}
    if yunet and shortlist:
        log(f"shot-probing {len(shortlist)} candidate(s) at {args.probe_frames} frames each")
        with ThreadPoolExecutor(max_workers=min(6, len(shortlist))) as pool:
            futures = {i: pool.submit(shot_probe, ws.source, sp.start, sp.end,
                                      args.probe_frames, cuts["times"], yunet)
                       for i, sp in enumerate(shortlist)}
            for i, fut in futures.items():
                probes[i] = fut.result()
        flagged = sum(1 for p in probes.values() if not p["safe"])
        dead = sum(1 for p in probes.values() if p.get("fatal"))
        log(f"shot probe: {len(shortlist) - flagged} clean, {flagged} flagged, "
            f"{dead} with no face to lock onto. A flag means the CAMERA moved — "
            f"none of these can tell you WHO is talking.")

    # assemble
    taken: set[str] = set()
    lead_tokens = ed["opening"]["lead_in_tokens_to_trim"]
    lead_max = int(ed["opening"]["lead_in_words_max"])
    speaker = args.speaker or slugify(meta.get("uploader") or meta.get("title") or "speaker",
                                      max_words=2, max_chars=20)
    cands = []
    for i, sp in enumerate(shortlist):
        msg, trimmed = trim_lead_in(sp.text, lead_tokens, lead_max)
        slug = make_slug(sp.text, sp.theme, profile, taken)
        probe = probes.get(i)
        flags = list(sp.flags)
        if trimmed:
            flags.append(f"lead_in_trimmed_{trimmed}w")
        cands.append({
            "n": i + 1, "start": sp.start, "end": sp.end, "dur": round(sp.dur, 2),
            "score": sp.score, "base": sp.base, "band": profile.band(sp.score),
            "primary_theme": sp.theme, "themes": sp.dbg["themes"],
            "veto": sp.dbg["veto"], "gates": sp.dbg["gates"],
            "words": sp.dbg["words"], "wps": sp.dbg["wps"],
            "filler_per_100w": sp.dbg["filler_per_100w"],
            "stance": sp.dbg["stance"], "address": sp.dbg["address"],
            "concrete": sp.dbg["concrete"], "shape": sp.dbg["shape"],
            "flags": flags,
            "slug": slug, "speaker": speaker,
            "text": sp.text,
            "message": msg,
            "emphasis_candidate": emphasis_candidate(msg, sp.theme, profile),
            "shot": probe or {"skipped": True},
            "pick": False,
        })

    # picks: strict per-theme cap, and (optionally) shot-flagged candidates demoted
    counts: dict[str, int] = {}
    if wanted:
        for c in cands:
            c["pick"] = c["n"] in wanted
        missing = sorted(wanted - {c["n"] for c in cands})
        if missing:
            warn(f"--pick {','.join(map(str, missing))} does not exist: the shortlist "
                 f"only runs to #{len(cands)}. Candidate numbers are shortlist positions, "
                 f"so they shift if --emit or --oversample change.")
    else:
        # Score orders the shortlist, but two things outrank it when choosing what to
        # auto-pick, because both are format failures rather than taste: a span with no
        # face to lock onto, and a span that opens on what may be the host's turn (the
        # profile's own rule is "one speaker per clip, always"). Neither is dropped —
        # they stay in the output with their reasons and can still be forced with --pick.
        def priority(c):
            return (bool(c["shot"].get("fatal")) if args.unsafe != "ignore" else False,
                    args.unsafe == "demote" and not c["shot"].get("safe", True),
                    "opens_on_question_check_speaker" in c["flags"],
                    -c["score"])

        picked = 0
        for c in sorted(cands, key=priority):
            if picked >= args.emit:
                break
            if args.unsafe != "ignore" and c["shot"].get("fatal"):
                continue
            if args.unsafe == "demote" and not c["shot"].get("safe", True):
                continue
            if counts.get(c["primary_theme"], 0) >= args.max_per_theme:
                continue
            c["pick"] = True
            picked += 1
            counts[c["primary_theme"]] = counts.get(c["primary_theme"], 0) + 1
        if picked < args.emit:
            warn(f"only {picked} of {args.emit} pick(s) are renderable — the rest have no "
                 f"face to lock onto or hit the per-theme cap. The full shortlist is still "
                 f"in the output; force one with --pick N.")

    result = {
        "schema": "sludgify-candidates/1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": meta | {"key": ws.key, "work": str(ws.root)},
        "profile": {"path": str(profile.path), "version": profile.version,
                    "corpus_fingerprint": profile.fingerprint},
        "transcript": {k: v for k, v in tinfo.items() if k not in ("segments", "_sludgify")},
        "shot_cuts": {"threshold": cuts["threshold"], "n": len(cuts["times"]),
                      "times": cuts["times"][:400],
                      "median_gap_s": median_gap(cuts["times"])},
        "mining": {
            "emit": args.emit, "oversample": args.oversample, "min_score": args.min_score,
            "body_s": [args.body_min, args.body_max], "body_target_s": args.body_target,
            "max_per_theme": args.max_per_theme, "cut_barrier": stats["barrier"],
            "sentence_units": stats["n_segments"], "pool": stats["pool"],
            "shortlisted": len(cands), "trim": args.trim,
        },
        "thresholds": profile.thresholds,
        "candidates": cands,
        "must_do_before_shipping": profile.data["scoring"]["post_score_gates"],
    }
    if args.dump_pool:
        result["scored_pool"] = [
            {"start": s.start, "end": s.end, "dur": round(s.dur, 2), "score": s.score,
             "base": s.base, "primary_theme": s.theme, "veto": s.dbg["veto"],
             "gates": s.dbg["gates"], "words": s.dbg["words"], "text": s.text}
            for s in spans[:400]
        ]

    # extract / render
    do_render = args.render and not args.plan
    if args.extract or do_render:
        for c in cands:
            if not c["pick"]:
                continue
            sp = shortlist[c["n"] - 1]
            path = extract(ws, sp, c["n"], c["slug"], "clips" in args.force)
            c["clip"] = str(path)
            log(f"extracted #{c['n']} {c['slug']} -> {path.name}")

    if do_render:
        sludge = args.sludge.expanduser()
        args.out_dir.mkdir(parents=True, exist_ok=True)
        picks = [c for c in cands if c["pick"]]
        cta_len = ffprobe_duration(Path(args.cta).expanduser()) if args.cta else 0.0

        def render_one(c):
            out = args.out_dir / f"sludge-{c['speaker']}-{c['slug']}.mp4"
            msg = "" if args.no_message else c["message"]
            cmd = sludge_argv(sludge, Path(c["clip"]), out, msg, profile, args)
            c["sludge_argv"] = cmd
            if args.dry_run:
                print(" ".join(shell_quote(x) for x in cmd), file=sys.stderr)
                return
            log(f"rendering #{c['n']} {c['slug']}")
            proc = subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr,
                                  stdin=subprocess.DEVNULL)
            c["render_returncode"] = proc.returncode
            if proc.returncode != 0:
                warn(f"sludge.py exited {proc.returncode} for #{c['n']} {c['slug']}")
                return
            c["out"] = str(out)
            if not args.no_strip:
                # Sample the BODY, not the finished file: the CTA is a full-frame tail
                # (~6s) and sampling by fraction of the total spends a frame of the
                # strip on the logo instead of on a cut that might be letterboxed.
                body = max(ffprobe_duration(out) - cta_len, 1.0)
                cuts, times = rendered_shot_times(out, body)
                per_cut = bool(times)
                if not per_cut:
                    # One continuous shot (or scdet found nothing): the strip becomes a
                    # head-lock drift check instead of a per-cut identity check.
                    times = [body * f for f in (0.1, 0.5, 0.9)]
                c["render_shot_cuts"] = cuts
                c["strip_times"] = times
                c["strip_per_cut"] = per_cut
                strip = frame_strip(out, ws.verify / f"{c['n']:02d}-{c['slug']}-strip.png", times)
                if strip:
                    c["frame_strip"] = str(strip)
                    what = (f"one frame per cut ({len(times)} shot(s), cuts at "
                            f"{cuts})" if per_cut else
                            f"{len(times)} frames — scdet found no cut in the top pane")
                    log(f"frame strip -> {strip}  [{what}]  READ IT: same person, "
                        f"same background, same head size in every frame")

        if args.render_jobs > 1 and not args.dry_run:
            with ThreadPoolExecutor(max_workers=args.render_jobs) as pool:
                list(pool.map(render_one, picks))
        else:
            for c in picks:
                render_one(c)

    payload = json.dumps(result, indent=2)
    ws.candidates.write_text(payload)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(payload)
    print(payload)
    return 0


def median_gap(times: list[float]) -> float | None:
    """None, not inf: json.dumps turns inf into the literal `Infinity`, which is not
    valid JSON and is rejected by every parser stricter than Python's."""
    if len(times) < 2:
        return None
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    return round(gaps[len(gaps) // 2], 2)


def shell_quote(s: str) -> str:
    return s if re.fullmatch(r"[\w@%+=:,./-]+", s) else "'" + s.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        die("interrupted")
