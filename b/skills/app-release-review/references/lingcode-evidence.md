# LingCode bundle research: bounded implementation examples

Inspected on **2026-09-30**, at the user-requested `/Applications/LingCode.app`. Metadata identifies LingCode **2.7.54**, build **203**, bundle identifier `hainanmandi.LingCode`, with `CFBundleSupportedPlatforms = MacOSX` and minimum macOS 14.0. This is an installed desktop bundle, not an iOS or Android source checkout. No mobile build, mobile console configuration, complete native source or backend was supplied through that path. Do not reuse the observations as a verdict on another app or future LingCode build.

Snapshot SHA-256: `Contents/Info.plist` = `34363d00004812662faa58ff848389729b64d7f0f36308c954d15bb4cdb26797`; `Contents/Resources/agent-bridge/lib/openai-compat.mjs` = `3f375af394b71012204fa563535df8925cabe59dc359682601f7e95af21343b6`. Recheck content and line references if these change.

Re-preflight the paths before reading a local copy. If absent, skip this optional reference; never require installing LingCode to run the release skill. Do not run bundled agent/cloud-deployment tools merely to inspect them.

## Observed code and resulting review questions

| Evidence in bundle | Observed fact | Reusable review implication |
|---|---|---|
| `Contents/Info.plist` | macOS-only platform; microphone/speech/camera and user-folder purpose strings; `lingcode` URL scheme; auth/inference service URLs; Sparkle updater configuration | Detect actual platform, permissions, auth routing, integrations and update mechanism. Desktop folder access and updater behavior are not evidence for compliant mobile sandbox/updates. |
| `Contents/Resources/agent-bridge/lib/openai-compat.mjs:145` and `:160`–`:184` | User content enters the conversation; request body includes messages and tool schemas and is POSTed to a configured model endpoint | Trace prompt/attachment/tool-result data to every configurable provider. Establish actual personal-data scope, recipient, retention/training practices and consent evidence. This local excerpt does not prove a missing consent flow elsewhere. |
| Same file `:265`–`:305` | Hook checks and a permission callback precede mutating native/MCP tool execution; allowed calls are executed afterward | Review user-action controls separately from data-sharing consent. A tool approval callback alone cannot prove Apple AI-sharing or Play User Data compliance. |
| `Contents/Resources/agent-bridge/lib/backend-deploy-permission.mjs:10`–`:43` | Production apply metadata describes migration/function/deletion counts and warns about malformed confirmation summaries | Concrete, reviewable changes and clear destructive outcomes are useful product-quality patterns for agent actions. This is not a store-policy checklist or authorization for this skill to deploy. |
| `Contents/Resources/agent-bridge/README.md` | CLI documentation describes selectable providers, credential storage, session-history opt-in and transcript export | Inventory provider-specific collection, local persistence, export, deletion, credential handling and public claims; validate implementation rather than copying README assertions. |
| `Contents/Resources/ClaudeWebUI/index.html:58`–`:64` | A polite live transcript region and dialog roles are present | Semantics are useful static evidence; verify announcements, focus entry/trapping/restoration and native web-view behavior at runtime. Streaming updates can still make a task unusable. |
| Same HTML `:79` | Composer textarea has placeholder text; no explicit label is present in this element's initial markup | Candidate for accessible-name inspection. Dynamic JS/native wrapping may change the name, so confirm the accessibility tree and task behavior before reporting a failure. |
| `Contents/Resources/ClaudeWebUI/styles.css:603` and `:611` | A reduced-motion media query exists; composer suppresses its outline | Do not infer no reduced-motion support from appearance. Inspect the query's effect and actual focus indication, including surrounding styles and native focus behavior. |
| `Contents/Resources/swift-nio_NIOPosix.bundle/Contents/Resources/PrivacyInfo.xcprivacy` and analogous `swift-nio__NIOFileSystem.bundle` | SDK manifests declare required-reason API use, tracking and collected-data fields | This proves those packaged SDK files exist; it does not prove the full app's manifests, approved API reasons, store privacy answers or Data safety are complete. |

All paths above are relative to `/Applications/LingCode.app`. HTML/CSS/JS findings describe packaged web content, not a complete native accessibility audit. The bundle search found no reusable store-release checklist in the inspected bridge/web-UI paths; this skill's policy coverage therefore comes from the independent primary-source research.

## If the skill is run against this bundle

Retain both requested mobile platform tracks. Create concrete verification/readiness tasks to locate or build the intended iOS and Android apps, establish IDs/owners/device scope, and obtain mobile release artifacts and console evidence. Group downstream missing-build evidence sensibly while retaining every baseline row as unverified with that prerequisite. Do not assert that no mobile app exists anywhere; only this supplied artifact is macOS-only.

Follow with provider/data inventory, native mobile permission/sandbox evaluation, common-task accessibility testing, accurate listing/capture plans and console declarations. Link each issue to actual observed paths or an exact missing evidence request. This is an example of evidence discipline, not a precomputed audit of LingCode's entire product.
