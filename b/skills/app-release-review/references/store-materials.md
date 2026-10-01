# Store copy, assets and documentation

Read the app's shipped behavior before writing copy. Source IDs resolve in `sources.json`. Numbers below are a **2026-09-30 snapshot**, not permanent constants; refresh current pages and the applicable console forms before producing an upload-ready specification.

## Listing field worksheet

| Store | Fields and observed limits | Source |
|---|---|---|
| App Store | Name 2–30 characters; optional subtitle up to 30 | A-INFO |
| App Store | Required plain-text description up to 4,000 characters; required keywords up to **100 bytes**; optional promotional text up to 170 characters | A-VERSION |
| App Store | Support URL with contact information, privacy URL, copyright, category, age rating, version/build; what's new up to 4,000 characters for updates, unavailable for first release | A-INFO, A-VERSION, A-AGE |
| Google Play | Title up to 30, short description up to 80, full description up to 4,000 characters | G-LISTING |
| Google Play | App/game, free/paid, category/tags, contact email, privacy URL, audience and content declarations, countries and locales | G-CREATE, G-PREPARE |

Count every locale's text and UTF-8 bytes separately where the field uses bytes. Keep Apple promotional text, subtitle and keywords separate from Play fields; Play has no separate Apple-style keyword field. Do not label optional fields mandatory.

For each store/locale draft: one clear opening benefit, who the app serves, the verified core features and how they help, significant connectivity/device/account limitations, truthful paid-feature/subscription disclosures, and support/privacy/terms references where applicable. Link every concrete claim to a shipped flow or evidence. Avoid planned features, unverifiable superiority, keyword blocks, false affiliations and irrelevant platform imagery. Copy should describe the mobile release being reviewed, not a more capable desktop sibling. Review the stores' current metadata rules (A-REVIEW §2.3, G-LISTING).

Provide a claim table: claim -> platform/build -> observed flow -> free/paid availability -> evidence -> approved wording. Mark unknowns `NEEDS INPUT`. Explain AI/on-device/cloud behavior only to the extent verified; do not invent a no-training/no-collection promise. Marketing structure and screenshot ordering are recommendations, not extra store mandates (A-PRODUCT).

## Capture and asset plan

For each asset record store, locale, supported device bucket, orientation, source build, screen/flow, fixture account, exact dimensions/format, overlay copy, accessibility text where supported, policy classification, rights owner and output path. Keep raw capture and final design separately. Record actual file metadata and visual QA. Create tasks to capture missing assets; do not fabricate UI screenshots or use generated images as evidence of a running app.

**App Store:** the checked screenshot page accepts 1–10 JPEG/JPG/PNG screenshots with no transparency. Supply accepted iPhone device-bucket captures; 6.5-inch captures are required if 6.9-inch captures are not supplied. A 13-inch iPad set is required when the app runs on iPad. Resolve current fallback/scaling rules and all supported device types on A-SCREENSHOTS; do not assume every historical bucket requires separate captures. Example accepted portraits include 1320×2868 (6.9-inch) and 2064×2752 or 2048×2732 (13-inch iPad). App previews are optional; when provided, check A-PREVIEWS for current device resolutions, duration (15–30 seconds), format, frame rate and file size. (A-SCREENSHOTS, A-PREVIEWS)

**Google Play:** the checked general listing requirements include a 512×512 PNG icon (maximum 1024 KB), 1024×500 JPEG/24-bit PNG feature graphic without alpha, and at least two JPEG/24-bit PNG screenshots overall. General screenshot dimensions are 320–3840 pixels and the longest dimension cannot exceed twice the shortest; up to eight per supported device type. Larger-screen and specialized form factors have their own specs. Four high-resolution screenshots are recommendation-surface eligibility guidance, not the universal two-image submission minimum. Graphic alt text is recommended (140 characters or less). A preview video is optional; if used, verify the YouTube visibility, embedding, no-ads and age-restriction rules. (G-ASSETS)

Do not confuse store icons with launcher/app asset catalogs. Recheck the native icon requirements and dark/tinted/alternate variants in the target toolchain. Capture each supported platform's actual UI. Use synthetic data, licensed graphics and no private notifications or secrets. Check readability at listing size, clipping, localization, general-audience suitability and truthful depiction. Use the strongest core outcome early in the sequence; include paywall context when showing paid functionality. On Play, evaluate each submitted image/video's AI asset self-declaration independently of whether the app itself generates AI content (G-AI-ASSETS).

## Documents and console package

| Deliverable | Required facts and acceptance evidence |
|---|---|
| Data inventory/disclosure worksheet | Data type, collecting code/SDK, platform, on/off-device handling, recipient and processor, purpose, linked identity, tracking/sale/sharing definitions, optionality, encryption, retention, deletion and evidence. Derive Apple privacy and Play Data safety answers separately (A-PRIVACY, G-DATA). |
| Privacy policy | Actual app/entity/contact, collected data and purposes, recipients, security, retention/deletion, choices and relevant populations/markets. Working public URL and in-app access; Play's policy has URL/format/access constraints. A generic template or SDK manifest is insufficient (A-REVIEW §5.1, G-USER-DATA). |
| Deletion documentation | In-app path, Play's required external request resource when account creation triggers it, identity confirmation, associated data, processors, completion timing, retained exceptions, subscription implications and tested results (A-DELETE, G-DELETE). |
| Support page | Public contact, common questions, permission/recovery instructions, billing/restore/cancellation help, deletion and accessibility support as applicable. Test URLs while logged out and from the app. |
| Terms/EULA and billing copy | Determine standard versus custom EULA; price/period/renewal/trial disclosures and privacy/terms links for the product. Do not require a custom EULA universally. Verify with current payment/subscription policy. |
| Reviewer notes/access | Exact core/new features, secure test-account handoff, steps for every gated/paid flow, hardware/region/backend dependencies and unusual behavior. Test reviewer access with a fresh session; never place credentials in GitHub tasks (A-VERSION, G-PREPARE). |
| Compliance packet | Encryption determination/documents if needed, rights/licenses, regional/trader evidence, restricted-permission declarations/demo videos, medical/financial approvals and moderation/child-safety standards when triggered. |
| Release test/operations packet | Device and accessibility matrices, beta findings, backend compatibility, crash/ANR/performance evidence, build provenance, release notes, publishing settings, monitoring/support owner and recovery plan. |

Produce usable listing and notes drafts plus artifact-specific tasks. For unsupported policy facts, produce a worksheet/draft and a task to obtain and approve the facts. Do not claim that policy drafting itself establishes legal compliance. If the app lacks code or behavior needed by the document, schedule that prerequisite before finalizing the claim.
