# Typography & Alignment (TYPE)

## TYPE-1 — Don't center-align stacked text

Stacked lines of center-aligned text create a new alignment anchor on every
line, making blocks hard to scan. Left-align (in LTR locales) so the reader
gets a single alignment anchor, then use proximity to group related text.
Centering is acceptable for a single short line (a badge, an empty-state
one-liner), never for multi-line copy or lists of text blocks.

- Severity: P1
- Detect (code): `textAlign: 'center'` / `text-center` / `text-align: center`
  on containers holding multi-line copy, paragraph stacks, or card lists.
- Detect (visual): more than two consecutive centered text lines of different
  widths; ragged both-edges columns.
- Fix: left-align the block; recover grouping with spacing (proximity), not
  centering.
- Source: 2090368430914310313 "Stop centering text in your UI"

## TYPE-2 — Title case, never ALL CAPS, in buttons and labels

Uppercase labels are less scannable and read as shouting. Use title or
sentence case in buttons, tabs, and navigation.

- Severity: P1
- Detect (code): `textTransform: 'uppercase'` / `uppercase` class on
  button/label text; hardcoded ALL-CAPS strings in CTAs.
- Fix: sentence/title case; reserve caps for tiny meta-labels (max ~11px
  eyebrow text) if the brand demands it.
- Source: 1968252670604685678 "Stop making these hero UI mistakes"

## TYPE-3 — Text must pass contrast checks

Heading or body text over images/gradients that fails WCAG contrast is
non-accessible decoration, not content. Add a scrim (linear gradient), move
the text, or drop the layer.

- Severity: P0
- Detect (code): text rendered directly over `ImageBackground` / hero images
  without an overlay layer; hardcoded low-contrast pairs (e.g. gray-400 on
  white).
- Detect (visual): run contrast on screenshot text regions; anything under
  4.5:1 body / 3:1 large text fails.
- Fix: gradient scrim under text, darker token, or reposition.
- Source: 1968252670604685678 "Stop making these hero UI mistakes"

## TYPE-4 — Use text styles/tokens, not one-off font values

Detached, ad-hoc font sizes and weights fragment the type system. Every text
node should resolve to a named style (theme typography token / Tailwind text
scale), with bold/italic as a weight variant of the same style, not a new
style.

- Severity: P2
- Detect (code): raw `fontSize:` numbers scattered outside the theme file;
  more than ~6 distinct font sizes across screens; inline `fontWeight`
  overrides that duplicate an existing style.
- Fix: map to the nearest existing text token; extend the scale only
  deliberately.
- Source: 1803372068186083545 "Stop detaching text styles in Figma"
