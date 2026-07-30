---
name: b:sludge
description: |
  MANUAL TRIGGER ONLY: invoke only when user types /b:sludge.
  Render sludge content: 50/50 vertical split, head-locked talking head on top,
  random clip on the bottom, ducked music bed, a broadcast vocal chain on the voice,
  and word-highlighted live captions synced to the speaker.
argument-hint: "[message] @head @clip [@music] [@cta]"
disable-model-invocation: true
---

Render a [sludge content](https://en.wikipedia.org/wiki/Sludge_content) video — the
1080x1920 two-pane format built to hold attention: a talking head speaking the
message on top, unrelated stimulation on the bottom, karaoke captions across the
seam.

Everything is done by `scripts/sludge.py`. Your job is to resolve the arguments,
run it, look at the result, and report back.

## Invocation

```
/b:sludge [message] @head @clip [@music] [@cta]
```

- **`@head`** — the talking head video. Its audio becomes the voice track and
  drives caption timing. It is EQ'd, compressed and loudness-normalised on the way
  through so the voice carries on a phone speaker; `--no-voice-polish` uses it raw.
- **`@clip`** — the bottom filler clip (subway surfers, gameplay, slime, whatever).
  Looped if shorter than the head, started at a random offset. **Its own audio is kept
  and stays audible under the voice** (gently ducked), levelled to sit exactly on the
  ducked `@music` bed so the two read as one background; `--mute-clip` drops it. Many
  downloaded clips are silent — the log says so explicitly when there is no audio
  stream to hear.
- **`@music`** — optional music bed, ducked under the voice alongside the clip.
  Looped if short, fades in and out.
- **`@cta`** — optional call-to-action video, appended full-frame after the
  split-screen body. With `@music`, the CTA cut is landed on a beat and the bed's drop
  is lined up with it, and the ducking stops there.
- **`[message]`** — optional caption text, the editorial target for choosing cuts,
  and where `*emphasis*` is marked. Omit it and captions come from the transcript.

With `@music`, the closing words also get a beat-matched delay that rings out over the
CTA cut — `--echo-words` controls how many, `--no-echo` turns it off.

**Argument order does not matter** — resolve by type, not position:

- an audio-only file (`.mp3`, `.m4a`, `.wav`, `.aac`, `.flac`) is the `@music` bed
- of the videos, the head is the one with speech (`ffprobe -show_streams`); if
  several have audio, the head is usually the portrait/vertical one
- a short branded/outro video, or one the user calls a CTA, is `@cta`
- everything that isn't a path is the message

Ask only when two videos both have speech and the filenames give you nothing.

## Two ways to run it

**Straight render** — the head video is already the take you want. Silences get cut
automatically; you just render. Use this when the head is short (under ~20s) or the
user says "just use the whole thing".

**Plan first** — the head is a long take and the message is a specific claim. Then
you choose which parts survive, which is the editorial work described below. Use
this whenever the head is longer than about 30s, or the user's message is narrower
than what the video says.

## Run it

The script is self-executing — the shebang uses `uv run --script`, so OpenCV and
NumPy install into an ephemeral environment on first use.

```bash
<skill-dir>/scripts/sludge.py \
  --head /abs/path/head.mp4 \
  --clip /abs/path/clip.mp4 \
  --music /abs/path/bed.mp3 \
  --cta /abs/path/cta.mp4 \
  --message "compound interest turns *\$40* a week into a million dollars" \
  --out /abs/path/out.mp4
```

Drop `--music` / `--cta` if those weren't given. Wrap words in `*asterisks*` to give
them the solo treatment (see Emphasis below).

Omit `--out` and it writes `sludge-<timestamp>.mp4` to the current directory.
Prefer writing next to the head video unless the user said otherwise.

## Cutting for the message

This is the part that needs your judgment, not the script's. Two steps:

**1. Get the candidates.**

```bash
scripts/sludge.py --head head.mp4 --clip clip.mp4 \
  --message "the claim we are making" --plan --target-duration 25
```

That prints JSON and renders nothing: every transcript segment with `start`, `end`,
`text`, and a `score` (message-word overlap, speech density, filler penalty), plus a
`suggested` selection. **The score only measures message-fit — it cannot tell what is
funny, contradictory, quotable, or damning. Read the `text` and overrule it.**

**2. Write the edit and render it.**

```bash
cat > /tmp/edl.json <<'EOF'
{"cuts": [
  {"start": 5.28,  "end": 9.04,  "label": "hook"},
  {"start": 19.64, "end": 22.86, "label": "stat"},
  {"start": 15.28, "end": 18.48, "label": "payoff"}
]}
EOF
scripts/sludge.py --head head.mp4 --clip clip.mp4 --edl /tmp/edl.json \
  --message "..." --out out.mp4
```

Audio, captions and the head lock all follow the cuts automatically. `--edl`
overrides `--start` / `--duration`.

### How to choose — be opinionated

- **Hook first.** The strongest claim leads, even if it was said last. Cut order is
  honoured, so reordering is not just allowed, it is the main lever you have.
- **Kill the preamble.** "So, um, I guess I should introduce myself" — nobody stays
  through setup. Greetings, throat-clearing and context go first.
- **Cut filler segments outright.** High `filler`, low `overlap`, no specifics: gone.
- **Keep the concrete.** Numbers, dollar amounts, percentages, named contrasts,
  before/after. Drop hedges ("I think maybe it's sort of").
- **One idea per cut.** If a segment carries two thoughts, cut the weaker half.
- **Escalate, then stop.** hook → evidence → payoff. End on the strongest line with
  no trailing tail; dead air at the end is worse than a hard stop.
- **4–8 cuts** feels alive. 1–2 feels like an unedited take, more than ~12 stops
  being coherent.
- **15–30s total.** If the good material only adds up to 12s, ship 12s. Never pad.
- **Never split a sentence.** The `--plan` boundaries already sit on pauses and
  punctuation — start from those rather than inventing timestamps.

Note that reordering can misrepresent what someone said. When the head footage is
of a real person making an argument, reorder for pacing, not to invert their
meaning — and say what you rearranged when you report back.

Expect roughly: a few seconds of face detection, then whisper transcription (the
slow step — tens of seconds; cached per input file in `~/.cache/b-sludge`), then
a realtime-ish render. Progress goes to stderr; the final line on stdout is the
output path.

## What the pipeline does

1. **Head lock (top pane).** Detects the face on *every* frame — YuNet (a small DNN,
   model auto-downloaded and cached; Haar cascades offline) — and anchors on a point
   between the eyes and mouth, the stillest point on a talking face. Position and
   size are filtered separately (position lightly, size heavily, both zero-phase so
   nothing lags), then each output frame is an affine warp that pins the face to a
   fixed point at a fixed size. The head sits still; the background moves around it.
   Detector dropouts are bridged by template matching for up to 8 frames, then the
   track interpolates. No detectable face anywhere falls back to a static crop.
2. **Jump cuts.** Silences longer than `--max-gap` (0.35s) are cut out of every
   segment, leaving a little air around each speech run — this is what makes a
   talking head feel fast. Each surviving cut also gets a different framing from a
   punch-in cycle, because an identical reframe either side of a cut reads as a
   glitch rather than an edit. Cuts are frame-aligned, and picture, voice and
   captions are all built from the same boundaries.

   Every cut is then held open until its own last word has finished, plus `--tail-pad`
   (0.25s). Cut boundaries come from word ends, so any imprecision there is audible as
   a chopped-off word — the guard is what stops that, and the beat matcher is not
   allowed to shorten a cut back past it.
3. **Bottom pane.** The clip is scaled to cover 1080x960, centre-cropped, looped
   to length, and started at a random (seedable) offset.
4. **Captions.** Whisper returns word-level timestamps, then **both ends of every word
   are snapped onto measured speech**. Whisper is biased early on both sides, and each
   side breaks something different:

   | | raw whisper error | after refinement | what it broke |
   |---|---|---|---|
   | word starts | −156 to −417 ms | −22 to −27 ms | highlight fires before the word |
   | word ends | −260 to −355 ms | −2 to −7 ms | jump cuts clip the closing word |

   Timings are then remapped onto the cut timeline, so highlights stay on the
   speaker across every jump cut and reorder. If a message was given,
   its words are aligned to the transcript with `difflib`, so the highlight lands
   on the word actually being spoken; unmatched runs interpolate between anchors.
   A message that diverges too far from the audio (<60% matched) is paced evenly
   across the speech span instead of bunching at the anchors. Rendered as an ASS
   file with one event per word — the active word turns yellow and scales up.
5. **Vocal chain.** Phone and camera mics record a thin, uneven voice, and on a phone
   speaker that reads as amateur no matter how well the picture is cut. The head's audio
   goes through a broadcast-style chain: rumble filtered out, chest weight added at
   140 Hz and a wide low shelf, a dip at 1.15 kHz to clear the boxy midrange, presence
   at 4.2 kHz plus a high shelf and air at 8.5 kHz for intelligibility on a small
   speaker, then two compressors — a fast one (12ms/160ms) to even out syllables and a
   slow one (200ms/1.5s) to even out the take — and a limiter. Finally `loudnorm` to
   `--voice-lufs` (−14 LUFS, the streaming standard).

   EQ and dynamics run on the **uncut** take, ahead of the trims: the compressors need
   continuous context to settle, and running them per-cut would let each piece land at
   its own level and pump at every seam. The loudness pass is the exception and waits
   until **after** the concat, so its moving gain follows what is actually heard —
   normalising first would leave a gain step at every cut, fitted to material that is
   no longer there.

   Measured on a 3-cut render with a bed: **−13.0 LUFS integrated, LRA 4.4 LU, true
   peak −4.3 dBFS** — consistent and loud with headroom to spare. The same render
   with `--no-voice-polish` sits 7.8 LU quieter. Because the voice is the loudest thing
   in the mix and the bed and clip sum in above it, raising `--voice-lufs` also raises
   the whole file; `--no-voice-polish` uses the audio exactly as recorded.
6. **Ducked background.** The clip's own audio and the music bed both sit under the
   voice, each with its own `sidechaincompress` keyed off the same voice — real dynamic
   ducking, so they drop while someone talks and recover in the gaps instead of sitting
   at a fixed level. Sharing one key means they still move in lockstep and cannot pump
   against each other.

   They duck by different amounts on purpose. The music can drop a long way and still
   do its job (`--duck-ratio 9`, measured −11.8 dB), but the clip is the bottom pane's
   own noise and has to stay audible or the pane feels dead (`--clip-duck-ratio 3`,
   measured −8.6 dB). After jump cuts the body is speech almost end to end, so a
   deeply ducked clip is effectively inaudible for the whole video — which is why it
   gets the gentler treatment.

   **The clip is levelled to the ducked bed.** The ear reads the clip and the bed as
   one background layer, so a mismatch between them just sounds like a mistake. The
   gentler ratio means the clip keeps more of its level, so left at equal volumes it
   floats above the bed. `--clip-volume` is therefore solved for rather than fixed: a
   compressor takes a signal `overshoot * (1 - 1/ratio)` dB down, where the overshoot
   is how far the voice sits above `--duck-threshold`, and since both branches share
   one key and one threshold the whole difference is the ratio. Handing that
   difference back as attenuation puts them on top of each other.

   This is only reliable because the vocal chain pins the voice to a known loudness —
   against a raw take (`--no-voice-polish`) the overshoot is whatever the recording
   happened to be, so it is measured with `ebur128` instead. When the voice never
   clears the threshold, or with `--no-duck`, the overshoot is zero and it falls out
   to simply matching the bed's level.

   Measured with identical pink noise on both branches, so the numbers are directly
   comparable. Defaults, bed at `0.45`/ratio 9 → clip solved to `0.331`:

   | | during speech | in gaps |
   |---|---|---|
   | clip | −42.2 dB | −33.6 dB |
   | bed | −42.2 dB | −30.4 dB |

   Dead level through speech, which is nearly the whole body. In the gaps the bed
   swells 3.2 dB past the clip — the deeper ratio recovering further — which is the
   bed doing its job in the pauses. It generalises: at `--music-volume 0.7
   --duck-ratio 5` the clip solves to `0.582` and lands within 0.2 dB. Passing
   `--clip-volume` explicitly opts out and pins it. With no bed there is nothing to
   match and it stays at `0.4`.

   The final mix goes through `alimiter` so summing can't clip.
7. **CTA tail, beat-matched.** `@cta` is normalised to a full-frame 1080x1920 clip and
   concatenated after the body. Captions are burned before the concat, so they can't
   bleed onto it. The voice track ends with the body, which is what makes the ducking
   stop at the CTA — no special-casing needed.

   With `@music`, the cut is put on the music rather than wherever the last word
   happened to land. The bed's tempo and beat phase are estimated (onset envelope →
   autocorrelation for the period → pulse-train correlation for the phase), the CTA cut
   is moved onto the nearest beat by trimming or extending the last cut by a few frames,
   and then the bed is offset or delayed so its drop lands on that same beat. The
   detected drop is itself snapped to the grid first, since drops fall on beats.

   Measured on a 120 BPM click track with a drop at a known 12.000s: tempo recovered as
   120.0 BPM, and a click lands **23 ms (0.7 frames)** from the CTA cut. That is at the
   floor — a cut can only land on a 33 ms frame boundary, so ±17 ms is the best possible.
   Shrinking the last cut is preferred over extending it, because the source may not
   have more frames. If no steady tempo is found, the script says so and skips it.
