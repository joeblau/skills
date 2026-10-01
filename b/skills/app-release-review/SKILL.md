---
name: app-release-review
description: Review an app for native iOS and Android release, research current App Store and Google Play requirements, and produce a complete evidence-backed checklist, GitHub issue drafts, and store documentation and asset plans. Use for a pre-release audit or store submission readiness review.
---

# App Release Review

Turn a real app's release gaps into actionable GitHub tasks for native implementation, App Store Connect, Google Play Console, and the documents and assets users and reviewers need. Cover both stores by default; honor a narrower platform request. Review Swift/UIKit/SwiftUI, Kotlin/Views/Compose, React Native/Expo, Flutter, hybrid apps, and supplied binaries without assuming a framework.

Use Claude Code (`/b:app-release-review` as a plugin or `/app-release-review` locally), Codex (`$app-release-review`), or Kimi (`/skill:app-release-review`). Resolve bundled resources relative to this file's real directory. Keep generated output in the target repository or a user-selected output directory, outside the skill installation.

## Scope and evidence

1. Resolve the target repository, supplied build, and intended release date. Preflight paths with `rg --files` or `fd` before reading. Record commit/build identifiers, bundle/application IDs, native targets, minimum OS, device families, release type (first release/update), markets, locales, target ages, monetization, login, data recipients, and developer account type. Infer what the evidence supports; mark the rest unknown.
2. Inventory every screen, modal, extension, embedded web view, first-run path, permission request, account flow, paywall, setting, and core task. Include server behavior and third-party SDKs, not only the UI. A web preview, desktop bundle, source declaration, or simulator screenshot does not prove a native mobile build works.
3. Ask only for facts that materially change applicability and cannot be discovered. Continue independent review. Unknown console settings, private backend behavior, account type/date, or missing builds become verification tasks, not passes or automatic exemptions. If only a macOS or web app exists, retain the requested iOS and Android work as platform-readiness tasks and label downstream mobile checks unverified; do not mark them all inapplicable.

Read [references/catalog.json](references/catalog.json) for the baseline checks and [references/sources.json](references/sources.json) for primary-source starting points. The catalog is a floor: discover additional applicable rules from current policies, console mandatory tasks, markets, and app features. Add stable `EXTRA-*` IDs for such checks instead of forcing them into unrelated entries. Review complete policy sections relevant to the app; a fixed catalog cannot cover every future policy or regulated feature.

Read these guides when their work is reached:

- [references/native-review.md](references/native-review.md): source/build/runtime examination and platform-specific verification.
- [references/accessibility.md](references/accessibility.md): manual task matrices, native assistive technology, and accessibility claims.
- [references/store-materials.md](references/store-materials.md): copy, screenshots, icons, previews, and documentation deliverables.
- [references/conditional-review.md](references/conditional-review.md): feature-, account-, and market-dependent policy branches.
- [references/output-contract.md](references/output-contract.md): audit JSON, GitHub task bodies, dependencies, and rendering.
- [references/lingcode-evidence.md](references/lingcode-evidence.md): optional patterns grounded in the inspected LingCode bundle; read only if that reference or an AI coding app is relevant. It is not a required dependency or the default target app.

## Research current rules

Browse on every audit. Open the actual primary pages; search snippets and copied launch checklists are leads, not proof. Use Apple guidelines, App Store Connect Help and upcoming requirements; Google Play full policies, help articles, announcements and deadlines; Android technical documentation; W3C and applicable regulators. Use practitioner advice only as a clearly labeled recommendation supported by reasoning.

Recheck date-sensitive SDK/deployment/target API thresholds, Billing Library support, memory-page support, testing eligibility, identity/package verification, age-rating forms, screenshot device buckets, AI declarations, and regional payment programs. Distinguish published, effective, future, and account-specific requirements. Save the checked date, effective date/scope, section, direct URL, interpretation, and app applicability. Treat future deadlines as planning work if the release crosses them, not a present violation. Keep conflicts visible and resolve them using the specific policy and the target console's applicable notice; never silently choose an obsolete number.

The bundled research was checked on **2026-09-30**. Refresh it rather than treating that date's values as permanent. If browsing fails, complete supported code work, label affected rules unverified, and generate source-verification tasks. Do not claim an audit is ready on stale evidence.

## Examine the app

Perform static, release-build, device/runtime, accessibility, backend/data, store-console, and document/asset passes. Use the target project's existing build/test instructions and available tools. Run meaningful tests for core paths, failure states, purchases, permissions, and deletion. Record commands, device/OS, build ID, paths, dates, and observed results. State unavailable passes explicitly.

