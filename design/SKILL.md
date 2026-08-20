---
name: b:design
description: |
  MANUAL TRIGGER ONLY: invoke only when user types /b:design.
  Deep design review of a React / React Native app against a check catalog
  codified from Zander Whitehurst's (@zander_supafast) design-video corpus.
  Static code pass + visual screenshot pass + flow pass, then a ranked
  report with concrete fixes. Add --fix to apply the fixes.
argument-hint: "[path-to-app] [--fix] [--static-only] [--screens <glob|route,...>]"
disable-model-invocation: true
---

Review a React or React Native codebase the way a senior product designer
would — grounded in the 30 codified checks in [checks/](checks/README.md),
each traceable to a source video (`https://x.com/zander_supafast/status/<id>`).

Read `checks/README.md` plus all category files **before** reviewing so
findings cite stable check IDs. Do not invent checks outside the catalog;
if you spot something real that no check covers, report it in a separate
"Outside catalog" section.

## Phase 0 — Scope

1. Resolve the target: the argument path, else the current repo. Detect
   stack: `react-native`/`expo` in package.json → RN; else React web
   (Next.js/Vite/CRA). Detect styling system (StyleSheet, Tailwind,
   styled-components, CSS modules) — it changes the grep patterns below.
2. Inventory surfaces: routes/screens (navigation config, `app/` or
   `pages/` dirs), the design system (`theme`, `tokens`, `components/ui`),
   and shared components (Button, Input, Modal, Card).
3. If `--screens` was given, restrict to those; otherwise rank screens by
   user impact (entry, auth, core loop, checkout/conversion, settings) and
   cover at least the top 6.

## Phase 1 — Static pass (code)

Run the catalog against code. High-signal starting greps (adapt to the
styling system; these find candidates, you judge them in context):

| Check | Candidate pattern |
|---|---|
| TYPE-1 | `rg -n "textAlign:\s*'center'|text-center|text-align:\s*center"` on multi-line copy containers |
| TYPE-2 | `rg -n "textTransform:\s*'uppercase'|uppercase"` in button/label components |
| TYPE-4 | `rg -n "fontSize:\s*\d+"` outside theme files; count distinct values |
| LAY-2 | `rg -n "borderWidth|border(?!Radius)"` on non-interactive containers; `divide-y` |
| LAY-3 | `rg -n "(margin|padding|gap)[^:]*:\s*(\d+)"` → flag values not on the 4px grid |
| LAY-4 | same `borderRadius` token on parent+child with nonzero padding |
| COL-1 | `rg -n "#[0-9a-fA-F]{3,8}|rgba?\(" app src --glob '!*theme*' --glob '!*tokens*'` |
| COL-2 | `rg -n "useColorScheme|prefers-color-scheme|dark:"` — absence or partial use |
| COL-4 | count `danger|destructive|red` button variants per screen |
| BTN-1 | count `variant=.primary.` / brand-filled buttons per screen file |
| BTN-2 | `rg -n ">(OK|Yes|No|Submit|Subscribe)<"` and Alert button labels |
| BTN-5 | `Pressable`/buttons with no pressed/hover style or transition |
| FORM-1 | `rg -n "placeholder="` inputs; verify a persistent label exists nearby |
| FORM-2 | `rg -n "placeholder=\"?(e\.?g\.?|Type here|Enter text)"` -i |
| MODAL-1 | `rg -n "Are you sure|Alert\.alert"` |
| MOT-1 | `rg -n "animation:\s*'none'|animationEnabled:\s*false"` in navigators |
| MOT-4 | `rg -n "Easing\.linear|duration:\s*0\b"` |
| MOT-5 | `rg -n "ActivityIndicator|Spinner"` as sole loading UI; absence of Skeleton |
| MOT-6 | empty-state branches rendering bare text |
| IMG-1 | icon imports from >1 package; inline `<svg`/`<Svg` beside library icons |

Also read the theme/tokens file(s) end-to-end and the four shared
components (Button, Input, Modal, Card) — most systemic findings live
there, and one fix in a shared component outranks twenty screen patches.

## Phase 2 — Visual pass (screenshots)

Skip only if `--static-only`.

- **React web**: start the dev server (see the project's README/scripts;
  the `run` skill's conventions apply). Screenshot each in-scope route via
  Chrome MCP (`tabs_context_mcp` → `navigate` → `computer screenshot`) at
  mobile (390px) and desktop widths, in **both light and dark mode**
  (emulate via `prefers-color-scheme`, or the app's own toggle).
- **React Native**: build/run on the iOS simulator (`npx expo run:ios` or
  the project's script), drive with `xcrun simctl` and capture
  `xcrun simctl io booted screenshot <file>.png`; toggle appearance with
  `xcrun simctl ui booted appearance dark|light`. If no simulator is
  available, fall back to Expo web if the project supports it, and say so
  in the report.

Evaluate each screenshot against the visual detections in the catalog
(box-count for LAY-1, contrast for TYPE-3/COL-6, button-count for BTN-1,
anchor scan for HIER-3, dark-mode parity for COL-2/COL-3, hero rules for
HIER-4). Save screenshots to a `design-review/` scratch dir and reference
them by filename in findings.

## Phase 3 — Flow pass

Walk 2–3 core flows (e.g. browse → detail → action, plus one destructive
action and one form). Judge:

- Transition types against MOT-1 (context/drill/continuity) and MODAL-2.
- Entrance choreography against MOT-2/MOT-3.
- Loading, empty, and error states against MOT-5/MOT-6 (throttle network or
  stub data if needed).
- Confirmation surfaces against MODAL-1.
- Form journeys against FORM-1..4.

## Phase 4 — Report

Produce `DESIGN-REVIEW.md` in the target repo (and summarize inline):

1. **Verdict** — 2–3 sentences: the app's biggest design lever right now.
2. **Findings table** — ranked by severity (P0 → P2): `ID | Check | Where
   (file:line or screenshot) | Finding | Fix`.
3. **Per-finding detail** for P0/P1: evidence, why it matters (one line,
   from the check), the concrete change (real code diff or token change,
   matching the project's styling system), and the source video link.
4. **Systemic recommendations** — token/theme/shared-component changes that
   fix whole classes of findings at once, called out separately from
   point fixes.
5. **Outside catalog** — anything real the catalog doesn't cover, clearly
   labeled as reviewer judgment.

Do not pad: skip checks that pass, and say which checks were verified clean
in one line. If `--fix` was passed, apply fixes in severity order after the
report — systemic (tokens/shared components) first, then per-screen; run
the app's typecheck/tests between batches.
