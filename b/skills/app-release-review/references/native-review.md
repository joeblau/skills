# Native implementation and runtime review

Read with `catalog.json`. Collect the evidence needed for each applicable row; split results by platform even for shared code. Source IDs below resolve in `sources.json`.

## Discover the release implementation

Preflight paths, then inspect build configuration, dependencies/lockfiles, entitlement/permission files, routes, shared components, authentication, account settings, payment code, network clients, storage, analytics and consent. Search terms are leads; trace call sites and packaged release configuration.

| Stack | Useful evidence |
|---|---|
| iOS | `.xcodeproj/project.pbxproj`, `.xcworkspace`, Swift packages, `Podfile.lock`, `Info.plist`, `.entitlements`, `PrivacyInfo.xcprivacy`, asset catalogs, localized purpose strings, archive metadata |
| Android | `build.gradle`/`.kts`, version catalogs, merged release `AndroidManifest.xml`, network security config, backup/data-extraction rules, release signing configuration, native `.so` libraries, AAB metadata |
| React Native/Expo | JS navigation and accessibility plus generated native projects/config plugins, `app.json`/app config, EAS profiles, permission and SDK changes in the actual artifact |
| Flutter | `pubspec.lock`, widget semantics, native iOS/Android folders, plugin manifests and release artifacts |
| Hybrid | Native shell, web routes, web-view configuration, navigation/bridge allowlists, native permission prompts and mobile accessibility tree |
| Installed bundle only | Package metadata, packaged readable code/resources, dependencies, permissions, architecture and provenance; source and backend behavior remain incomplete |

Record which files establish intended behavior and which build/runtime observations establish actual behavior. Do not inspect personal credential stores or dump signing secrets. A dependency list is not a complete account of data collection.

## iOS release pass

Use A-REVIEW, A-SUBMIT, A-REQUIREMENTS and A-DISTRIBUTE.

- Compare deployment target and SDK/toolchain against the current upload rules. An SDK version and minimum supported OS are different gates. Check bundle/version/build identifiers, supported devices, signing/provisioning and capabilities in the archive.
- Verify asset catalogs/icons, entitlements, associated domains, push environment, extensions and permission purpose strings. Explain each protected resource in context; exercise denial and later revocation. Review background modes, API use, downloaded/executed code and web views against the relevant guideline sections.
- Build and validate the intended distribution configuration using project instructions. Test the release candidate on physical devices when possible, across minimum/latest supported OS and supported iPhone/iPad layouts. Record archive validation and App Store Connect processing status separately from local compilation.
- Inspect app and SDK privacy manifests, required-reason API declarations, and applicable SDK signatures. The aggregate privacy report informs the assessment; it does not replace App Store privacy answers or the privacy policy. An SDK manifest does not establish the app's full data practices (A-MANIFEST, A-REASONS, A-SDK, A-PRIVACY).
- Exercise first launch, all login methods, return from OAuth, expired sessions, reinstallation/update, purchases/restoration and deletion. Examine Apple-silicon Mac/visionOS availability if enabled, rather than assuming those environments were intentionally supported.
- TestFlight is a useful release test route, not a universal production submission prerequisite. Keep external beta review and App Review distinct (A-TESTFLIGHT).

## Android release pass

Use G-TARGET, G-CREATE, D-SIGN, D-PAGES, D-64 and G-RELEASE.

- Inspect `targetSdk`, `minSdk`, `compileSdk`, package name/version code, merged release permissions, component exports, signing and Play App Signing setup. Verify current submission requirements for new apps versus updates and device-type exceptions.
- Inspect the signed AAB and generated device APKs, including transitive SDK/native libraries and ABI coverage. Test 64-bit and 16 KB page compatibility where applicable; a pure Java/Kotlin app can still acquire native libraries through dependencies. Recheck memory-page deadlines and console exemptions: older articles can conflict with the current page.
- Review release debuggability/test libraries, backup behavior, cleartext networking, exported activities/providers/services, deep links, web-view bridges and credentials/logging. Use OWASP MASVS as a risk-based quality benchmark, not a mandatory Play certification (OWASP).
- Exercise Android back/predictive back, edge-to-edge insets, keyboard handling, rotation, fold/unfold, resizing and multiwindow; process death/background restrictions; offline, slow network and interrupted transactions. Check notification channels/permission and foreground-service types/notifications/declarations when used. Use the current adaptive/core quality guidance (D-QUALITY) and add discovered device-specific checks.
- Review Play pre-launch/pre-review results, crashes/ANRs, compatibility warnings and SDK Index warnings. Check signing-certificate fingerprints used by OAuth/API integrations against the Play-distributed artifact, not just the debug build (G-SDK, D-SIGN).

## Common flow and backend pass

Inventory and test: first run, onboarding skip, empty/loading/error states, every advertised core feature, offline/retry, permissions denied/revoked, login/reset/logout, all purchase states, restoration, support, account/data deletion and deep links. Use synthetic accounts/data and nonproduction fixtures; avoid real charges or destructive production operations unless authorized.

For each run record: platform, device/model, OS, build, locale, accessibility setting, starting state, steps, expected result, actual result, evidence path and date. Include failures from interruptions, low resources, server errors and upgrades/migrations. Measure performance and energy on core paths; distinguish a project budget from a store rule.

Trace auth and authorization to backend enforcement. Inspect local credential storage, TLS validation, access boundaries, consent recording and SDK initialization timing. Test deletion's server propagation, third-party deletion and retained-data exceptions. A successful button tap alone is insufficient.

For AI coding/agent apps inspect provider routing, content reporting, AI data consent, persistence/transcript exports, attachments, local execution, dynamic tools/plugins and human action controls. Document on-device versus server execution. Desktop tool-permission prompts cannot establish mobile policy compliance. See `lingcode-evidence.md` for concrete source patterns.
