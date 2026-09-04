---
name: sludgify
description: |
  MANUAL TRIGGER ONLY: invoke only when user types /b:sludgify.
  Point at a YouTube or X video and ship finished sludge clips — the ones that
  continue the narrative the existing corpus already promotes. sludgify decides
  WHAT to clip; b:sludge renders it.
argument-hint: "<video-url|file> [--emit N] [--render --clip … --music … --cta …]"
disable-model-invocation: true
---

Mine a long video for the clips worth shipping, then render them through
[b:sludge](../sludge/SKILL.md).

b:sludge already answers "how do I render a sludge video". It cannot answer "which
20 seconds of this two-hour podcast belong on the account". That is this skill, and
the answer is not "the most quotable line" — it is **the line that continues an
argument 76 published clips have already been making**. That corpus is measured into
`corpus-profile.json`, and `scripts/sludgify.py` scores every span in the source
against it.

The real pipeline this automates, which the user was doing by hand:

```
raw/{show}.mp4  →  raw/{xx}-segs/{prefix}-{NN}-{slug}.mp4  →  out/sludge-{speaker}-{slug}.mp4  →  out/to-pipeline/  →  out/shipped/
   1.7 GB episode      hand-cut per-topic segment              one b:sludge render each          curated survivors
```

sludgify automates the middle stage. Segments are still cut physically before
rendering — that is what keeps whisper off a 2-hour file, makes the per-cut frame
check tractable, and lets a crop be applied once per episode.

**Your job is the middle of it.** Run the miner, *read the candidates*, kill the ones
that are off-narrative or in the wrong mouth, write the slug, render, look at the
frames, report. The script produces facts. Every decision that would embarrass the
user if it were wrong is yours.

---

## Invocation

```
/b:sludgify <video-url|path> [flags]
```

One positional argument: a YouTube or X URL, or a local video file. A bare URL is a
complete invocation — everything else has a default from the profile.

```bash
<skill-dir>/scripts/sludgify.py "https://youtu.be/XXXX" --plan
```

The script is self-executing (`uv run --script`); OpenCV and NumPy install into an
ephemeral environment on first use. Progress goes to stderr, one JSON document to
stdout. Exit 0 on success, 1 on error.

The flags you will actually reach for:

| flag | default | what it does |
|---|---|---|
| `--plan` | **on** | mine and print candidates, render nothing |
| `--extract` | off | also cut each pick out of the source into `<work>/clips/` |
| `--render` | off | extract, then run `sludge.py` per pick |
| `--pick 2,5` | picks | render exactly these candidate numbers, after you have read them |
| `--trim A-B` | — | replace the shortlist with ONE span cut from source seconds A–B, snapped to whole sentence units. This is how you act on `ends_mid_clause`, drop a host turn off the front, or land on the thesis |
| `--emit N` | auto | how many picks to mark (auto: ~1 per 8–10 min, clamped 1–12) |
| `--oversample N` | `2` | shortlist `emit × oversample`, so there is something to choose between |
| `--speaker NAME` | inferred | speaker key for `sludge-{speaker}-{slug}.mp4` |
| `--out-dir DIR` | cwd | where rendered mp4s land |
| `--clip PATH` | — | bottom filler video, **required by `--render`** |
| `--music PATH` / `--cta PATH` | — | bed and tail, passed through |
| `--dry-run` | off | with `--render`, print the `sludge.py` argv instead of running it |
| `--dump-pool` | off | include every scored span, not just the shortlist |
| `--force STAGES` | — | recompute `download,audio,transcript,cuts,clips,all` |

House assets for this project, resolved relative to the working directory:
`--clip clip-nibs.mp4 --music tiktok/red-aura-funk-21.mp3 --cta cta.mp4`. They are
paths, not conventions — the script does not scan for them, and `--render` without
`--clip` dies before it transcribes anything.

Full list: `scripts/sludgify.py --help`.

**Output naming is not negotiable:** `out/sludge-{speaker}-{slug}.mp4`. That is the
corpus convention and the downstream pipeline (`out/to-pipeline/`, `out/shipped/`)
reads it.

### Two ways to run it

**Shortlist first** (`--plan`, the default) — a new show, a panel, a multicam
podcast, or anything over ~40 minutes. You read the candidates, decide, then come
back with `--render --pick`. This is the normal path.

**Straight through** (`--render`) — a source you have clipped before, one speaker,
one camera. Even then, read the shortlist that prints on the way past.

---

## The house narrative — read this before choosing anything

The corpus is not "crypto clips". It is a specific argument, and the *absences* prove
it: across all 4,656 corpus words, regex probes for Fed/rates/inflation, SEC/
regulation, price targets, technical analysis, tokenomics, scams/rugs/hacks, Bitcoin
maximalism, ZK/DePIN/validators, AI-as-technology, lifestyle flex and institutional
desk mechanics each return **0 hits**. Arthur Hayes is in this corpus four times and
**not once for his liquidity-macro thesis** — only for craft and dedication. That is
an editorial filter, not a topic sample.

**Twelve themes**, ordered by corpus mass. Full lexicons and exemplars live in
`corpus-profile.json` under `.themes`.

| # | theme | the claim | primary clips |
|---|---|---|---|
| 1 | `apps_over_infra` | The L1 cycle is over. Value is in real apps with real revenue — and almost nobody is building good ones. | 15 |
| 2 | `everything_tradable` | The crypto/TradFi line is gone. Perps, prediction markets, equities, 24/7. The venue with the liquidity wins. | 13 |
| 3 | `traders_as_protagonist` | Trading is a public identity and a status ladder. The trader replaced the founder as the culture's protagonist. | 6 |
| 4 | `risk_is_a_craft` | Leverage is legitimate when earned with work and stated risk. The shame is being casual, not being levered. | 8 |
| 5 | `trading_as_daily_habit` | Trading becomes an everyday app behaviour — and the interface has not been built yet. | 6 |
| 6 | `psychology_of_the_hit` | Traders are addicted to being right, not to money — and enough never arrives. | 5 |
| 7 | `ai_as_market_regime` | AI matters only because it makes everything unpredictable. Never as technology to admire. | 6 |
| 8 | `volatility_is_the_product` | People show up for volatility. Dampening it kills the market. | 4 |
| 9 | `retail_can_win` | Ordinary people can beat the market. S&P-and-chill is passivity. | 4 |
| 10 | `noise_vs_ground_truth` | The crowd trades noise. The edge is independent observation, and it is not mysterious. | 4 |
| 11 | `unloved_moment` | Negative sentiment is when the best opportunities appear. | 2 |
| 12 | `raw_pnl_over_ideology` | Results are the only credential. Verified money is the scoreboard. | 3 |