8. **Tail echo.** The closing words of the voice get a delay whose interval is a note
   value at the bed's tempo, so the repeats fall in time with the music. Only the tail
   is fed to the delay and the repeats are placed back where they came from, so the dry
   words are untouched and the echo **rings out over the CTA cut** rather than stopping
   at it. The echo deliberately does *not* key the ducking — otherwise the music would
   stay held down during exactly the moment it is supposed to open up.

   Each repeat is built explicitly rather than with `aecho`, which would pass the dry
   signal through as well and quietly make the closing words louder than the rest of
   the take. This branch is repeats only — verified at 0.00 dB difference on the dry
   word.

   Measured with a single click as the voice at 120 BPM: 4 repeats at exactly **+250,
   +500, +750, +1000 ms** (the 1/8 note), at −4.4, −8.6, −12.7 and −16.9 dB. **The last
   two land past the CTA cut**, still clearly audible, which is the overlap the effect
   exists for. On by default when there is music; with no tempo available it falls back
   to a fixed delay and says so. The log prints how far the tail rings and how much of
   that is over the CTA.
9. **Composite.** The warped top pane is piped straight into the final encode (no
   intermediate generation), `vstack`ed with the bottom pane, captions burned over
   the seam, voice padded to the full duration, H.264 + AAC, `+faststart`.

