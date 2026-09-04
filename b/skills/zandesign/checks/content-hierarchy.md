# Content & Visual Hierarchy (HIER)

## HIER-1 — Hierarchy is built from grouped content, not decoration

Process: inventory the content types on the screen (e.g. user / activity /
comment / actions) → group and prioritize → let the groups form the layout.
Identity rows go horizontal (avatar + name + timestamp), size and color
create in-group hierarchy, metrics sit right-aligned in tight proximity to
their subject, related banners stay adjacent, long-form text stacks below in
descending order, actions float to predictable corners/bottom.

- Severity: P1
- Detect (visual): screens where everything is the same size/weight; metrics
  detached from what they measure; long text above its subject.
- Detect (code): flat sibling lists rendering heterogeneous content with one
  shared style.
- Source: 1802939950645584131 "Stop ignoring visual hierarchy in UI"

## HIER-2 — Less content, stronger hierarchy

Don't fill space with more content. Remove or demote until one element is
clearly primary (scale up the true focus), support it with atmosphere, and
keep a **single call to action** with a verb (BTN-1/BTN-2). Authentic
details (ticket cutouts, real imagery) beat added widgets.

- Severity: P1
- Detect (visual): competing focal points; multiple CTAs; date/metadata
  dominating over the actual subject.
- Source: 2053925539019165904 "Stop adding content to your UI"

## HIER-3 — Visual anchoring: shape before text

In lists of options (Uber's ride list is the reference), a consistent icon /
thumbnail column lets users recognize shape before reading text and scan
vertically — stripping the anchors raises cognitive load and slows
decisions.

- Severity: P1
- Detect (code): option/settings/product lists rendered as text-only rows
  when items have distinguishable identities.
- Detect (visual): dense text lists with no leading glyphs where users must
  compare options.
- Fix: add a consistent leading icon/illustration per row, one visual scale.
- Source: 1996927463679529069 "1 visual design rule to upgrade your UI"

## HIER-4 — One hero, one message

Heroes: exactly one hero image (two compete for attention), scrim for
contrast, then caption → title → primary + secondary CTA to reduce decision
friction. Navigation controls concentrate into a single control center — not
split across both ends of the navbar.

- Severity: P1
- Detect (visual): hero sections with 2+ images/videos; navbars with
  controls scattered left and right beyond logo + one cluster.
- Source: 1968252670604685678 "Stop making these hero UI mistakes"

## HIER-5 — Real content, varied content

Duplicated placeholder rows ("everything looks the same") and blurry images
hide layout truth and demo terribly. Use realistic, varied content and
sharp assets in review builds; empty states get designed (see MOT-6).

- Severity: P2
- Detect (code): mock arrays repeating one item N times; lorem ipsum
  strings; low-res images scaled up.
- Source: 1808453653604216891 "Stop designing with placeholder content",
  1810257177094873305 "Stop designing with blurry images"

## HIER-6 — Start from patterns, not from scratch

Zander's recurring workflow point: don't design commodity UI from scratch —
start from established patterns/templates/design systems (his references:
Wise, Cash App design systems; Framer templates for portfolios) and spend
the creativity budget on what differentiates the product. In code review
terms: prefer platform-established patterns (iOS tabs, standard sheets)
over novel reinventions of solved components.

- Severity: P2
- Detect: hand-rolled versions of solved components (custom tab bars,
  custom modals) that behave worse than the platform pattern.
- Source: 1972619393319465095 "Stop designing UI from scratch",
  2020761306114953403 (blank canvas), 1630932135254032384 (Wise),
  1990423909800272289 (Cash App)
