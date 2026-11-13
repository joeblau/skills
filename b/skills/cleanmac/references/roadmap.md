# CleanMac implementation roadmap

Research completed **2026-11-13** against the supplied screenshots and current
primary documentation, supplemented by read-only static inspection of installed
CleanMyMac 5.7.4. The initial local runtime now supports interactive scanning,
review, exact selection plans, native Trash, and recovery. **18 feature records
have partial implementations; 58 remain planned.** No issue is claimed complete
merely because its CLI entry point exists.

The [GitHub roadmap](https://github.com/joeblau/skills/issues/9) tracks the complete
project. Every feature issue includes research evidence, a proposed
implementation, dependencies, acceptance criteria, and primary sources.

## Start here

- [Research findings](research.md): what is documented, what is proposed, and
  which product internals are unavailable.
- [Conversation design](interaction.md): intake, scan scope, progressive review,
  selection, execution, cancellation, and recovery.
- [Implementation contract](architecture.md): deterministic adapters, stable
  identities, state records, action semantics, and release gates.
- [Feature catalog](features.json): detailed proposals and dependency IDs.
- [Source catalog](sources.json): 68 primary web sources, seven local macOS
  manuals, and one installed-app static inspection, with provenance.
- [Screenshot coverage](coverage.json): all 10 screenshots and all 21 My Tools
  tiles mapped to feature IDs.
- [Issue index](issues.json): exact published issue URLs for all feature IDs.

## Installed-app research integration

The [installed-app evidence](research.md#installed-app-inspection) supplements
these twelve existing feature issues with implementation notes and acceptance
criteria: [CM-001 / #10](https://github.com/joeblau/skills/issues/10), [CM-002 / #11](https://github.com/joeblau/skills/issues/11), [CM-003 / #12](https://github.com/joeblau/skills/issues/12), [CM-005 / #14](https://github.com/joeblau/skills/issues/14), [CM-008 / #17](https://github.com/joeblau/skills/issues/17), [CM-011 / #20](https://github.com/joeblau/skills/issues/20), [CM-012 / #21](https://github.com/joeblau/skills/issues/21), [CM-014 / #23](https://github.com/joeblau/skills/issues/23), [CM-022 / #32](https://github.com/joeblau/skills/issues/32), [CM-045 / #57](https://github.com/joeblau/skills/issues/57), [CM-046 / #58](https://github.com/joeblau/skills/issues/58), [CM-053 / #65](https://github.com/joeblau/skills/issues/65).

The additions cover separate access/permission states, app-specific rule packs,
batched traversal evaluation, helper lifecycle, Xcode managed resources,
component ownership, and partial storage accounting. The roadmap epic is updated
with the same evidence. No new feature IDs or implementation-status changes are
introduced by that research pass; proprietary rule contents and CleanMyMac
runtime behavior remain unverified.

## Current local runtime (0.1.0)

Run `scripts/cleanmac` relative to the skill directory for the terminal wizard,
or invoke the installed `/cleanmac`, `/b:cleanmac`, `$cleanmac`, or
`/skill:cleanmac` through an agent. Machine calls use `--json`; the skill guides
scope, permission handoffs, review, exact approval, and recovery.

| Available scope | Remaining boundary |
|---|---|
| Selected-root metadata inventory, large-file review, exact duplicates, storage totals | Hidden/system/cloud/package/repository exclusions and denied scope remain explicit; allocation is not guaranteed reclaimable capacity. |
| Nine rule definitions cover npm, pip, Gradle, Xcode, Electron, Dropbox, Spotify, media/messaging caches, and unknown app data | Only verified npm content-addressed cache blobs have a conditional cleanup handler; other layouts are review-only. |
| Exact selected-file plans, user-level Foundation Trash, no-overwrite restore | Same-volume private capture and compiled helper required; no permanent deletion, directory cleanup, or privileged service. |
| Persistent ignores, saved scans/plans/receipts, interactive review and JSON CLI | Saved plans preserve exact keeper/selection state; they do not grant new approval after scope changes. |
| Applications, Xcode categories, storage and memory overview | Read-only metadata, no app uninstall, simulator/runtime deletion, system optimization, or continuous monitoring. |

Python 3.10+ is required. The optional Swift helper is built explicitly on each
Mac with Apple Command Line Tools or Xcode and is locally ad-hoc signed. A
missing helper disables cleanup, not scans. Use `capabilities` for the actual
host, permission, helper, and per-feature availability. No initial onboarding
requests administrator rights or Full Disk Access for every scan.

Development progress is recorded as `partial` in `features.json`; the issue
acceptance criteria still include broader host/OS compatibility and specialized
features. Cloud/email/browser integrations, malware engines, Photos, privilege
services, system maintenance, and background schedules remain unavailable.

## Validation record — 2026-11-13

- **267 tests executed: 266 passed, one opt-in native integration test skipped**
  in the default suite. Tests exercise real CLI subprocesses, scan-only session
  preservation, exact keeper/selection plans, exclusions, permissions, scope and
  content changes, interrupted operations, receipts, and restore conflicts.
- The native Swift helper builds on **macOS 27.2.1 arm64**, Python **3.14.7**,
  Swift **6.4.2**. Native info, rejection, and read-only preflight tests pass.
  An explicit live test verified that this host's Trash-access denial stops
  cleanup before any file moves. A full product Trash/restore roundtrip remains
  unverified with current permissions; simulated transport tests do not replace it.
- The earlier disposable native-test file was restored using normal Foundation
  APIs after exact identity/content checks, then its test workspace was removed.
  No personal files or macOS permission grants were changed.
- A 2,000-file disposable metadata benchmark completed in **0.369 seconds** with
  **5,974,782 bytes** peak traced Python allocation. This is one local measurement,
  not a cross-device performance guarantee or a comparison with CleanMyMac.
- Skill validation, Python compilation, shell syntax, whitespace checks, and
  installed-symlink invocation passed. Claude and shared Codex/Kimi skill links
  point to this source. Other macOS/architecture and complete agent-host workflows
  remain unverified; no roadmap issue was closed on this evidence alone.

To rerun: `python3 -m unittest discover -s b/skills/cleanmac/tests -q` from the
repository. Opt into the real native test only with disposable fixtures using
`CLEANMAC_NATIVE_INTEGRATION=1` and `-p test_native_roundtrip.py`; it stops without
moving files when required Trash access is denied.

## Delivery sequence

| Band | Work | Exit condition |
|---|---|---|
| P0 | Runtime, capability reporting, rules, inventory, exact plans, recovery, conversation, private state, tests | A disposable end-to-end workflow can scan, review, apply only approved items, and recover without pretending unsupported operations exist. |
| P1 | First local storage journey and related adapters | Downloads, large files, exact duplicates, selected proven cache rules, and storage drilldown work with honest accounting. Additional P1 adapters ship only when validated. |
| P2 | Applications, OS-specific maintenance, protection/privacy, monitoring and health | Each module publishes tested permissions, compatibility, coverage, and recovery behavior. |
| P3 | Cloud, email, browser, native helpers and advanced feasibility | Provider/native integration evidence exists; unsupported cases have precise handoffs or an explicit unavailable status. |

Phase is a planning band, not a strict dependency order or a promise of feature
parity. Follow each issue's dependencies. Aggregate workflows can ship with
honestly unavailable optional modules; they must not infer clean results from
skipped scans.

## Delivery modes

| Mode | Meaning |
|---|---|
| build | Implement the proposed feature and prove the acceptance criteria. |
| conditional | Enable only validated OS/app/provider/rule combinations. |
| research | Prototype and decide feasibility first; report-only or unsupported can be the correct outcome. |
| native-helper | Requires an optional native companion with capability and lifecycle validation. |
| guided | Hand off to supported native controls and verify the observed outcome. |

## Feature issues

### Foundation

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-001 | [Portable runner and capability preflight](https://github.com/joeblau/skills/issues/10) | P0 | build |
| CM-002 | [Versioned cleanup rules and exclusions](https://github.com/joeblau/skills/issues/11) | P0 | build |
| CM-003 | [Read-only inventory and honest storage accounting](https://github.com/joeblau/skills/issues/12) | P0 | build |
| CM-004 | [Selection plans bound to user approval](https://github.com/joeblau/skills/issues/13) | P0 | build |
| CM-005 | [Action executor, Trash, recovery, and verification](https://github.com/joeblau/skills/issues/14) | P0 | build |
| CM-006 | [Conversational scan, review, clean, and resume flow](https://github.com/joeblau/skills/issues/15) | P0 | build |
| CM-007 | [Session history, receipts, and private local state](https://github.com/joeblau/skills/issues/16) | P0 | build |
| CM-008 | [Compatibility fixtures and release gates](https://github.com/joeblau/skills/issues/17) | P0 | build |

### Core experience

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-009 | [Smart Care orchestration and summary](https://github.com/joeblau/skills/issues/18) | P1 | build |
| CM-010 | [My Tools, saved scopes, favorites, and ignore lists](https://github.com/joeblau/skills/issues/19) | P1 | build |
| CM-075 | [Mac health, activity statistics, and explainable insights](https://github.com/joeblau/skills/issues/84) | P2 | build |
| CM-076 | [Opt-in schedules, alerts, and background lifecycle](https://github.com/joeblau/skills/issues/85) | P3 | build |

### Cleanup

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-011 | [User cache cleanup](https://github.com/joeblau/skills/issues/20) | P1 | conditional |
| CM-012 | [System cache cleanup](https://github.com/joeblau/skills/issues/21) | P2 | research |
| CM-013 | [User log cleanup](https://github.com/joeblau/skills/issues/22) | P1 | conditional |
| CM-014 | [System log cleanup](https://github.com/joeblau/skills/issues/23) | P2 | research |
| CM-015 | [Broken login-item detection](https://github.com/joeblau/skills/issues/25) | P2 | conditional |
| CM-016 | [Damaged preference detection and reset](https://github.com/joeblau/skills/issues/26) | P2 | conditional |
| CM-017 | [Deleted-user home archive review](https://github.com/joeblau/skills/issues/27) | P2 | conditional |
| CM-018 | [Document version inventory and selective removal](https://github.com/joeblau/skills/issues/28) | P3 | native-helper |
| CM-019 | [iPhone and iPad local backup review](https://github.com/joeblau/skills/issues/29) | P2 | conditional |
| CM-020 | [Unused app localization feasibility](https://github.com/joeblau/skills/issues/30) | P3 | research |
| CM-021 | [Unused universal-binary slice feasibility](https://github.com/joeblau/skills/issues/31) | P3 | research |
| CM-022 | [Xcode build and simulator cleanup](https://github.com/joeblau/skills/issues/32) | P1 | conditional |
| CM-023 | [AI tool caches, logs, and temporary data](https://github.com/joeblau/skills/issues/33) | P1 | conditional |
| CM-024 | [Local AI model inventory and removal](https://github.com/joeblau/skills/issues/34) | P1 | conditional |
| CM-025 | [Recoverable Apple Mail attachment cleanup](https://github.com/joeblau/skills/issues/35) | P3 | research |
| CM-026 | [Startup, external-drive, and Mail Trash review](https://github.com/joeblau/skills/issues/36) | P1 | conditional |

### Protection

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-027 | [On-demand malware scanning and quarantine](https://github.com/joeblau/skills/issues/37) | P2 | conditional |
| CM-028 | [Archive, DMG, Mail, USB, and iCloud malware targets](https://github.com/joeblau/skills/issues/38) | P3 | research |
| CM-029 | [Scheduled malware scans and real-time protection feasibility](https://github.com/joeblau/skills/issues/39) | P3 | research |
| CM-030 | [Browser privacy by profile and data category](https://github.com/joeblau/skills/issues/40) | P2 | conditional |
| CM-031 | [Recent apps, documents, and servers privacy](https://github.com/joeblau/skills/issues/41) | P2 | conditional |
| CM-032 | [Remembered Wi-Fi network review](https://github.com/joeblau/skills/issues/42) | P2 | guided |
| CM-033 | [Application permission review and reset](https://github.com/joeblau/skills/issues/43) | P2 | conditional |

### Performance

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-034 | [Symptom-driven DNS cache reset](https://github.com/joeblau/skills/issues/44) | P2 | conditional |
| CM-035 | [Purgeable-space assessment and supported reclamation](https://github.com/joeblau/skills/issues/48) | P2 | research |
| CM-036 | [Memory-pressure diagnosis and RAM cleanup decision](https://github.com/joeblau/skills/issues/50) | P2 | research |
| CM-037 | [Legacy periodic maintenance scripts](https://github.com/joeblau/skills/issues/51) | P3 | conditional |
| CM-038 | [Spotlight indexing diagnosis and rebuild](https://github.com/joeblau/skills/issues/52) | P2 | conditional |
| CM-039 | [Disk-permission repair feasibility and guided alternatives](https://github.com/joeblau/skills/issues/53) | P3 | research |
| CM-040 | [Apple Mail performance and mailbox rebuild](https://github.com/joeblau/skills/issues/54) | P3 | guided |
| CM-041 | [Time Machine local snapshot review and thinning](https://github.com/joeblau/skills/issues/45) | P2 | conditional |
| CM-042 | [Login-item inventory and management](https://github.com/joeblau/skills/issues/24) | P2 | conditional |
| CM-043 | [Background agents and daemons management](https://github.com/joeblau/skills/issues/55) | P2 | conditional |

### Applications

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-044 | [Application inventory, unused apps, and compatibility](https://github.com/joeblau/skills/issues/56) | P1 | build |
| CM-045 | [App uninstall with owned-component review](https://github.com/joeblau/skills/issues/57) | P2 | conditional |
| CM-046 | [Orphaned app components and installer leftovers](https://github.com/joeblau/skills/issues/58) | P2 | conditional |
| CM-047 | [App update discovery and verified installation](https://github.com/joeblau/skills/issues/59) | P2 | conditional |

### My Clutter

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-048 | [Large and old file discovery](https://github.com/joeblau/skills/issues/60) | P1 | build |
| CM-049 | [Exact duplicate finder and keeper selection](https://github.com/joeblau/skills/issues/61) | P1 | build |
| CM-050 | [Similar-image grouping and visual review](https://github.com/joeblau/skills/issues/62) | P2 | native-helper |
| CM-051 | [Downloads by source, age, and file type](https://github.com/joeblau/skills/issues/63) | P1 | build |
| CM-052 | [Photos library integration for duplicate and similar-image review](https://github.com/joeblau/skills/issues/64) | P3 | native-helper |

### Space Lens

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-053 | [Storage map, hidden files, and folder drilldown](https://github.com/joeblau/skills/issues/65) | P1 | build |

### Cloud Cleanup

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-054 | [Cloud account adapters and action semantics](https://github.com/joeblau/skills/issues/46) | P3 | build |
| CM-055 | [iCloud Drive inventory, eviction, and deletion](https://github.com/joeblau/skills/issues/47) | P3 | native-helper |
| CM-056 | [Google Drive cleanup adapter](https://github.com/joeblau/skills/issues/66) | P3 | conditional |
| CM-057 | [OneDrive cleanup adapter](https://github.com/joeblau/skills/issues/67) | P3 | conditional |
| CM-058 | [Dropbox cleanup adapter](https://github.com/joeblau/skills/issues/68) | P3 | conditional |

### Browser Care

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-059 | [Browser tab inventory, duplicates, and old tabs](https://github.com/joeblau/skills/issues/69) | P2 | conditional |
| CM-060 | [Pause, resume, close, and recover browser tabs](https://github.com/joeblau/skills/issues/70) | P2 | conditional |
| CM-061 | [Browser CPU and memory attribution](https://github.com/joeblau/skills/issues/72) | P3 | research |

### My Tools

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-062 | [Screen Cleaning Mode native companion](https://github.com/joeblau/skills/issues/73) | P3 | native-helper |

### Email Cleanup

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-063 | [Gmail connection and metadata-only inventory](https://github.com/joeblau/skills/issues/74) | P3 | conditional |
| CM-064 | [Email Smart Cleanup suggestions](https://github.com/joeblau/skills/issues/75) | P3 | build |
| CM-065 | [Newsletter review and unsubscribe actions](https://github.com/joeblau/skills/issues/77) | P3 | conditional |
| CM-066 | [Spam review and cleanup](https://github.com/joeblau/skills/issues/78) | P3 | conditional |
| CM-067 | [Draft review and explicit deletion](https://github.com/joeblau/skills/issues/79) | P3 | conditional |
| CM-068 | [Sender-based mail review, archive, trash, and restore](https://github.com/joeblau/skills/issues/76) | P3 | conditional |

### Monitoring

| ID | Feature issue | Band | Mode |
|---|---|---|---|
| CM-069 | [CPU activity, heavy consumers, and unresponsive apps](https://github.com/joeblau/skills/issues/71) | P2 | conditional |
| CM-070 | [Memory pressure and process memory monitoring](https://github.com/joeblau/skills/issues/49) | P2 | conditional |
| CM-071 | [Storage health and low-space monitoring](https://github.com/joeblau/skills/issues/80) | P2 | conditional |
| CM-072 | [Battery condition, cycle, and drain monitoring](https://github.com/joeblau/skills/issues/81) | P2 | conditional |
| CM-073 | [Network activity and optional speed checks](https://github.com/joeblau/skills/issues/82) | P2 | conditional |
| CM-074 | [Connected-device storage and battery overview](https://github.com/joeblau/skills/issues/83) | P2 | conditional |

## Screenshot traceability

| Image | Screen | Feature IDs |
|---|---|---|
| 1 | Smart Care | CM-009, CM-011, CM-027, CM-034, CM-047, CM-049 |
| 2 | Junk Cleanup | CM-011, CM-012, CM-013, CM-014, CM-015, CM-016, CM-017, CM-018, CM-019, CM-020, CM-021, CM-022, CM-023, CM-024, CM-025, CM-026 |
| 3 | Protection | CM-027, CM-028, CM-029, CM-030, CM-031, CM-032, CM-033 |
| 4 | Performance | CM-034, CM-035, CM-036, CM-037, CM-038, CM-039, CM-040, CM-041, CM-042, CM-043 |
| 5 | Applications | CM-044, CM-045, CM-046, CM-047 |
| 6 | My Clutter | CM-048, CM-049, CM-050, CM-051, CM-052 |
| 7 | Space Lens | CM-053 |
| 8 | Cloud Cleanup | CM-054, CM-055, CM-056, CM-057, CM-058 |
| 9 | My Tools (first 15 tiles) | CM-010, CM-059, CM-060, CM-061, CM-062, CM-063, CM-064, CM-065, CM-066, CM-067, CM-068, CM-023, CM-024, CM-046, CM-047, CM-033, CM-043, CM-051, CM-049, CM-048, CM-042, CM-025, CM-034, CM-035, CM-036, CM-037, CM-038, CM-039, CM-040, CM-041, CM-027, CM-028, CM-029 |
| 10 | My Tools (remaining six tiles) | CM-010, CM-030, CM-031, CM-032, CM-050, CM-052, CM-011, CM-012, CM-013, CM-014, CM-015, CM-016, CM-017, CM-018, CM-019, CM-020, CM-021, CM-022, CM-041, CM-026, CM-045 |

### Every My Tools tile

| Tile | Image | Implementation issues |
|---|---|---|
| Browser Care | 9 | [CM-059](https://github.com/joeblau/skills/issues/69), [CM-060](https://github.com/joeblau/skills/issues/70), [CM-061](https://github.com/joeblau/skills/issues/72) |
| Screen Cleaning Mode | 9 | [CM-062](https://github.com/joeblau/skills/issues/73) |
| Email Cleanup | 9 | [CM-063](https://github.com/joeblau/skills/issues/74), [CM-064](https://github.com/joeblau/skills/issues/75), [CM-065](https://github.com/joeblau/skills/issues/77), [CM-066](https://github.com/joeblau/skills/issues/78), [CM-067](https://github.com/joeblau/skills/issues/79), [CM-068](https://github.com/joeblau/skills/issues/76) |
| AI Clutter | 9 | [CM-023](https://github.com/joeblau/skills/issues/33), [CM-024](https://github.com/joeblau/skills/issues/34) |
| App Leftovers | 9 | [CM-046](https://github.com/joeblau/skills/issues/58) |
| App Updater | 9 | [CM-047](https://github.com/joeblau/skills/issues/59) |
| Application Permissions | 9 | [CM-033](https://github.com/joeblau/skills/issues/43) |
| Background Items | 9 | [CM-043](https://github.com/joeblau/skills/issues/55) |
| Downloads | 9 | [CM-051](https://github.com/joeblau/skills/issues/63) |
| Duplicate Finder | 9 | [CM-049](https://github.com/joeblau/skills/issues/61) |
| Large and Old Files | 9 | [CM-048](https://github.com/joeblau/skills/issues/60) |
| Login Items | 9 | [CM-042](https://github.com/joeblau/skills/issues/24) |
| Mail Attachments | 9 | [CM-025](https://github.com/joeblau/skills/issues/35) |
| Maintenance Tasks | 9 | [CM-034](https://github.com/joeblau/skills/issues/44), [CM-035](https://github.com/joeblau/skills/issues/48), [CM-036](https://github.com/joeblau/skills/issues/50), [CM-037](https://github.com/joeblau/skills/issues/51), [CM-038](https://github.com/joeblau/skills/issues/52), [CM-039](https://github.com/joeblau/skills/issues/53), [CM-040](https://github.com/joeblau/skills/issues/54), [CM-041](https://github.com/joeblau/skills/issues/45) |
| Malware Finder | 9 | [CM-027](https://github.com/joeblau/skills/issues/37), [CM-028](https://github.com/joeblau/skills/issues/38), [CM-029](https://github.com/joeblau/skills/issues/39) |
| Privacy Items | 10 | [CM-030](https://github.com/joeblau/skills/issues/40), [CM-031](https://github.com/joeblau/skills/issues/41), [CM-032](https://github.com/joeblau/skills/issues/42) |
| Similar Images | 10 | [CM-050](https://github.com/joeblau/skills/issues/62), [CM-052](https://github.com/joeblau/skills/issues/64) |
| System Junk | 10 | [CM-011](https://github.com/joeblau/skills/issues/20), [CM-012](https://github.com/joeblau/skills/issues/21), [CM-013](https://github.com/joeblau/skills/issues/22), [CM-014](https://github.com/joeblau/skills/issues/23), [CM-015](https://github.com/joeblau/skills/issues/25), [CM-016](https://github.com/joeblau/skills/issues/26), [CM-017](https://github.com/joeblau/skills/issues/27), [CM-018](https://github.com/joeblau/skills/issues/28), [CM-019](https://github.com/joeblau/skills/issues/29), [CM-020](https://github.com/joeblau/skills/issues/30), [CM-021](https://github.com/joeblau/skills/issues/31), [CM-022](https://github.com/joeblau/skills/issues/32) |
| Time Machine Snapshot | 10 | [CM-041](https://github.com/joeblau/skills/issues/45) |
| Trash Bins | 10 | [CM-026](https://github.com/joeblau/skills/issues/36) |
| Uninstaller | 10 | [CM-044](https://github.com/joeblau/skills/issues/56), [CM-045](https://github.com/joeblau/skills/issues/57) |

## Scope boundaries

The roadmap implements independently documented capabilities. It does not copy
MacPaw's artwork, signatures, rule database, subscription UI, or marketing
announcements. Historical features absent from the current baseline stay out of
scope unless new evidence or a user request brings them back.

No current file means a scanner has shipped merely because it has an issue.
Keep the skill's capability notice, implementation status, source records, and
issue state consistent as work lands. Revalidate version-sensitive sources
before enabling each adapter.
