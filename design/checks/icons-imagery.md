# Icons & Imagery (IMG)

## IMG-1 — Use an established icon library, consistently

Don't hand-draw commodity icons. Use one consistent library — references:
Hugeicons (27k+), Lucide (clean, consistent, scalable), Heroicons
(playful). One library, one stroke weight, one size grid per product. Mixed
libraries or mixed filled/outline styles in one view is the failure mode.

- Severity: P1
- Detect (code): imports from multiple icon packages; ad-hoc inline SVGs
  next to library icons; icons sized inconsistently (16/18/20/22/24 soup).
- Fix: standardize on one library + size/stroke tokens.
- Source: 1836024015778918554 "Stop designing custom icons"

## IMG-2 — Icons reflect meaning, sized to their tier

Icons aren't decoration: transaction logos beat generic filled squares
styled like mini primary buttons; small neutral icons for metadata. An icon
styled with primary-button emphasis competes with the real CTA (BTN-1).

- Severity: P2
- Source: 1802684455670136954

## IMG-3 — Sharp assets only

No blurry, upscaled, or stretched imagery. Provide @2x/@3x (RN) or
srcset/next-image (web); crop with intention.

- Severity: P1
- Detect (code): single-resolution raster assets rendered larger than
  intrinsic size; missing `resizeMode`/`object-fit` choices.
- Source: 1810257177094873305 "Stop designing with blurry images"

## IMG-4 — Background-removed, integrated product imagery

Product/hero images work harder with backgrounds removed and elements
integrated into the layout (angled perspective, layered text) rather than
pasted rectangles.

- Severity: P2 (advisory, marketing surfaces)
- Source: 1959943697342066792, 1716738251057225921, 1534830424203067392