`raw_pnl_over_ideology` has few *primary* clips and is still the ideological spine —
it works as a **secondary** theme almost everywhere. The scorer's second-theme bonus
is what surfaces it.

**What the corpus believes:** trading is the main event of the culture, not a
sideshow. The scoreboard is verifiable P&L, not followers or credentials. Crypto was
always about money. Apps win, chains are commodity. Volatility is the product.
Retail belongs here and can win — with work. Leverage is honorable when earned. The
barrier is attention, not credentials. Everything converges into one 24/7 tradable
surface. Ordinary observation beats institutional research. The interface hasn't been
built yet. Honest confession of motive is in character.

**What it dismisses:** decentralization ideology and maxis. Infrastructure discourse
("the L1 bullshit"). Institutions as a truth-source. Passive indexing as an identity.
Token incentives as a substitute for product. Influencer clout without P&L. Emotional
attachment to positions. Pure gambling outside markets. Mystification of trading.
Doomerism.

### Three tensions to hold, not resolve

The corpus contradicts itself on purpose. Rejecting one side because it contradicts
the other is the most likely way to mis-mine a source.

| tension | how the corpus reconciles it | what breaks if you "fix" it |
|---|---|---|
| Anti-gambling vs pro-speculation | **Work.** Speculation with research and stated risk is craft; a slot machine is not. | Treat "gambling" as negative vocabulary and you reject legitimate `risk_is_a_craft` clips; treat it as positive and you ship sports-betting content. No regex captures this. |
| "Retail can win" vs "retail is bad at calling the top" | **Agency.** Retail-as-individual-who-does-work wins; retail-as-herd is wrong at extremes. | Both sides are on-narrative. |
| Casual daily habit vs full-time dedication | Both endorsed. What is never endorsed is **casual leverage**. | The contradiction is the position, not an inconsistency. |

### Rhetorical shapes, by corpus frequency

Contrarian reversal 16 · second-person instruction 14 · prediction 14 · named contrast
10 · insider mechanism 9 · before/after progression 7 · confession 6 · rant 6 ·
permissioned→permissionless 5.

**The dominant combination is reversal + second-person instruction:** state the
consensus, flip it, point at *you*. 56/76 clips carry second-person address at 3.83
"you" per 100 words; 45/76 carry a negation. A span braiding two shapes beats one
that maxes out a single shape.

---

## What the miner does

Every stage writes one file into `~/.cache/b-sludgify/<source-key>/` and is skipped
when that file exists, so re-running with different mining knobs costs seconds, not
minutes. Measured on `raw/hayes.mp4` (126 s): 9.5 s cold, **4.4 s warm**. On a 1,839 s
multicam panel: 81 s cold, **0.28 s warm**.

1. **Fetch and cache.** yt-dlp for a URL; a local file is symlinked. Keyed by source,
   so `meta.json` / `source.mp4` survive every re-run.
2. **Transcribe the whole source, unprimed.** whisper.cpp at 30–38x realtime
   (`ggml-small.en.bin`), falling back to chunked openai-whisper at ~2.2x.
   **There is no message on the scan pass, so priming is not even available** —
   lesson 2 is structurally impossible here. It becomes live again at render time.
3. **Re-segment into sentence units.** whisper.cpp segments on a *time* budget, not
   sentences: measured **11%** of hayes segments and **49%** of a panel's segments
   ended on terminal punctuation. That collapses span growth to the duration clamps
   and lands every candidate mid-thought. The re-segmenter splits on the punctuation
   already inside the text and maps char ranges back to times by interpolation —
   **11% → 100% and 49% → 100%**. This is the single biggest quality lever in the
   script. `--no-resegment` disables it.
4. **Grow spans and score them** against the 12 themes: theme fit 30 pts, stance 20,
   second-person address 12, concreteness 12, claim shape 12, delivery 8, duration fit
   6, then multiplicative vetoes and two gates. Everything is read out of
   `corpus-profile.json` — change the profile, change the taste, no code edit.
5. **Shot-cut scan.** `ffmpeg` scene detection at `--scene-threshold 0.4`, retaining
   every score above 0.1 so the threshold can be re-derived from cache without
   rescanning (verified: 0.4 → 0.2 in 0.28 s).
6. **Shot-safety probe.** 12 frames per candidate, face census per frame.
7. **Extract** each pick with `-c:a copy` (`--extract`).
8. **Render** each pick through `sludge.py` and write a frame strip (`--render`).

### The score is calibrated, and the calibration is auditable

Scored against the 76 corpus clips and 19 hand-written negative controls (macro,
price target, regulation, tokenomics, TA, scam drama, Bitcoin maxi, ZK infra, AI tech,
institutional, housekeeping, host question, filler, anecdote, flex, generic startup
advice, crosstalk, and two on-topic-but-empty near-misses):

| | n | min | p25 | median | p75 | max |
|---|---|---|---|---|---|---|
| corpus | 76 | 14.3 | 38.9 | **44.9** | 50.3 | 69.2 |
| negatives | 19 | 4.5 | 6.1 | 9.7 | 14.0 | 29.2 |

At the reject floor of 30: **86.8% corpus recall, 100% of negatives rejected.**

| band | score | action |
|---|---|---|
| reject | < 30 | do not clip |
| shortlist | 30–38 | consider only if the frame-check loves it |
| strong | 38–45 | normal candidate — corpus p25 to median |
| flagship | ≥ 45 | corpus upper half — lead with these |

The top-scoring corpus clips are the ones a human would call canonical —
`apps-over-l1s` 69, `name-one-good-product` 67, `nobody-can-predict` 66,
`we-love-volatility` 63. That the ranking recovers the user's own best picks is the
evidence the rubric encodes the right thing.

