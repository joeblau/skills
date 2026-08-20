# Modals & Dialogs (MODAL)

## MODAL-1 — Confirmation modal anatomy (no "Are you sure?")

The canonical destructive-confirmation modal:

1. **Title = verb + object** ("Delete folder?"), not "Are you sure?" and no
   decorative question-mark icon.
2. **Left-aligned text** for readability (TYPE-1).
3. **Body is informative, not pleading**: state concisely that the action is
   irreversible — don't ask "are you really really sure".
4. **Confirm button = the same verb** ("Delete"), title case, destructive
   styling *here* (this is where the danger color belongs — COL-4).
5. **Secondary action = "Cancel"** (not "Discard"), so users can continue
   their journey.
6. **Close icon** in the corner as an extra exit.
7. Works in dark mode.

- Severity: P0
- Detect (code): `Alert.alert('Are you sure?')`, modal titles that are
  questions without verbs, confirm buttons labeled `OK`/`Yes`, missing
  cancel.
- Detect (visual): question-mark iconography; center-aligned modal body text.
- Source: 1838191200832020932 "Stop designing 'Are you sure?' modals"

## MODAL-2 — Overlay motion has hierarchy

Sheets and modals don't just appear (a "basic dissolve" is good UI; great UI
staggers): dim + blur the background to create depth, slide fixed chrome
(nav bar) away to make room, scale-and-fade the primary card in, simple
fade for fixed controls, action sheet slides up last as the primary delayed
transition.

- Severity: P2
- Detect (code): modals with no enter animation, or a single opacity toggle;
  no backdrop treatment.
- Fix: backdrop fade/blur → chrome yields → content scale+fade → sheet
  slide, each offset slightly (see MOT-2).
- Source: 1895050658341966082 "What separates Good UI from Great UI?"
  (Revolut breakdown)
