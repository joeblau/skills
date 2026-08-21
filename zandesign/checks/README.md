# Design Review Checks

Codified from 208 transcribed design videos by Zander Whitehurst
([@zander_supafast](https://x.com/zander_supafast), founder of Memorisely,
Figma Educator Advisory Board), posted Dec 2021 – Aug 2026. Source corpus lives
in `../corpus/` (`posts.tsv` = tweet index, `transcripts/` = whisper output,
`corpus.md` = combined). Each check cites its source tweet ID so the original
video is one URL away: `https://x.com/zander_supafast/status/<id>`.

Check IDs are stable — cite them in review reports (e.g. `BTN-1`).

| File | Prefix | Domain |
|---|---|---|
| typography-alignment.md | TYPE | Text alignment, casing, readability |
| layout-spacing.md | LAY | Containers, borders, whitespace, spacing grid, radius |
| color-tokens.md | COL | Semantic tokens, dark mode, gradients, contrast |
| buttons-actions.md | BTN | Button hierarchy, sizing, labels |
| forms.md | FORM | Labels, placeholders, inputs |
| modals-dialogs.md | MODAL | Confirmation and conversion modals |
| content-hierarchy.md | HIER | Visual hierarchy, scannability, content economy |
| motion.md | MOT | Transitions, staggering, easing, loading/empty states |
| icons-imagery.md | IMG | Icon systems, hero imagery, placeholder content |

Severity scale used in reports:

- **P0 usability** — measurably hurts task completion (placeholder-as-label,
  failing contrast, six primary buttons)
- **P1 hierarchy** — makes the UI harder to parse (centered stacks, border
  soup, no visual anchors)
- **P2 polish** — separates good from great (motion staggering, corner-radius
  math, dark-mode tuning)
