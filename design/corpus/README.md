# Corpus

Source material for the `b:design` check catalog: every native-video post
from [@zander_supafast](https://x.com/zander_supafast) (Dec 2021 – Aug 2026),
harvested via date-windowed X search with Chrome MCP.

- `posts.tsv` — `tweet_id \t date \t title` for all 215 harvested posts
- `ids.txt` — download queue consumed by `worker.sh`
- `transcripts/` — whisper (base.en) transcripts, one per video (208)
- `corpus.md` — transcripts joined with metadata, newest first
- `worker.sh` — download (yt-dlp) + transcribe (whisper) pipeline; takes an
  id-list file, skips completed work, safe to re-run
- `videos/` — downloaded mp4s (gitignored, ~500MB)

Refresh with new posts: append tweet IDs to `ids.txt` (and rows to
`posts.tsv`), run `./worker.sh ids.txt`, then rebuild `corpus.md` (the
python snippet in git history / regenerate: join `posts.tsv` +
`transcripts/*.txt` sorted by date descending).

Notes: 4 queued tweets have no downloadable video; 2 videos are silent
(no audio track). X search is lossy over long ranges — the harvest walked a
date cursor and probed gaps, but a handful of posts may still be missing.