**Score parity is asserted, not assumed.** The script reads every weight, regex, veto
and threshold out of the JSON rather than hardcoding them, but two grammar regexes
live in the script. `--self-test` is what catches divergence:

```bash
scripts/sludgify.py --self-test --manifest /path/to/corpus.json
# profile … v1.0.0  fingerprint 9b63ae40833727f0
# profile validation ok=True problems=0
# re-scored 76/76 corpus clips; 0 mismatch(es); max |delta| = 0.0
```

0.19 s. Run it after any profile regeneration; it is the drift alarm.

### Ten corpus clips score below the reject floor

`fully-attentive`, `shilling-of-the-world`, `fortunate`, `might-never-be-over`,
`designated-account-risk`, `liquid-capital-markets`, `speculation`, `rocket-ship`,
`1000x-onchain-volumes`, `crypto-is-about-money` — all shipped, all scoring 14–29.
They were picked on delivery and punchline, not vocabulary. **The rubric is an
ordering tool with a hard floor, not a model of the user's taste.** Never auto-cut
the top N.

---

## Reading the candidates

This is the work. **The score measures theme fit and shape. It cannot tell what is
funny, contradictory, quotable or damning, it cannot tell whether the speaker is the
guest or the host, and it cannot tell whether a claim contradicts the thesis. Read
the `text` and overrule it.**

```bash
scripts/sludgify.py raw/podcast.mp4 --plan \
  | jq -r '.candidates[] | "\(.n)  \(.score)  \(.band)  \(.primary_theme)  \(.dur)s  \(.flags|join(","))\n    \(.text)\n"'
```

Per candidate: `n, start, end, dur, score, base, band, primary_theme, themes[3],
veto[], gates[], words, wps, filler_per_100w, stance{}, address{}, concrete{},
shape{}, flags[], slug, speaker, text, message, emphasis_candidate, shot{}, pick`.
After `--render` each pick also carries `out, sludge_argv[], render_returncode,
frame_strip, render_shot_cuts[], strip_times[], strip_per_cut`.

### Trimming a candidate

The editorial rules below tell you to trim — *"trim the span back to the last complete
clause"*, *"if the best line is at 60% through the span, trim the span so it ends
there"*. `--trim A-B` is how you do that without leaving the pipeline; hand-cutting with
ffmpeg loses the verbatim message, the slug, the strip and the output naming.

It keeps only sentence units lying **wholly** inside the window, so a trim can never
re-admit the half sentence you were cutting off, and it re-scores and re-probes what is
left. Read the unit boundaries out of the cached transcript first:

```bash
jq -r '.transcription[] | "\(.offsets.from/1000) - \(.offsets.to/1000)  \(.text)"' \
  ~/.cache/b-sludgify/<key>/whispercpp.json
```

Measured on `fl-03-interface`: the grown span opened on *"have shown for millennials,
it's not clear that they will keep that going"* — an unresolved `they`, failing the D1
pronoun test. `--trim 6.8-21.6` dropped it and the score went **44.5 → 45.4**, strong to
flagship, landing the span within 1 s and 1 word of the version the user cut by hand.
**Trimming preamble usually raises the score. If it lowers it, you cut the claim.**

**Candidate flags — what each one means:**

| flag | verdict |
|---|---|
| `opens_mid_clause` | **fine.** 21% of the corpus does it. Not a defect. |
| `opens_on_connective` / `lead_in_trimmed_Nw` | already handled — the lead-in was trimmed off the message |
| `ends_mid_clause` | **fix it.** Trim the span back to the last complete clause with `--trim` |
| `opens_on_question_check_speaker` | a host turn may be in the span. Read it. 3/76 corpus clips legitimately open on a question the speaker restates himself — only you can tell those apart |
| `crosses_N_shot_cuts` | camera-angle change inside the span → pass `--headroom 1.4` and frame-check every cut |
| `under_word_floor` | under 40 words. Keep only if it is a genuine one-liner (the corpus floor is 19 words / 7.0 s) |
| `wps_out_of_range` | outside 2.0–4.5 w/s. **This is a whisper failure, not a fast talker** |

### The two hard negatives

Both leaked past a naive scorer during calibration and needed dedicated gates. Both
still need your eyes:

- **On-topic recitation** — correct vocabulary, zero claim. *"We launched perps in
  March, then spot in June, then the app…"* Caught by the `recitation` veto plus the
  `no_claim` gate (×0.70), which fires when a span has no negation, superlative,
  quantifier, modal or stance hit.
- **On-brand rhetoric, off-brand topic** — generic startup or hiring advice, heavy
  second-person, imperative openers, zero core terms. Caught by the `off_topic` gate
  (×0.60), which needs ≥2 distinct core terms across all 12 lexicons.