Trace each data flow: collection -> local storage -> network recipient -> processing/purpose -> retention -> deletion. Include analytics, crash reports, advertising, push, auth, payments, AI inputs and outputs, attachments, speech, clipboard, logs, backups, and optional providers. Reconcile implementation with the privacy policy, Apple privacy answers, Google Data safety, consent, purpose strings, and SDK declarations. These are distinct artifacts with distinct definitions.

Review screenshots visually and inspect dimensions/format; read the copy. For accessibility, test common tasks with assistive technology and system settings across supported devices, including login, purchasing, and account settings. Search matches identify candidates only. A manifest or ARIA attribute is not proof of runtime behavior; automated scans cannot establish complete accessibility.

Every baseline check for each requested platform must have exactly one result:

- `pass`: direct evidence proves the check for the declared scope and build.
- `fail`: an observed implementation or artifact contradicts an applicable requirement or quality criterion.
- `unverified`: applicability or proof is missing; state the missing evidence and exact next check.
- `not-applicable`: an evidenced trigger exclusion, not absence of files or access.

Classify the obligation separately as `required`, `conditional`, or `recommended`. Conditional findings need the actual trigger and source. A serious accessibility or reliability gap can be a project release blocker even when the relevant store label or guideline is optional; explain that judgment and keep the store mandate distinct. Do not invent legal certification, claim guaranteed approval, or mark every recommendation mandatory.

## Produce the release work

Generate a scoped audit under `release-review/<app>-<version>/` (choose a collision-free directory). Produce:

- `audit.json`: scope, refreshed sources, every platform/check result, additional discovered checks, tasks and artifact inventory.
- `RELEASE-REVIEW.md`: app/build scope, readiness **per store**, highest-impact findings, missing evidence, applicability decisions, source changes, tested coverage and limits.
- `CHECKLIST.md`: full check matrix, including passes and exclusions; unresolved rows link to task IDs.
- `GITHUB-TASKS.md` and `issues/<task-id>.md`: ordered, review-ready tasks with checkboxes and reproducible acceptance criteria.
- `materials/`: separate iOS and Android listing drafts, reviewer-notes drafts, screenshot capture plan and asset inventory, privacy/data disclosure worksheet, support/deletion documentation plan, and accessibility matrix/statement draft. Include feature-dependent documents such as terms, moderation standards, licenses, permissions videos or regulated-service evidence. Mark unsupported facts as `NEEDS INPUT`; do not fabricate business contact details, retention periods, licenses, test results, or legal claims.

For every failed or unverified check, supply a concrete task or map it to an existing task/issue with the same acceptance criteria. Group only work with the same owner and verifiable outcome. Keep platform differences explicit. Prioritize and order prerequisites: evidence/data inventory -> native fixes/backend -> verified disclosures/listing/assets -> console declarations -> submission. A blocker in one store does not establish a blocker in the other.

Tasks must distinguish `native`, `backend`, `console`, `documentation`, `assets`, and `verification` work. Include obligation and trigger, platform, area, priority, store-specific release-blocking judgment, code/runtime/console evidence, direct policy links and checked dates, remediation, artifact paths, acceptance checkboxes, dependencies, owner role, and a stable deduplication key. Missing evidence tasks should request the exact access/test/export needed, not presume a defect. Never put passwords, API keys, signing material, private user data, or reviewer credentials in GitHub bodies; reference a secure handoff instead.

Use the bundled helper (Python 3, standard library only) to validate coverage and render the checklist/task files:

```bash
python3 <skill-dir>/scripts/render_tasks.py <output-dir>/audit.json --out <output-dir>
```

It checks structural coverage and task links; it does not inspect the app, confirm policy truth, or certify release readiness. Review the rendered output and underlying evidence before making a readiness claim. Required or triggered-conditional failures/unknowns remain open release gates; recommendations do not automatically block submission. A ready verdict additionally requires current console/build evidence and no unreviewed feature or market branches.

## GitHub publication

Default to local task drafts. Creating issues, labels, milestones, or comments requires the user's instruction to publish them; an audit request alone does not authorize GitHub writes. If already authorized, finish the drafts and validation, resolve the target repository, inspect existing issues, and publish without asking again. Use a connector or `gh issue create --repo OWNER/REPO --title ... --body-file <resolved-file>`; do not interpolate issue bodies into shell commands. Do not assign people or create labels/milestones unless requested or established by repository conventions.

Deduplicate on the app/version/platform/check/outcome key included in each body. On an uncertain write response, search for that exact key before retrying. Save confirmed issue URLs to `published-issues.json`; preserve previous publications on reruns and report any unresolved writes. Store submission, release rollout, code fixes, destructive testing, and sending messages are separate actions outside the audit unless requested.