The top pane is decoded through ffmpeg rather than OpenCV so that rotation metadata
is honoured — phone footage would otherwise be tracked in the wrong orientation.

## Emphasis

An emphasised word leaves its group, fills the screen alone at 145%, turns the
highlight colour, and shakes — a decaying jitter of position and rotation, re-emitted
every ~45 ms, that hits hard and settles rather than buzzing throughout.

Mark them two ways, and both can be used at once:

- **`*asterisks*` in the message** (or `--emphasize "word,word"`) — explicit, always wins.
- **automatic** — numbers, money and percentages (`$40`, `90%`, `10x`). These are the
  lines people screenshot and they read instantly with the sound off. `--no-auto-emphasis`
  turns this off.

Use it on 1–3 words per video. Everything emphasised is nothing emphasised, and each
solo word costs a beat of reading time that the rest of the sentence no longer gets.

## Transcription and typos

Captions come from **openai-whisper**, default model **`large-v3-turbo`** (1.5 GB,
downloaded once to `~/.cache/whisper`). Available: `tiny`, `base`, `small`, `medium`,
`large-v3-turbo`, `large-v3`, plus `.en` variants.

**The message is fed to whisper as `--initial_prompt`.** This is the single biggest
lever on typos, and it costs nothing. Measured on deliberately hard audio (tickers and
jargon), 18s of speech on CPU:

