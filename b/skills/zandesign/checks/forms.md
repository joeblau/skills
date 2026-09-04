# Forms & Inputs (FORM)

## FORM-1 — Never use placeholders as labels

Placeholder-only "minimalist" inputs kill usability: the moment the user
types, context disappears — error rates spike, completion slows. Every input
keeps a persistent visible label; the placeholder is a *hint*, not the label.

- Severity: P0
- Detect (code): `TextInput`/`input` with `placeholder` and no associated
  `<label>` / label element / accessibilityLabel + visible text; floating
  label libraries that hide the label at rest.
- Fix: persistent label above the field + hint placeholder.
- Source: 1874759761180365226 "Stop using placeholders as labels"

## FORM-2 — Placeholder content: example or instruction, never "e.g."

"e.g." isn't recognized across languages and trips screen readers ("enter
e.g. John Doe"). Use either a bare example ("John Doe" — Dovetail, Spotify)
or a direct instruction ("Enter your full name" — Notion, Linear). Never
abstract junk like "Type here".

- Severity: P1
- Detect (code): placeholder strings containing `e.g.`, `E.g`, `eg.`, or
  `Type here`, `Enter text`.
- Fix: bare realistic example or direct instruction.
- Source: 1879858975426122119 "Stop using e.g in placeholders",
  1874759761180365226

## FORM-3 — Unambiguous prompts on conversion forms

Email-capture and signup forms: outcome-oriented heading (what the user
gets, not "Subscribe to my newsletter"), benefit-verb button, unambiguous
placeholder ("Enter email address"), tight spacing, form anchored to the
natural end of reading flow, with privacy note and dismiss affordance.

- Severity: P1
- Detect (code/visual): newsletter/signup surfaces violating any of: heading
  about the company not the user; button says "Subscribe"; no privacy note;
  no close affordance.
- Source: 1971536302286971070 "Stop telling customers to 'subscribe'"

## FORM-4 — Input states are designed

Text fields have default, focus (active border/label move), filled, and
error states, animated smoothly (Material-style label float is the
reference). A field that only ever shows one style is undesigned.

- Severity: P2
- Detect (code): input components with no focus styling beyond browser
  default; no error state rendering.
- Source: 1472912035944009731 "Animating Material Design Text Fields"
