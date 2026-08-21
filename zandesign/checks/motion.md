# Motion & Transitions (MOT)

## MOT-1 — Three transition types, used for the right job

- **Context transition** — content changes, structure stays: tabs, filters,
  state changes. Animate the content swap, keep the frame stable.
- **Drill transition** — moving between hierarchy levels: list → item,
  feed → post, summary → full view. Directional push/slide.
- **Continuity transition** — a shared element persists and morphs:
  card → full screen, mini-player → full player, grid → product page.
  (Shared-element transition on RN; FLIP/view-transitions on web.)

Using a hard cut (or the wrong type) where a shared element exists breaks
the user's spatial model.

- Severity: P2
- Detect (code): navigation with `animation: 'none'`; tab switches that
  remount whole screens; detail screens whose hero image could morph from
  the list but doesn't.
- Source: 2013949087624642570 "3 UI Transitions Every Designer Should Use"

## MOT-2 — Rule of three: staggered attention layers

A perfect static layout still fails if everything appears at once — nothing
stands out. Each section has primary / secondary / tertiary attention
layers, animated sequentially with ~0.2s delay offsets to guide attention
over time. Heading text animates first (by word/line), then supporting
content, then tertiary chrome.

- Severity: P2
- Detect (code): screens/sections that mount with no entrance choreography
  where marketing/onboarding surfaces exist; all elements sharing one
  animation with zero delay offsets.
- Fix: 3 layers max, 0.2s stagger, copy the same curve across layers.
- Source: 2038980148972621945 "Stop designing hierarchy without motion",
  1846152101019210007 (staggered portfolio), 1895050658341966082

## MOT-3 — Motion has hierarchy too

Overlay choreography (see MODAL-2): depth via backdrop blur/dim, chrome
yields space, primary element scale+fades, fixed controls simple-fade,
sheet slides in last. Every animated screen should have one primary motion,
not five equal ones.

- Severity: P2
- Source: 1895050658341966082

## MOT-4 — Easing: ease-out energy, springs with restraint

Transitions start with energy and settle (ease-out) — content entering
decelerates into place. Springs for playful elements: reference values
`duration 0.8, bounce 0.4` for a 3D button; "bouncy" for hamburgers/hearts,
"gentle" to settle. Never linear, never instant swaps for spatial changes.

- Severity: P2
- Detect (code): `Easing.linear`, default `duration: 0` state flips,
  `LayoutAnimation` absent where lists reflow.
- Source: 2071915410648183239 (button easing curves), 1838541264859369978,
  1528698131088539648

## MOT-5 — Loading states are designed

Loading is a first-class state: skeletons or branded loading animation
(bouncy gradient bars, glowing rings), not a bare spinner or a blank
screen.

- Severity: P1
- Detect (code): `isLoading && <ActivityIndicator/>` as the entire loading
  UI on primary screens; no skeleton components in the codebase.
- Source: 1554384338619453440, 1713865111536877727, 1589933689353355264,
  1488441709000286209 (Lazy Load skeleton plugin)

## MOT-6 — Empty states have personality

Empty states go beyond a gray line of text: playful illustration (3D clay
icons are the reference), motion, and a clear next action.

- Severity: P2
- Detect (code): empty-list branches rendering a bare `<Text>No items</Text>`.
- Source: 1958110103275462659 "Animate 3D clay icons in your UI"

## MOT-7 — Scroll-linked and hover moments where they earn attention

Marketing/portfolio surfaces: on-scroll reveals, hover scale (~1.05–1.1)
on cards, morphing nav on scroll. Product surfaces: restraint — motion
follows function (context/drill/continuity), decorative scroll effects stay
on marketing pages.

- Severity: P2 (advisory)
- Source: 2069746623551300057 (nav → on-scroll animated), 1714219948724076644
  (hover scale), 2034995045191872973