| model | time | result |
|---|---|---|
| `small`, no prompt | 6.8s | `VTS-AX`, `FX-AX`, "**Kuda Mode**" for CUDA moat |
| `small`, primed | 6.8s | **every word correct** |
| `small.en`, no prompt | 14.0s | "Invidia Kudemota YAMD" — worst of all |
| `large-v3-turbo`, no prompt | 23.5s | `FX AIX`, "CUDA **mode**" |
| `large-v3-turbo`, primed | ~24s | correct |

Two things follow, both counterintuitive enough to be worth remembering: **priming beats
upgrading the model**, and the **`.en` models are not better** — they were the worst on
proper nouns despite the English-only specialisation.

So when the user reports typos:

1. **Give the words to `--vocab`** — names, tickers, product names, handles. This is the
   first move, not the last. `--vocab "Anthropic, Claude, MCP, Cerebras"`.
2. Make sure the message actually contains the terms; it is already used as the prompt.
3. Only then reach for a model change (`--model large-v3` for genuinely hard audio,
   `--model small` when you want a ~3x faster pass on clean audio).
4. `--no-prime` disables priming, e.g. to see the unbiased transcript.

Note that when a message is supplied and matches the audio, **the caption text is the
message's spelling**, so typos never reach the screen for covered words — transcript
typos only surface where the message doesn't cover what was said.

