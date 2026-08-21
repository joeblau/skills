# Buttons & Actions (BTN)

## BTN-1 — One primary button per view

Setting every action to primary destroys hierarchy and creates cognitive
overload — users waste time finding *the* action. Organize actions into
primary (one), secondary (neutral filled), tertiary (neutral outline/ghost),
and fold overflow behind progressive disclosure (a "More" tertiary button)
when a view wants more than ~4 actions.

- Severity: P0
- Detect (code): multiple `variant="primary"` / brand-filled buttons rendered
  in one screen/section; screens with 5+ visible buttons and no tier system;
  a Button component with no variant prop at all.
- Detect (visual): count filled brand-color buttons per screenshot; >1 fails.
- Fix: demote to secondary/tertiary; add a `More` disclosure for the tail.
- Source: 1802684455670136954 "Stop designing with primary buttons"

## BTN-2 — Verb labels, benefit-oriented copy

Button text is a verb describing the action ("Delete folder", "Buy tickets",
"Get weekly tips") — never "Yes"/"OK"/"Are you sure?", never imperative
noise like "Subscribe now!", and never capitalized shouting (TYPE-2).
Marketing CTAs state the user's benefit, not the team's task.

- Severity: P1
- Detect (code): button children equal to `OK`, `Yes`, `No`, `Submit`,
  `Subscribe`, `Click here`.
- Fix: rename to the concrete verb + object; for conversion surfaces, lead
  with the outcome.
- Source: 1838191200832020932, 1971536302286971070

## BTN-3 — Minimum button width

Auto-layout buttons sized purely by label + padding produce a ragged,
uneven UI when labels vary. Set a minimum width (and stretch to align within
a group) for visual balance and larger tap targets.

- Severity: P2
- Detect (code): Button component with no `minWidth`; sibling buttons of
  visibly different widths in a row/stack.
- Detect (visual): uneven button widths in the same group.
- Fix: min-width token (e.g. 96–120pt) + full-width in narrow containers.
- Source: 1996575940038578337 "Stop ignoring your button width"

## BTN-4 — Full-width tap target for the primary conversion action

On mobile and in modals/forms, the primary CTA should span the container
width — bigger target, clearer hierarchy.

- Severity: P2
- Detect (code): modal/form primary buttons with intrinsic width centered in
  a wide container.
- Source: 1971536302286971070 "Stop telling customers to 'subscribe'"

## BTN-5 — Interactive states exist and animate

Buttons (and cards, tabs, switches) need default / hover (web) / pressed /
active states, with quick spring or ease-out transitions — not instant
swaps and not dead styles. A 3D/elevated button presses *down*; a liked
heart bounces; a switch slides with a liquid ease.

- Severity: P2
- Detect (code): `Pressable`/`TouchableOpacity` with no pressed style;
  `:hover`/`:active` absent on web buttons; state changes with no
  transition.
- Fix: add state styles + ~150–250ms ease-out or spring (see MOT-4).
- Source: 2071915410648183239 (button easing), 1838541264859369978
  (3D press), 1528698131088539648 (heart), 1471444082308308992 (switch)