Two more that no regex can catch, listed in `.reject_list` as `judgement`:
**doom held sincerely** (the corpus's one doom mention quotes it as the *wrong
crowd's* view — same words, opposite stance) and **biographical anecdote**.

### Selection policy for a batch from one source

- **Cap at 2 clips per primary theme per video.** One podcast must not ship five
  `everything_tradable` clips.
- **Prefer spans that braid two themes** over spans that max out one.
- **One speaker per clip, always.** Any span containing a host turn is disqualified
  regardless of score.
- Prefer 15–25 s of speech. Accept 9–12 s only when the span is a complete reversal or
  rant with a hard payoff — 12 corpus clips are that short, and `i-just-want-raw-pnl`
  is 9.9 s and scores 56.
- **`primary_theme` is lexical and will mislabel.** `fortunate` — a luck/confession
  clip — classifies as `apps_over_infra` on the words "build" and "technology". Use
  it for diversity capping, never as a claim about what the clip is about.

---

## The speaker trap

**This is the failure that has bitten most often, and it is invisible in every log.**
One batch shipped 24 seconds of the wrong man's face. Another had sludge's own
`suggested` selection pointing at segments where a HOST was speaking. Both renders
reported `100% coverage` and warned about nothing.

**Coverage is a detection metric, not an identity one.** `face samples: N detected +
0 bridged (100% coverage)` means the tracker found *a* face. It never means the right
one. The shot probe's six signals prove the camera held still; a locked two-shot with
the host talking passes all of them.

### Worked example — a real source, a real trap

`raw/hayes.mp4`, 1920x1080, 126 s. `--plan` reports:

```
[sludgify] 0 camera cut(s) at scene>0.4
[sludgify] 13 span(s) grown, 2 shortlisted (scores 47.8-62.6; reject floor 30.0)
```

Zero camera cuts, and the top candidate scores **62.6, flagship**, with no vetoes and
no gates. Its `message` — emitted verbatim, ready to pass to sludge — is:

> "If you dedicate yourself to trading crypto, you can be very successful, but you
> have to dedicate yourself to it. And I don't think many traders are willing to put
> in the effort. It's 24/7. There's no way around it. You gotta go 24/7 and with
> leverage. **Yeah, exactly.** Or you know, if you're just scalping…"

That "Yeah, exactly." is a backchannel from the other box. The score cannot see it.
Now look at the actual frame:

```bash
ffmpeg -v error -y -ss 110 -i raw/hayes.mp4 -frames:v 1 -vf "scale=960:-1" /tmp/f.png
```

It is a **two-box video call** — host on the left, guest on the right, both faces on
screen the entire time. The layout never cuts, so scene detection correctly reports
zero cuts. Every mechanical signal is green. The head lock would pick whichever face
YuNet prefers and report 100% coverage either way.

### The fix is the source, not the flags

sludgify has no `--crop`, and neither does sludge. **Pre-crop the episode once with
ffmpeg, then run sludgify on the cropped file** — that is what the `-reframed`
suffixes in the corpus directory are, applied by hand twice already.

```bash
# measure the box on a gridded frame first
ffmpeg -v error -y -ss 110 -i raw/hayes.mp4 -frames:v 1 \
  -vf "scale=960:-1,drawgrid=w=96:h=54:t=1:c=red@0.8" /tmp/grid.png

# then crop to it — W:H:X:Y in SOURCE pixels, audio copied
ffmpeg -v error -y -i raw/hayes.mp4 \
  -vf "crop=896:514:968:286" -c:v libx264 -crf 18 -preset veryfast -c:a copy \
  raw/hayes-guest.mp4
```

2.0 s for a 126 s source. **`-c:a copy` is what makes this free.** Measured on this
exact pair: the transcript from the cropped file is byte-identical to the original
(`md5 16392ea8ba6cb04b355576ce55fd81f4` both ways), so every score, span boundary and
word timing is unchanged — `62.6` and `47.8` before and after. The crop re-keys the
cache and costs one transcription pass; it moves nothing.

And it works. The candidate at 62.5 s went from `faces: 2, safe: false` to
`faces: 1, safe: true, flags: []`.

**Crop once per episode and reuse it for every clip from that episode.** Ten parallel
agents each rediscovering the same rectangle is ten times the work for one answer.

### Four failure shapes to recognise

| layout | what the census sees | what betrays it |
|---|---|---|
| Wide 3-shot cut into a close-up | `crosses_N_shot_cuts`, 2–3 faces | the frame strip |
| **Cutaway to the listener's reaction shot** | `crosses_N_shot_cuts`, `face appearance correlation drops` | the frame strip — the audio never changes speaker, so nothing else can see it |
| Two-box video call, static | **0 cuts, and 2 faces every frame** | the frame — nothing else |
| 2x2 grid, static | 0 cuts, 4 faces | the frame — nothing else |
| Masked / off-axis panelist | 1 face, clean | the frame, and the transcript's voice |

> **Cropping does not fix a cutaway, and this is the common case on a produced
> podcast.** The pre-crop above cures a *static* layout — two boxes, a grid — where the
> wrong face is on screen the whole time. A multicam edit that cuts to the host nodding
> is a different failure: the frame is a clean single face, correctly locked, of the
> wrong person, for as long as the director held the shot. There is no crop for it. The
> fixes are `--trim` to a window inside one camera angle, `--scene-threshold` to keep
> spans off the cuts, or rejecting the span. Measured on `cc-15`: candidate 1 scored
> 46.6 flagship with a clean transcript and shipped **7.5 s of the host's face out of a
> 23.5 s body**. Its shot flags said so — `face appearance correlation drops to 0.21`,
> `up to 3 faces on screen` — and the score could not.

### `shot.safe` is a max, and it false-positives

Read `shot.samples` per frame, not the roll-up. On the cropped hayes source the
top candidate still reported `"up to 2 faces on screen"` — from **one frame out of
twelve** (t=124.04). Pulling that frame shows only the guest; the second "face" is a
framed photo on the bookshelf behind him. A single false positive poisons the whole
span's `safe` flag.

```bash
jq -c '.candidates[N].shot.samples[] | {t,faces,cx,cy,h}' plan.json
```

If `faces` is 1 on eleven frames and 2 on one, look at that one frame before you
believe it. `--unsafe {flag,demote,ignore}` controls how flags affect auto-picking;
nothing is ever dropped from the shortlist, and `--pick N` forces any span back.

---

## Writing the clip

The one-paragraph version, from `.one_paragraph` in the profile:

> A good clip is 18–24 seconds and about 60 words of one person making one argument to
> the viewer. It opens on the claim inside the first sentence with at most two words
> of lead-in — mid-sentence entry is fine, preamble never is. It needs no context: no
> unresolved pronouns, no names the audience doesn't already know, no reference to the
> room. It lands on its own thesis in the last three words, on a complete clause, and
> the CTA cuts in immediately. One EDL span if the material is contiguous, never more
> than three; sludge's silence removal supplies the 3–6 visible cuts on its own. One
> face for the whole clip, frame-checked at every cut.

### Length

All duration numbers here are the **speech body**, excluding the CTA tail. The
finished mp4 is body + a dead-consistent **6.4 s** (p25 6.3, p75 6.5).

**The trap:** the finished-mp4 median is 26.4 s, and wiring `--target-duration` off
that ships ~32 s clips — above the corpus p75. Body median is **20.1 s**.

- **Target 18–24 s.** 59% of the corpus is 15–25 s; only 2/76 exceed 30 s while 24%
  are under 15 s. **Short is safe, long is not.**
- Hard floor 12 s, hard ceiling 30 s.
- **Target ~60 words** (median 59, IQR 48–76). Reject under 40 words unless it is a
  genuine one-liner.
- **Ship 13 s if that is all there is. Never pad.**

### The opening

**89% of the corpus puts the hook in the first sentence.** Permitted hooks, in corpus
frequency order: flat claim (59/76), imperative to the viewer (4), a question the
speaker asks and answers himself (3), a hard stat (2).

- **Lead-in budget: 4 words, preferably 1.** Median is 1 word (~0.3 s); 96% are ≤2;
  nothing exceeds 4. Trim `so, and, like, but, um, uh, well, okay, right, yeah, just,
  actually, basically, obviously, again, you know, I mean`.
- **At most one setup sentence**, and only if it is a concrete scenario. Two = reject.
  Only 11% use even one.
- **Mid-clause entry is correct practice, not a defect.** 21% of the corpus does it —
  *"is that, like, the core of crypto has always been about money and finance"* is a
  shipped opening. The test is not grammaticality; it is whether the first three words
  carry content. **Do not build an opener-cleanliness filter.**
- Preferred grammars, in order: named-subject declarative (*"Users don't care about
  decentralization"*), contrarian negation (*"I don't think you're gonna see the L1
  bullshit happen again"*), superlative (*"This is the biggest moment in the history
  of capital markets"*). Hedged "I think" openings are 10.5% — allowed, never the
  default.
- **Automatic rejects, 0/76 of the corpus does any of these:** a greeting, a
  self-introduction, "um"/"uh", "that's a great question", the host's voice, any
  sentence whose subject is the conversation, a hedge stack.

### The landing

**End on the thesis.** The phrase the clip is *about* is in the final quarter of the
body in 64% of the corpus, in the last three spoken words in 41%, and is the literal
final word in 25%. If the best line is at 60% through the span, **trim the span so it
ends there**.

- Pick one of six close shapes deliberately: thesis restatement (17), punchline (9),
  snap summary (5), reason-payoff (4), bookend (4), reversal (2).
- **Restate in different words.** Verbatim bookending is 1/62. Reusing ≥3 content
  words from the opening reads as a loop, not a landing.
- **The snap close is a real weapon.** 26% close on ≤9 words after a longer build —
  *"So those are good perps."* *"And that's what the people want."* When the body ran
  long, close short.
- **End on a complete clause.** Never close on a summary of the summary, "so yeah", a
  hand-off, a hedge, or dead air — 0/76.

> **Measurement trap, stated because it will fool anyone re-deriving this.**
> `corpus.json`'s transcripts appear to show 24% of clips trailing off mid-word. That
> is an artifact of the 6.4 s CTA trim clipping the last word *before* transcription,
> not an edit. Re-transcribed real tails were complete 6/6: *"…so those are good
> PERPS"*, *"…when it corrects, it's gonna be BRUTAL"*, *"…I'm also TRADING"*. The
> genuine mid-word stop rate is 4–7%. Encoding "end mid-word for punch" ships broken
> endings. **Verify tails against the mp4, never the corpus text.**

### Self-containedness — five gates, all reads not regexes

61% of the corpus contains zero proper nouns; only 3/76 carry three or more. Not one
clip depends on a name introduced earlier in the source.

- **D1 pronoun test.** Every *it/this/that/they/those* resolves inside the span or
  names the clip's own topic. *"This is the biggest moment in the history of capital
  markets"* passes; *"That's what I was saying about their model"* fails. **This is a
  reading instruction, not a regex** — a loose demonstrative probe hits 32/76 and most
  of those resolve fine on a human read.
- **D2 proper noun test.** Any name kept must be one the audience already knows:
  Hyperliquid, Solana, Bitcoin, Ethereum, Polymarket, Coinbase, Robinhood, Amazon/AWS,
  S&P 500, LeBron, Kardashian, TikTok, Instagram, X, Morgan Stanley, IBM, SanDisk,
  Interactive Brokers.
- **D3 room test.** Reject "like I said earlier", "to your point", "going back to what
  you asked", "as we discussed".
- **D4 standalone read.** Read the span cold. If the first sentence needs a preceding
  sentence to parse, **reject — do not fix it by extending backwards into preamble**.
- **D5 viewer address (positive).** 84% address "you" or "we" directly. Talking *to*
  the viewer is itself the self-containment device — it replaces the missing context
  with the viewer's own situation. Prefer it, all else equal.

### Cut structure

- **Prefer ONE span.** Max 3, hard max 4. Every corpus transcript reads as continuous
  prose in source order; the only proven EDL work is **removal** — dropping a sentence
  from the middle of a take. Richard's three clips are three selections of one 35 s
  take.
- **Minimum span 4 s.** Do not select a 2 s fragment and hope it joins.
- **Do not reorder** unless promoting a hook to the front. If you do, say so when
  reporting, and never reorder to invert the speaker's meaning.

  > This deliberately narrows b:sludge, which says reordering "is not just allowed, it
  > is the main lever". The reason for narrowing it: reordering interacts badly with
  > the butt-joined-cut stutter below, and worse with misrepresenting a real person on
  > camera. The evidence is weak in both directions — a well-executed reorder is
  > invisible in a transcript by construction — so the default is conservative.

- **Let sludge produce the rhythm.** Its silence removal (`--max-gap 0.35`) yields 3–6
  visible framing changes in a 20 s body — measured on the corpus at median 4 visible
  segments, IQR 3–5, 4.4 s each, which is exactly what sludge produces on its own.
  **Do not chase a cut count in the EDL**; chase a clean 18–24 s of argument and the
  rhythm comes free. (Cut counts are a proxy: no EDLs were preserved, so these are
  top-pane scene detections at `crop=1080:960:0:0, scene>0.18`.)

### The message

**`--message` must be the near-verbatim transcript of the span — ≥90% of the spoken
words, 55–80 words for a 20 s body.** sludgify emits it that way already, with only
the profile's lead-in tokens trimmed.

Two independent failure modes make this non-negotiable, and they pull in opposite
directions:

| what you pass | what breaks |
|---|---|
| a short paraphrase | it is fed to whisper as `--initial_prompt` and can **swallow the take** — a real 10 s span came back as the two words *"when i"*; unprimed it was 104 words |
| a partial paraphrase clearing 60% alignment | the **message** gets captioned instead of the speech, so everything the paraphrase doesn't cover renders with **no captions at all** — 91% aligned still left 11 s of blank caption band |

A verbatim message makes both impossible. **If you only have a slogan, pass no
message** (`--no-message`) — captions then come from the transcript and are correct.
**Never pass a paraphrase.** `--no-prime` is the escape when a transcript comes back
swallowed; the tell is `wps_out_of_range` or an alignment line reading `0/N`.

Verified on a real render through this path: `aligned 76/79 message words to the
audio (96%)`, far above the 60% cliff.

### Emphasis

**Leave auto-emphasis ON.** It fires on `$` / `%` / `Nx` only, which is 16–17% of
corpus-like clips — it structurally cannot over-fire. Never pass
`--no-auto-emphasis`.

- **Do not add asterisks to a span that already has a number.** The auto rule has it;
  doubling up burns two beats of reading time.
- For the 83% with no number: **at most one asterisk**, on the most screenshot-able
  word in the *closing* clause — normally the slug word. `emphasis_candidate` in the
  output is a suggestion and is `null` whenever a number is present. Never exceed 2
  per video.
- **Tie-breaker:** between two otherwise equal spans, take the one with a number — it
  reads with the sound off. But do not manufacture one; only 24% of the corpus
  contains any digit, and the best lines (*"Users don't care about decentralization"*,
  *"Cut the noise"*) have none.

### The slug

`sludge-{speaker}-{slug}.mp4`. Speaker is a bare lowercase handle or surname.

**The slug is a near-verbatim lift of the punchline, not a topic label.** 57% of
corpus slugs come from the last sentence; 33% appear as an exact contiguous phrase in
the transcript. 3–5 kebab words (median 4), ≤38 chars. Prefer the clause carrying the
reversal or the imperative.

**If you cannot write the slug from the closing line, the clip does not land —
re-cut it.**

> **The script's slug is a starting point, and it is the weakest part of the
> pipeline.** Measured against the user's own 76 slugs: mean word recall 0.18, 25/76
> share at least one word. It produces corpus-shaped names
> (`fully-engaged-and-attentive`, `shitty-wage-cooking-job`, `those-are-good-perps`)
> — rename them. One alternative was measured and rejected: also searching the first
> sentence made agreement *worse*, 25/76 → 21/76.

---

## Handing off to b:sludge

`--render` extracts each pick to `<work>/clips/NN-slug.mp4` and calls `sludge.py` on
it. **Extract-then-render, never `--edl` against the full source** — sludge
transcribes the whole head before it parses the EDL, so the cost is proportional to
the head file, and its cache key includes the per-clip prompt.

`--dry-run` prints the argv instead of running it. A real one:

```
sludge.py --head <work>/clips/03-fully-engaged-and-attentive.mp4 \
  --clip clip-nibs.mp4 --music tiktok/red-aura-funk-21.mp3 --cta cta.mp4 \
  --message "If you dedicate yourself to trading crypto, …" \
  --max-gap 0.35 --tail-pad 0.25 --cut-pad 0.07 --min-cut 0.4 \
  --caption-words 3 --caption-chars 24 --headroom 1.4 \
  --out out/sludge-hayes-fully-engaged-and-attentive.mp4
```

Why each default is there:

| flag | why |
|---|---|
| `--max-gap 0.35 --tail-pad 0.25 --cut-pad 0.07` | sludge's own defaults. Overlap needs `tail_pad + cut_pad > max_gap`; `0.25 + 0.07 < 0.35` clears it |
| `--headroom 1.4` | a cut from a wider camera angle otherwise locks at a much lower scale than its neighbours and renders with **black bars**, while coverage still says 100% |
| `--caption-words 3 --caption-chars 24` | verified against rendered corpus frames |
| `--target-duration 20` when planning | sludge's default of 25 plans against *total*; 20 s of body is the corpus centre |

**Never pass these:**

| flag | why |
|---|---|
| `--tail-pad 0.5` | it was the *documented cure* for a clipped last word and it caused **87 ms played twice** — an audible stutter |
| `--max-gap 0.15` | same, 103 ms twice and a cut inside a word |
| `--no-auto-emphasis` | auto-emphasis fires on 17% of clips; it cannot over-fire |

### Never butt-join two EDL cuts

Adjacent segments sharing a boundary **always** overlap once the tail guard runs, and
the overlap replays as an audible stutter. **Merge contiguous segments into ONE cut.**
sludgify emits one span per candidate, so this only bites when you hand-write an EDL —
and that is exactly the case sludge's invariant check is built to catch:
`cut boundaries: N cut(s), all edges in silence, no overlap`.

### Every temp file gets the slug prefix

Renders run concurrently (`--render-jobs`). A generic `plan.json` / `render.log` in a
shared scratchpad gets clobbered by the next clip and you lose or cross plans between
them. sludgify already prefixes clips (`NN-slug.mp4`) and strips
(`NN-slug-strip.png`) — do the same for anything you write by hand.

### Expected log lines — do not chase these

| log line | verdict |
|---|---|
| `tempo: 92.3 BPM … (strength 0.28)` then `beat match: no usable beat within half a beat of the CTA cut` | **expected** on the house bed. Not a bug. **Do not pass `--bpm`.** |
| `WARNING: cut edge at X falls inside the word 'W'` | **usually false.** It checks raw whisper spans, which are imprecise at boundaries. Confirm with an RMS envelope before acting |
| `face samples: N detected + 0 bridged (100% coverage)` | **proves nothing about identity.** This is what the wrong-man batch reported |
| `A flag means the CAMERA moved — none of these can tell you WHO is talking` | sludgify saying the same thing about its own probe |
| `aligned 0/N message words (0%)` | priming failure — re-run with `--no-prime` |
| `WARNING: cuts overlap in the source` | **real.** A defect in a hand-written EDL |

---

## Verify before reporting

Four checks. The first is not optional and has never been optional.

**1. One frame per cut — not evenly spaced samples.** `--render` writes this to
`<work>/verify/NN-slug-strip.png` automatically; `--no-strip` skips it and you should
not. Read the strip: **the same person, the same background, and the same head
position and size in every frame.** A different person is the speaker trap. A
letterboxed frame is the headroom trap.

The strip is built by scene-detecting the **rendered top pane** (`scdet=threshold=8`
after `crop=1080:960:0:0`) and sampling the **midpoint of every shot** — the cut times
land in `render_shot_cuts`, the sampled times in `strip_times`, and the log says
`one frame per cut (N shot(s), cuts at [...])`. If `strip_per_cut` is `false`, scdet
found no cut and the strip fell back to three fractions; it is then a head-lock drift
check, not an identity check, and you should sample by hand.

> **Why not fractions.** This was a real defect, caught by this skill's own check on
> its own output. Sampling the body at fixed fractions lands wherever the clock falls,
> so a wrong-face shot shorter than the stride is invisible. On
> `sludge-chriscamillo-stuff-means-youre-winning` the head lock sat on the **host**
> from 6.73 s to 14.23 s — 7.5 s of a 23.5 s body — and the fraction sampler caught it
> only because `0.5 × body` happened to fall inside that window. A 2 s cutaway would
> have been missed and the clip would have shipped.

To build one by hand from an EDL:

```bash
for t in 0.6 8.2 15.4; do
  ffmpeg -v error -y -ss $t -i out.mp4 -frames:v 1 \
    -vf "crop=1080:960:0:0,scale=250:-1" "/tmp/<slug>-fr_$t.png"
done
ffmpeg -v error -y -i /tmp/<slug>-fr_0.6.png -i /tmp/<slug>-fr_8.2.png \
  -i /tmp/<slug>-fr_15.4.png -filter_complex hstack=inputs=3 /tmp/<slug>-strip.png
```

That crop is the top pane only, which is what makes it a lock check — a single frame
cannot show whether the head is still.

**2. A full frame from the middle of every long cut**, for the captions. One word
highlighted, nothing clipped at the edges. **A blank caption band is invisible in the
log** — if the message cleared 60% alignment, sludge captions the message and says
nothing about the speech it doesn't cover.

**3. Transcribe the rendered output and read it.**

```bash
ffmpeg -v error -y -i out.mp4 -vn -ac 1 -ar 16000 /tmp/<slug>-out.wav
whisper /tmp/<slug>-out.wav --model small --language en \
  --output_format txt --output_dir /tmp
```

That is the edit as the audience hears it. **If it reads as a non-sequitur, fix the
EDL, not the captions.** It is also the check on the bed: words garbled here that
were clean without `--music` mean the bed is burying the voice.

**4. An RMS envelope, but only when a cut-edge WARNING fires.** 10–20 ms windows: a
continuous **−10 to −20 dB** run across the edge is a real mid-word cut; a dip below
**−40 dB** is silence and the warning is noise. Use a 4 kHz-highpassed envelope when
the cut lands after s/z/sh/f/th — broadband RMS cannot see a final /s/ and will let
you slice it.

### What to report

- **Output paths and durations** — body and total, so the 6.4 s CTA is accounted for.
- **Each slug, and why that clip.** Which theme it continues, and what makes it
  quotable beyond the score.
- **Whether you reordered anything.** Say so explicitly.
- **The speaker check, by name.** "Frame-checked N cuts in the strip; same person
  throughout" — or what you cropped and why.
- **Every warning that survived**, and for each one whether you confirmed it or
  dismissed it as expected noise. A dismissed `beat match` line is fine; a dismissed
  overlap warning is not.
- **Candidates you rejected and why**, when the user asked for N and you shipped fewer.

On the first run against a new show, dump the pool (`--dump-pool`) and keep it — the
thresholds were fit against 19 hand-written negatives, not real off-narrative spans,
so real rejections are what lets them be re-fit.

---

## Tuning

| Symptom | Fix |
|---|---|
| Every candidate is off-thesis | check `.themes` in the profile — the source may simply be off-narrative. Do not lower `--min-score` to force picks |
| Too few candidates survive | `--oversample 4`, or `--body-max 34` to admit longer spans |
| Nothing above the reject floor | the source is off-narrative. Say so rather than shipping a 25 |
| Candidates all share one theme | `--max-per-theme 1` |
| Wrong panelist / two faces / grid layout | **pre-crop the source with ffmpeg** and re-run on the cropped file. See the speaker trap |
| Every span crosses a camera cut | `--scene-threshold 0.6`, or accept `--cross-cuts` and frame-check every candidate |
| Candidates land mid-thought | check the re-segment line in stderr — if terminal punctuation is low and you passed `--no-resegment`, drop it |
| Spans grow past 30 s | `--body-max 26 --body-target 20` |
| Clips too long overall | `--body-target 18` |
| `--pick N` rendered nothing | N was past the shortlist. It widens automatically now; check the warning |
| Candidate is right but opens or ends wrong | `--trim A-B` — it snaps to whole sentence units and re-scores |
| A host turn sits inside the only good span | `--trim` past it. If the host is mid-span and cannot be trimmed out, reject the span — do not EDL around it |
| Strip logged `scdet found no cut` | one continuous shot, or the lock flattened the cuts. Sample the top pane by hand at 1 fps and read it |
| Auto-picked span has no face to lock onto | expected — `--unsafe flag` keeps it out of picks but in the shortlist. `--pick N` forces it |
| Flagged spans you want excluded entirely | `--unsafe demote` |
| Black bars in the render | `--headroom 1.4` (already the default here) |
| Blank caption band mid-clip | the message was a paraphrase — re-render verbatim, or `--no-message` |
| Transcript collapsed / absurd words per sec | `--no-prime`, and `--force transcript` to redo the scan pass |
| Typos in tickers, handles, protocols | `--vocab "Hyperliquid, Polymarket, perps"` |
| Non-English or noisy audio | `--ggml-name ggml-large-v3-turbo.bin` |
| Transcription is the bottleneck on a 2 h source | expected. It is cached — the second run is seconds |
| Near-dupe of an existing corpus slug | intentional dupes are fine (5 near-duplicate pairs exist), but make it a conscious choice |
| Renders clobbering each other | every temp file needs the `<slug>-` prefix |
| Score disagrees with the profile after a regen | `--self-test --manifest corpus.json` |
| Need to pass something sludgify doesn't wrap | `--sludge-arg=--face-height --sludge-arg 0.40` — **the `=` is required** on any value starting with `--`, or argparse reads it as a flag and dies with `expected one argument` |

---

## Requirements

Check them all at once with b:sludge's preflight, which detects the OS and prints
platform-specific install commands (`--install` runs them, after asking the user):

```bash
<b:sludge-skill-dir>/scripts/preflight.sh --sludgify
```

- `yt-dlp` on PATH — URLs only; local files skip it
- `ffmpeg` / `ffprobe` with libass and libx264
- `uv` (runs the script and its OpenCV dependency)
- `whisper-cli` (whisper.cpp) preferred at 30–46x realtime; falls back to
  openai-whisper. The fallback **has now been exercised**: `--whisper openai` on a 25 s
  source ran at **3.3x realtime** and produced 78 words where whisper.cpp produced 68,
  scoring the same span 44.9 vs 44.5. It works; it is just 10–14x slower.
- The **b:sludge** skill's renderer, `sludge/scripts/sludge.py`. It sits beside this
  skill: `${CLAUDE_PLUGIN_ROOT}/skills/sludge/scripts/sludge.py` when the `b` plugin is
  installed, or `../sludge/scripts/sludge.py` relative to this skill directory when the
  skills are symlinked into `~/.claude/skills/` (override with `--sludge`)
- `corpus-profile.json` beside the skill (override with `--profile`)
- Network on first run, for the whisper model and the YuNet face model (shared with
  b:sludge's cache)

> **The download stage has been exercised on YouTube.** A public `youtube.com/watch`
> URL fetched video + audio, merged to `source.mp4`, and probed correctly
> (`duration 634.6`, title and uploader populated from `source.info.json`). The
> **X/Twitter path is still unrun.** If a URL fails, try `--cookies-from-browser chrome`
> for age-gated, members-only or authed X posts, and fall back to downloading by hand
> and passing the file.

---

## Measured corpus numbers

76 clips, 24.8 minutes of speech body, 4,656 words. Fingerprint `9b63ae40833727f0` —
compare it against `.provenance.corpus_fingerprint` to detect a stale profile.
Regenerate with:

```bash
scripts/build_profile.py --corpus ~/Developer/bloxwap/sludge/sludgify \
  --manifest corpus.json --out corpus-profile.json
```

| distribution | min | p10 | p25 | median | p75 | p90 | max |
|---|---|---|---|---|---|---|---|
| **body duration (s)** | 7.0 | 11.5 | 15.6 | **20.1** | 24.2 | 26.6 | 34.5 |
| finished duration (s) | 13.4 | 17.8 | 21.5 | 26.4 | 30.1 | 32.9 | 40.9 |
| CTA tail (s) | 2.9 | 5.8 | 6.3 | **6.4** | 6.5 | 6.5 | 7.0 |
| words per clip | 19 | 39 | 48 | **59** | 76 | 86 | 123 |
| **words per second** | 2.04 | 2.47 | 2.76 | **3.18** | 3.63 | 3.95 | 4.29 |
| filler per 100 w | 0.0 | 0.0 | 1.2 | 2.5 | 4.2 | 7.0 | 15.2 |
| "you" per 100 w | 0.0 | 0.0 | 0.0 | 2.6 | 5.3 | 8.8 | 22.2 |

The words-per-second row is the **whisper sanity gate**: the corpus spans 2.04–4.29
across 76 real clips, so a candidate reporting under 2.0 or over 4.5 is a transcript
failure, not a slow or fast talker. Read the centre as **3.0–3.2**, not 3.18 — it is
computed against the trimmed body, which is short by up to ~1 s of real speech at the
tail, so the true rate is very slightly lower.

| rate | value |
|---|---|
| body in 15–25 s | 59.2% |
| body under 15 s | 23.7% |
| body over 30 s | **2.6%** |
| clips containing any digit | 23.7% |
| auto-emphasis fires | 15.8% |
| hook in the first sentence | 89% |
| mid-clause entry | 21% |
| thesis in the last three words | 41% |
| lands on a payoff shape | 61% |
| snap close (≤9 words) | 26% |
| zero proper nouns | 61% |
| addresses "you" / "we" | 84% |
| slug ≥80% word coverage | 71% |
| slug verbatim phrase | 33% |

Rows above the digit/emphasis pair are regenerated by `build_profile.py`; the
editorial rates below them are **hand-classified across all 76 clips**, and where the
two disagree the profile says which to trust. Proper nouns are the live example: the
regex proxy in `.measurements` reads 48.7% because it counts any capitalised
non-sentence-initial token (mid-sentence "I", whisper's inconsistent casing), while
the hand count in `.editorial` is 61%. **Trust the hand count; the proxy is a drift
tripwire, not a measurement.** Same for auto-emphasis: regex 15.8% against a verified
hand count of 13/76 = 17%.

**Speakers (15):** choppingblock 19, chriscamillo 19, ansem 10, sunnydece 7,
frictionless 5, hayes 4, richard 3, rasmr 2, and one each of epstien, freidberg,
kimchi, raj, sacks, trump, trvon.

> Two speakers supply 38/76 clips, so the lexicon is partly their idiolect —
> "ground truth", "connecting dots", "designated account" are chriscamillo; "vamped",
> "meta shift", "rite of passage" are choppingblock. **On a source from a new speaker
> with different diction, theme fit under-fires.** The support tier and the
> second-theme bonus carry more weight there, and the 30–38 shortlist band should be
> read by you rather than auto-rejected.

**Top content terms by frequency:** think 43, people 39, crypto 32, want 23, money 19,
trading 18, time 16, make 16, now 15, markets 15, trade 14, product 13. By *document*
frequency the top content term is **`people`, in 30 of 76 clips (39%)** — ahead of
crypto at 20. The corpus is about **people**, not technology.

**Term overlap is a weak prior by construction.** The whole corpus is 4,656 words with
roughly 880 distinct content terms over ~2,000 content tokens (the exact split moves
with the stoplist), so IDF is thin and the ranking will surface generic market talk.
The score ranks by vocabulary the user already chose. **It does not understand the
argument.** That is what the reading is for.