## Captions are always word-synced

Caption timings always come from the audio, never from an assumption about pacing:

- With no message, captions are the transcript.
- With a message that matches the audio, message words inherit the transcript's
  timings (so your `$40` spelling survives) and unmatched words interpolate between
  neighbours.
- With a message that **doesn't** match (below 60% of words), there is nothing to sync
  to — so the script captions the transcript instead and logs that it did. Forcing the
  message with `--caption-source message` paces it evenly, which will visibly drift;
  only do that if the user asks for the message text specifically.

If the user says captions are late or early across the board, use `--caption-offset`
(seconds, + = later). If they're wrong only in places, that's a transcript problem —
see Transcription above.

## Verify before reporting

Always eyeball the output — the lock and the captions are what go wrong:

```bash
for t in 0.6 2.5 5.0; do
  ffmpeg -v error -y -ss $t -i out.mp4 -frames:v 1 \
    -vf "crop=1080:960:0:0,scale=250:-1" "/tmp/fr_$t.png"
done
ffmpeg -v error -y -i /tmp/fr_0.6.png -i /tmp/fr_2.5.png -i /tmp/fr_5.0.png \
  -filter_complex hstack=inputs=3 /tmp/strip.png
```

Read `/tmp/strip.png`. That crop is the top pane only, so a working lock shows the
head at the *same position and size* in all three frames — that side-by-side strip
is the check, since a single frame can't show whether the head is still. Then read a
full frame too, for the captions: one word highlighted, nothing clipped at the edges.

When you cut, also check the edit actually says what you intended — cuts can join
two half-thoughts into something the speaker never said:

```bash
ffmpeg -v error -y -i out.mp4 -vn -ac 1 -ar 16000 /tmp/out.wav
whisper /tmp/out.wav --model small --language en --output_format txt --output_dir /tmp
```

Read `/tmp/out.txt`. It is the finished video's own audio, so it is the edit as an
audience hears it. If it reads as a non-sequitur, fix the EDL, not the captions.

That same transcript is the check on the music bed: it is taken from the mixed
output, so if words come back garbled or missing that were clean without `--music`,
the bed is burying the voice — lower `--music-volume` or raise `--duck-ratio`.

Report the output path, duration, which segments you kept and in what order, and
anything the script warned about (detector coverage below ~90%, no faces found,
words past the cut, low caption alignment).

## Tuning

Reach for these when the check above looks off, or when the user asks:

