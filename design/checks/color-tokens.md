# Color & Tokens (COL)

## COL-1 — Two-layer token system: primitives + semantic

Colors live in two layers: primitive scales (`gray/100…900`, `blue/100…900`)
and semantic tokens that alias them (`color-background-screen`,
`color-surface-raised`, `color-text-primary`). Components reference semantic
tokens only. Semantic names follow `color / element / priority / state`
(e.g. `color-text-secondary`, `color-button-primary-hover`) — never
random names, never raw hex in components.

- Severity: P1
- Detect (code): hex/rgb literals inside component files; a theme file that
  is a flat list of arbitrary names (`brandBlue2`, `grayish`); no
  light/dark aliasing layer.
- Fix: introduce primitives + semantic aliases; sweep components to semantic
  tokens.
- Source: 1875103802082382115 "Primitive and Semantic color variables",
  1835633197905842226 "Stop guessing design tokens"

## COL-2 — Dark mode is a first-class mode

Every screen must work in both modes via token aliasing, not overrides
sprinkled per-component. Zander ends nearly every principle video with "and
see it in dark mode" — parity is the norm, not a stretch goal.

- Severity: P1
- Detect (code): `useColorScheme`/`prefers-color-scheme` unused or partially
  applied; hardcoded `#fff` backgrounds; screens that only define light
  values.
- Detect (visual): screenshot each key screen in both modes; look for
  unreadable text, untinted surfaces, blinding whites.
- Source: 1875103802082382115, 1988234478452359457, and the recurring
  "see it in dark mode" pattern across the corpus

## COL-3 — The 70/60 rule for saturated color in dark mode

High-saturation light-mode colors pop far too hard on dark surfaces. Apply
~70% opacity to warm colors and ~60% to cool colors in dark mode (or bake
equivalent desaturated tokens) so they blend while staying legible.

- Severity: P2
- Detect (code): identical saturated brand hexes used in both themes.
- Detect (visual): neon-glow chips/badges/charts in dark mode screenshots.
- Fix: dark-mode aliases with reduced opacity/saturation (warm 70%, cool 60%).
- Source: 1998382654408753176 "Stop breaking your colors in dark mode"

## COL-4 — Red (and brand color) with intention

Don't reflexively use red for destructive buttons — especially when the brand
color is red. Overusing a loud color turns signal into noise and creates
decision friction. Use neutral surfaces for most actions so the brand/danger
color reads as a deliberate signal; destructive confirmation belongs to the
modal pattern (MODAL-1), not to scattering red everywhere.

- Severity: P1
- Detect (code): `red`/`danger` variants on many buttons per screen; brand
  color used as default fill on most components.
- Fix: neutral secondary/tertiary buttons; one intentional accent per view;
  danger color only at the moment of destructive confirmation.
- Source: 1988234478452359457 "Stop using red for danger buttons"

## COL-5 — Gradients: consistent hue in HSL, four stops

Two-stop dark-to-light gradients go muddy through the gray middle. Build
gradients in HSL with a consistent hue: light source (highest saturation +
lightness) → hold → falloff → anchor (darkest). Shift hue only to derive a
palette of related gradients.

- Severity: P2
- Detect (code): two-stop gradients mixing unrelated hues; gradients defined
  ad hoc per component.
- Detect (visual): muddy/gray gradient midpoints.
- Source: 2021225376576381391 "Stop guessing your gradient colors"

## COL-6 — Scrim before text-on-image

Text over imagery needs a gradient scrim (or blur) layer to guarantee
contrast — one hero image, one scrim, then the type hierarchy.

- Severity: P0 (it's the contrast failure mode of TYPE-3)
- Detect (code): `ImageBackground`/`background-image` with direct text
  children and no overlay layer.
- Source: 1968252670604685678
