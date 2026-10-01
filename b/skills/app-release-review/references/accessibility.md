# Accessibility review and claim evidence

Use A-A11Y-LABELS, A-A11Y-TEST, A-A11Y-AUDIT, A-DESIGN-TIPS, D-A11Y, D-A11Y-TEST, W-WCAG and W-ICT from `sources.json`. Platform design recommendations, voluntary store disclosures and jurisdiction-specific obligations are different classifications. WCAG2ICT is informative guidance for non-web software; do not claim all native apps are legally required by stores to meet WCAG 2.2 AA.

## Review every common task

List common tasks first: onboarding, login/recovery, the core loop, search/forms, purchase/restore/cancel, consent/permissions, support, settings and account deletion. Include embedded browser, third-party checkout and extensions when part of those tasks. Create a matrix by platform/device and applicable assistive technology/setting. Mark each task `pass`, `fail`, `unverified`, or an evidenced exclusion; attach results to the release build.

| Area | Reproducible review |
|---|---|
| Screen reader | Complete tasks with iOS VoiceOver and Android TalkBack. Inspect accessible names, roles, values, states, headings, grouping, traversal, custom actions and meaningful image alternatives. Hide decoration. Read changes/errors without flooding speech, especially streaming chat. |
| Focus and navigation | Navigate with keyboard, Switch Control/Switch Access, Full Keyboard Access where supported, and Voice Control/Voice Access. Verify initial focus, modal trapping and restoration, no inaccessible overlays, actionable controls and usable alternative paths. |
| Text and zoom | Exercise largest Dynamic Type/accessibility sizes, Android font/display scaling, web zoom and reflow. No truncation of essential text or hidden actions; keyboard and banners must not obscure focus. Test supported locales and right-to-left layouts. |
| Contrast and color | Measure actual light/dark/high-contrast foreground/background combinations, including errors, disabled controls and charts. Use WCAG AA benchmarks: 4.5:1 normal text, 3:1 large text and applicable non-text UI. Judge thresholds and exceptions in their proper context. Provide meaning beyond color. |
| Touch and gestures | Measure tappable area and spacing, not just icon size. Use Apple's 44 pt and Android's 48 dp recommended design targets; distinguish web WCAG target-size criteria. Provide alternatives to drag, precision, multitouch and device motion. |
| Motion and timing | Enable Reduce Motion/remove animations; check parallax, autoplay, flashing and transitions. Provide pause/stop and time extensions where needed. Preserve meaning after reducing motion. |
| Audio/video | Verify captions including relevant sound, transcripts and audio descriptions where applicable, volume controls and non-audio notification alternatives. Test any voice-only workflow's text/input alternative. |
| Forms and cognitive load | Persistent labels, instructions, discoverable errors, accessible validation, consistent help, clear destructive confirmations, no needless repeated entry. Permit password managers/paste and accessible authentication alternatives. |
| Public materials | Read support/privacy/deletion pages with screen readers and keyboard. Provide readable copy, alt text for graphic assets where supported, captioned demonstrations and accurate accessibility instructions. |

Automated audits help find issues; manually complete workflows afterward. Use Accessibility Inspector and available XCTest audits for iOS; Android Accessibility Scanner/Compose or Espresso checks as appropriate. Axe on a web view assesses that surface only. A green scan is not a complete accessibility verdict.

## Apple Accessibility Nutrition Labels

As checked 2026-09-30, Apple allows device-specific responses for VoiceOver, Voice Control, Larger Text, Dark Interface, Differentiate Without Color Alone, Sufficient Contrast, Reduced Motion, Captions and Audio Descriptions. The published criteria require successful common tasks for each claimed feature; inspect each feature's current linked criteria. Larger Text's label criterion calls for 200% or more. Feature availability varies by device. Providing labels and an accessibility URL is optional; misleading claims are subject to accurate-metadata review. (A-A11Y-LABELS)

Create a claim worksheet: device, feature, current criterion URL, covered tasks, test results, known limitations, claim decision and approval owner. For third-party/user content in common tasks evaluate the reasonable mechanisms the app supplies (for example creator-provided image labels or captions), following Apple's actual criteria rather than inventing an all-content guarantee. Publish only supported claims. An accessibility statement should explain tested support, setting instructions, limitations and contact; omit invented certification.

## Turn gaps into tasks

Describe the user's blocked task and reproduce it with the setting/device/build. Identify the relevant native semantic or layout code. Acceptance should require completing the workflow after the fix, with an evidence artifact, not merely adding `accessibilityLabel`, `contentDescription` or `aria-label` everywhere. A placeholder-only text field is a candidate for inspection; inspect its actual accessible name before reporting a confirmed failure.

Flag an inability to use critical flows as a project blocker with a reason. Keep optional dark-mode/label work as recommendations unless a claim or independently applicable requirement changes that classification. Route legal accessibility applicability to the intended service and market; the European Accessibility Act covers specified products/services, not every mobile app solely because it is sold in Europe (EU-EAA).
