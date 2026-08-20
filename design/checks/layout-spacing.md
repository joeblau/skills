# Layout & Spacing (LAY)

## LAY-1 — Whitespace over containers

Don't use containers (cards, boxes, wrappers with backgrounds) for layout —
it produces a boxy UI and visual noise that weakens hierarchy. Let content
layout create natural whitespace groups; reserve surfaces for deliberate
emphasis (one elevated card among plain groups, not everything boxed).

- Severity: P1
- Detect (code): nested `Card`/`View`/`div` layers where several siblings all
  carry background + border + radius; wrappers whose only job is a gray box.
- Detect (visual): screenshot reads as a grid of boxes-inside-boxes; count
  distinct bordered/filled rectangles per screen — more than ~3 non-list
  surfaces is a smell.
- Fix: strip container styling to bare layout, group with spacing, keep at
  most one emphasis surface per region.
- Source: 2084623671444799847 "Stop adding containers to your UI"

## LAY-2 — Whitespace over borders

Borders drawn to "create hierarchy" make the eye see the border, not the
content. Remove borders; use padding, spacing, and color emphasis instead.
Divider lines between every list row are the same smell.

- Severity: P1
- Detect (code): `borderWidth`/`border` on non-interactive containers;
  `divide-y` / `ItemSeparatorComponent` used together with generous card
  styling; borders + background + shadow on the same element.
- Fix: delete the border, increase padding/gap; if separation is still
  needed, a subtle surface-color shift beats a line.
- Source: 2080000671781110136 "Stop adding borders to your UI"

## LAY-3 — 4px spacing grid

Spacing values should sit on a 4px grid (4/8/12/16/24/32…). Wonky one-off
values (13, 18, 22) break rhythm and proximity.

- Severity: P2
- Detect (code): margin/padding/gap literals not divisible by 4 (except 1–2px
  hairlines); a spacing scale that isn't defined anywhere.
- Fix: define a spacing scale token set; snap offenders to it.
- Source: 1968252670604685678 "Stop making these hero UI mistakes" (follow
  the four pixels)

## LAY-4 — Nested corner radius: outer = inner + padding

When a rounded element sits inside a rounded container, don't reuse the same
radius — it creates an awkward pinched gap. The container's radius should be
the inner element's radius plus the padding between them.

- Severity: P2
- Detect (code): parent and child both using the same `borderRadius` token
  with nonzero padding between them.
- Detect (visual): corner gap wedges on nested cards, thumbnails in cards,
  buttons in sheets.
- Fix: `outerRadius = innerRadius + padding`; check tight/regular/loose fits.
- Source: 1996139572556648582 "Stop guessing your corner radius"

## LAY-5 — Proximity builds relationships

Related items sit tight; unrelated items sit apart. Metrics belong next to
the thing they measure, captions next to their image, actions near their
object. If every gap is equal, grouping is invisible.

- Severity: P1
- Detect (code): uniform `gap` across semantically different boundaries;
  labels separated from values by layout structure.
- Detect (visual): equal vertical rhythm across the whole screen; orphaned
  captions.
- Fix: tighten intra-group spacing, widen inter-group spacing (e.g. 4–8 in,
  16–24 between).
- Source: 2090368430914310313, 1802939950645584131

## LAY-6 — Responsive breakpoints are designed, not accidental

Mobile, tablet, and desktop layouts should come from explicit breakpoint
values (min/max width tokens), not from whatever flex happens to do. On the
web, wide content must reflow, not stretch to 1400px line lengths.

- Severity: P1
- Detect (code): no breakpoint constants/media queries in a responsive web
  app; fixed pixel widths on top-level layout; RN layouts that ignore
  `useWindowDimensions` where tablet support is claimed.
- Fix: define breakpoint tokens once; max-width readable columns; test each
  breakpoint.
- Source: 1675855870356344838 "Setting device breakpoint variables",
  1673619316208549889 "Designing responsive card grids"