| Symptom | Fix |
|---|---|
| Head too small / too tight | `--face-height 0.55` / `0.35` (face height as a fraction of the pane; default 0.45) |
| Head sits too low or too high | `--face-y 0.35`–`0.5` (fraction of the pane) |
| Blurred fill showing at the edges | `--headroom 1.4` (zooms in for more pan room), or `--edge clamp` to keep the window inside the frame and let the lock loosen instead |
| Lock feels twitchy | `--tightness 0.4` |
| Head floats instead of locking | `--tightness 0.9` |
| Zoom pulses or lags | `--scale-smooth 0.4` (snappier) / `0.08` (calmer) |
| Want a lazy pan, not a lock | `--lock smooth` (add `--zoom 1.4`, `--smooth 0.06`) |
| Want no reframing at all | `--lock none` |
| Detector missing the face | `--preview-track` writes `<out>.track.mp4` with boxes + anchor; check coverage in the log |
| Cartoon / stylised / non-photo face | `--detector haar` (YuNet only knows real faces) |
| Offline machine | `--detector haar`, or `--face-model path/to/yunet.onnx` |
| Pacing still feels slow | `--max-gap 0.15` (cuts tighter into the pauses) |
| Cuts feel choppy / clipped words | `--max-gap 0.6 --cut-pad 0.12` |
| Wants one continuous take | `--no-jump-cuts` |
| Framing changes are distracting | `--no-punch-in` |
| Too many tiny fragments | `--min-cut 0.8` |
| Last word sounds clipped | `--tail-pad 0.5` (default 0.25s of air after every cut's last word) |
| Too much dead air at cut ends | `--tail-pad 0.1` |
| Echo should ring longer over the CTA | `--echo-repeats 6 --echo-decay 0.7`, or `--echo-note 1/4` |
| Voice sounds thin / far away | already on by default; check the log says the voice was polished |
| Voice sounds harsh or sibilant | `--no-voice-polish` (the presence and air boosts are what bite) |
| Whole file too loud / too quiet | `--voice-lufs -16` / `-12` (the voice is the mix's reference level) |
| Voice over-processed / pumping | `--no-voice-polish` |
| Bed too loud / too quiet | `--music-volume 0.25` / `0.7` |
| Bed still fighting the voice | `--duck-ratio 15 --duck-threshold 0.02` |
| Ducking too obvious / pumping | `--duck-ratio 4` |
| Bed should stay at one level | `--no-duck` |
| Wrong part of the song | `--music-offset 42` (overrides drop and beat sync) |
| Abrupt music in/out | `--music-fade 1.2` |
| Can't hear the clip's audio | `--clip-volume 0.6 --clip-duck-ratio 1.5` (and check the log — a silent source clip is reported) |
| Clip audio too loud | `--clip-volume 0.2` (drops the auto-match to the bed) |
| Clip and bed feel like different layers | already matched by default; the log prints the solved level |
| Clip audio unwanted | `--mute-clip` |
| Drop lands on the wrong beat | `--drop-at 18.5` (its time in the bed), or `--no-sync-drop` |
| Tempo detected wrong | `--bpm 128` (assumes the first beat is at 0:00) |
| CTA should cut where the words end | `--no-beat-match` |
| Echo too subtle | `--echo-mix 0.85 --echo-decay 0.65` |
| Echo muddying the words | `--echo-words 2 --echo-repeats 2 --echo-mix 0.4` |
| Echo feels off the beat | `--echo-note 1/4` or `1/8.` (dotted), or `--bpm` if the tempo is wrong |
| No echo wanted | `--no-echo` |
| CTA too long | `--cta-duration 3` |
| CTA audio clashing with the bed | `--cta-volume 0` |
| Captions uniformly early/late | `--caption-offset 0.15` |
| Emphasis too big / too jumpy | `--emphasis-scale 1.2 --shake-px 8` |
| No shaking wanted | `--no-shake` |
| Too many words emphasised | `--no-auto-emphasis` |
| Typos in names / tickers / jargon | `--vocab "Term, Other Term"` — see Transcription below |
| Captions garbled generally | `--model large-v3`, or `--language auto` if not English |
| Transcription too slow | `--model small` (~3x faster; prime it and it holds up) |
| Too much text on screen | `--caption-words 2 --caption-chars 18` |
| Captions in the way | `--caption-pos top` / `bottom` (default `center`, over the seam) |
| Different look | `--highlight-color '#00FF66' --font Impact --font-size 96 --no-uppercase` |
| Want the clip audible | `--keep-clip-audio --clip-volume 0.18` |
| Deterministic bottom clip | `--seed 42` or `--clip-offset 12.5` |
| Trim the head | `--start 3 --duration 20` |
| Visible divider | `--seam 6` |

Full list: `scripts/sludge.py --help`.

## Requirements

- `ffmpeg` / `ffprobe` with libass and libx264 (`brew install ffmpeg`)
- `uv` (runs the script and its OpenCV dependency)
- `whisper` on PATH, else the script falls back to `uvx --from openai-whisper whisper`
- Network on first run, for the 230 KB YuNet face model (cached in `~/.cache/b-sludge`)
  and the 1.5 GB `large-v3-turbo` whisper model (cached in `~/.cache/whisper`). Without
  network the script says so and uses Haar cascades; whisper needs its model present, so
  pass `--model small` if only that one is cached.

## Measured lock quality

On a 5s 1080x1920 test clip whose face moves 419x452px and changes size by 42%,
tracking the face position in the rendered top pane:

| | face centre sd | face size sd |
|---|---|---|
| source, unlocked | 146 x 134 px | 16% |
| `--lock none` | 183 x 141 px | 18% |
| `--lock smooth` | 74 x 15 px | 14% |
| `--lock tight` (default) | **3 x 7 px** | **1.8%** |
| `--lock tight --detector haar` | 5 x 5 px | 5.9% |

Haar holds position but not size, because its box height is a much noisier scale
signal than YuNet's — that is why it needs heavier `--scale-smooth`, and why YuNet
is the default.

If the user wants to skip transcription entirely, `--no-captions` renders the
split-screen with no text.
