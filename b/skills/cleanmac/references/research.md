# CleanMac research dossier

Research date: **2026-11-13**. Baseline: the user's ten screenshots and current
CleanMyMac documentation, with the full MacPaw Store/Setapp feature surface.
The release page lists **5.7.4**, dated November 6, 2026. Older CleanMyMac X/3
features are not silently treated as current requirements.
[Release history](https://macpaw.com/cleanmymac/whats-new)

## Evidence and limits

This research used public product documentation, upstream platform/provider
documentation, and read-only inspection of installed macOS command manuals.
A subsequent read-only static inspection of installed CleanMyMac 5.7.4 added
bundle metadata, signed entitlements, imported symbols, component strings, and
knowledge-base file sizes/headers. Database contents were not decoded. No cleanup,
runtime scan tracing, permission changes, personal-account connections, scanner
benchmarks, or removal tests were performed. Proposed implementation paths remain
proposals; static references do not establish live behavior or safety.

The main finding: reproducing the visible categories is feasible as a roadmap;
claiming the same detection, safety, or recovery guarantees is not justified.
MacPaw publishes its categories and some selection behavior, but not its complete
path rules, ownership heuristics, classification algorithms, or malware signatures.
Its Safety Database explicitly contains maintained app-specific exceptions, and
Moonlock is its own antimalware engine.
[Safety Database](https://macpaw.com/support/cleanmymac/knowledgebase/safety-database),
[Moonlock](https://macpaw.com/support/cleanmymac/knowledgebase/moonlock-engine)

`features.json` separates a short documented observation (`evidence`) from
CleanMac's proposed mechanics (`implementation`) and executable acceptance
criteria. `sources.json` records primary-source URLs and local manual provenance.
The original research pass left every feature planned. The local runtime now
marks implemented subsets as `status: partial`; see [current runtime scope](roadmap.md#current-local-runtime-010).
Static product observations are still distinct from CleanMac fixture validation.

## Installed-app inspection

On **2026-11-13**, the installed bundle was `/Applications/CleanMyMac_5.app`,
version **5.7.4**, build **50704.0.2611060828**. Source record: `local_app`
in [sources.json](sources.json). Inventory paths were resolved with `rg --files`
and `fd` before inspecting metadata with `plutil`, entitlements with
`codesign`, imports with `nm`, and embedded text with `strings`.

| Observation | Interpretation and limit | Existing issues |
|---|---|---|
| Main executable lacks `com.apple.security.app-sandbox`; `PermissionsKit` contains a Full Disk Access authorizer. | This build is not App Sandbox-enabled. TCC, ordinary permissions, and helper authorization remain distinct; actual grants were not verified. | [CM-001 / #10](https://github.com/joeblau/skills/issues/10) |
| `SMPrivilegedExecutables` declares `com.macpaw.CleanMyMac5.Agent`; the bundle includes the Agent and privileged operation/enumeration components. | A privileged helper is available to authorize selected operations. Bundled does not mean installed, authorized, or running. | [CM-005 / #14](https://github.com/joeblau/skills/issues/14), [CM-012 / #21](https://github.com/joeblau/skills/issues/21), [CM-014 / #23](https://github.com/joeblau/skills/issues/23) |
| `LegacyScanningCore` imports `getattrlistbulk`; `FileManagerService` imports `fts_open`/`fts_read` and `MDItemCreate`/`MDItemCopyAttribute`. | Native enumeration and supplemental metadata are implementation leads. Their presence proves neither throughput nor exact reclaimable-space accounting. | [CM-003 / #12](https://github.com/joeblau/skills/issues/12), [CM-053 / #65](https://github.com/joeblau/skills/issues/65) |
| `KnowledgeBaseService`, SQLCipher, ignore-rule types, `info.cmmkb` (299008 bytes), and `userInfo.cmmkb` (2048 bytes) are bundled. | The files are opaque and were not decoded. MacPaw's documentation supports the role of maintained exceptions, but the exact proprietary rules remain unknown. | [CM-002 / #11](https://github.com/joeblau/skills/issues/11) |
| `SystemJunkScanning` contains Electron, Gradle, Spotify, Dropbox, sandbox-container, and hardcoded-path cache task identifiers. | Candidate coverage extends beyond generic cache directories. These are research leads requiring independent app/version evidence and preservation fixtures. | [CM-011 / #20](https://github.com/joeblau/skills/issues/20) |
| Xcode identifiers cover DerivedData, module caches, archives, device support/logs, CoreSimulator caches, downloaded runtimes, and devices; `simctl` strings include a private framework path. | Distinct managed resources need distinct rules. Resolve supported tooling through the selected developer environment; do not adopt a private path as a supported contract. | [CM-022 / #32](https://github.com/joeblau/skills/issues/32) |
| `UninstallerScanning` references container metadata, preferences, saved state, cookies, logs, support files, documentation, and plugin locations. | These suggest ownership research categories; they do not disclose how ownership or orphan status is proven. | [CM-045 / #57](https://github.com/joeblau/skills/issues/57), [CM-046 / #58](https://github.com/joeblau/skills/issues/58) |
| Separate scanner frameworks, symlink/protected-file checks, and path/type/size ignore rules are present. No bundled kernel or system extension was found. | Prefer bounded scanner adapters and explicit capability reporting. Absence from this bundle is not proof about all installed services or future editions. | [CM-008 / #17](https://github.com/joeblau/skills/issues/17) |

Concrete cache-location strings included Spotify's
`~/Library/Application Support/Spotify/PersistentCache/Storage`, Adobe's
`~/Library/Application Support/Adobe/Common/Media Cache Files`, Telegram's
app/group-container and Application Support layouts, and WeChat's app container.
These observations are not deletion allowlists. Paths can contain offline media,
settings, shared data, or layouts that changed since this build.

The useful architectural inference is that broad authorized access, efficient
metadata traversal, and maintained app-specific rules work together. No
Apple-private disk-scanning entitlement was observed in the main executable or
Agent. This does not establish that every API used by the product is public.

MacPaw independently documents Full Disk Access, administrator-authorized Agent
operations, and preservation/quit/reopen rules in its Safety Database:
[Full Disk Access](https://macpaw.com/support/cleanmymac/knowledgebase/full-disk-access),
[Agent](https://macpaw.com/support/cleanmymac/knowledgebase/cleanmymac-agent),
[Safety Database](https://cms-static.macpaw.com/support/cleanmymac/knowledgebase/safety-database).
[Apple's App Sandbox documentation](https://developer.apple.com/documentation/security/app-sandbox)
explains the separate sandbox boundary.

The inspection update retained the twelve linked feature issues' original scope
and planned status; subsequent runtime progress is tracked separately.
Their additions distinguish observed evidence from CleanMac's proposed design
and require behavioral validation before any cleanup rule is enabled.

## What scanning actually needs to do

The proposed pipeline has five independent jobs:

1. **Discover** selected local roots, application identities, volumes, browser
   profiles, or provider accounts using metadata first.
2. **Classify** with versioned rules: ownership, disposable versus authored data,
   current use, backup recoverability, cloud state, and exceptions.
3. **Compare** where necessary: content hashes for exact duplicates, perceptual
   candidates for similar images, installed versus published app versions, or an
   actual malware engine with current signatures.
4. **Plan** exact actions against stable item identities. A size or a label such
   as “cache,” “old,” or “duplicate” is insufficient reason to delete.
5. **Verify** the selected action and recovery record. A successful command exit
   is not proof that a rebuild finished or that bytes became free.

These are CleanMac design decisions, not a claim about CleanMyMac internals.

## Product behavior that shapes the design

Smart Care is an aggregate workflow, not an exhaustive scan of every tool. Its
documentation names cleanup, quick malware detection, maintenance, app updates,
and duplicate Downloads. The detailed feature matrix and module pages differ
on some maintenance/update details; retain the distinction and verify live-build
behavior before asserting exact parity.
[Smart Care](https://macpaw.com/support/cleanmymac/knowledgebase/smart-care),
[edition matrix](https://macpaw.com/support/cleanmymac/knowledgebase/missing-features)

My Clutter's published thresholds are at least 50 MB for large files and above
100 KB for duplicates/similar images. CleanMac can expose those as parity presets
while allowing custom thresholds. Exact duplicate comparison and visual
similarity need separate algorithms and different review expectations.
[Large files](https://macpaw.com/support/cleanmymac/knowledgebase/large-and-old),
[duplicates](https://macpaw.com/support/cleanmymac/knowledgebase/duplicate-files),
[similar images](https://macpaw.com/support/cleanmymac/knowledgebase/similar-images)

Current My Clutter documentation describes permanent filesystem deletion, with
Photos assets handled differently. CleanMac deliberately proposes native Trash
for eligible local files and a separate permanent-erasure action. This makes
recovery possible but keeps the storage allocated until it is actually removed.
[Downloads behavior](https://macpaw.com/support/cleanmymac/knowledgebase/downloads),
[Apple Trash API](https://developer.apple.com/documentation/foundation/filemanager/trashitem(at:resultingitemurl:))

AI cleanup must protect the executing agent's own data. MacPaw explicitly names
temporary data from Claude, Codex, Gemini, and Cursor, plus Ollama/LM Studio
models. CleanMac will require per-version rules and model-manager-aware removal;
Hugging Face support is an additional proposed adapter, not claimed product parity.
[AI Clutter](https://macpaw.com/support/cleanmymac/knowledgebase/ai-clutter),
[Hugging Face cache management](https://huggingface.co/docs/huggingface_hub/en/guides/manage-cache)

Cloud Cleanup distinguishes deleting cloud content from removing a local synced
copy. Native iCloud eviction is materially different from ordinary deletion.
Google Drive and OneDrive expose provider-specific trash/recycle behavior; their
REST APIs are not generic local-cache-eviction APIs.
[Cloud Cleanup](https://macpaw.com/support/cleanmymac/knowledgebase/cloud-cleanup),
[Apple iCloud storage](https://developer.apple.com/documentation/xcode/reducing-your-app-s-disk-usage),
[Drive](https://developers.google.com/workspace/drive/api/guides/delete),
[OneDrive](https://learn.microsoft.com/en-us/graph/api/driveitem-delete?view=graph-rest-1.0)

Email Cleanup currently documents Gmail and metadata-based analysis. CleanMac
should enumerate message metadata locally and apply account-specific actions.
OAuth grants may be broader than the data the implementation uses; the product
must describe that accurately. Gmail draft deletion is distinct from moving a
message to Trash.
[Email Cleanup](https://macpaw.com/support/cleanmymac/knowledgebase/email-cleanup-get-started),
[Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes),
[draft deletion](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.drafts/delete)

Browser Care is a Chrome extension with tab management and resource reporting.
The public processes API is Dev-channel-only, so stable per-tab telemetry needs
a feasibility decision. Chrome's current browsingData API marks password
deletion as removed since Chrome 144; a successful no-op is not a successful
cleanup.
[Browser Care](https://macpaw.com/support/cleanmymac/knowledgebase/browser-care),
[process API](https://developer.chrome.com/docs/extensions/reference/api/processes),
[browsing data](https://developer.chrome.com/docs/extensions/reference/api/browsingData)

## macOS implementation boundaries

| Area | Proposed mechanism | Constraint or unresolved question |
|---|---|---|
| File discovery | Metadata enumeration, `lstat`, native resource keys, selected roots | Packages, hard links, APFS clones, sparse files, placeholders, TCC, and mount aliases require explicit treatment. |
| Local removal | Foundation Trash API plus journal and identity checks | Trash is recovery storage, not reclaimed capacity; pathname checks alone do not solve races. |
| Preferences | Format validation and an app-aware reset | A valid unfamiliar preference is not junk; active preference daemons complicate raw file edits. |
| Document versions | `NSFileVersion` with coordinated access | Never erase the whole revision store; losing versions is irreversible. |
| Login/background items | Supported metadata, version-tested diagnostics, Settings, targeted `launchctl` for legacy jobs | `SMAppService` controls an app's own helpers; diagnostic text is not a stable enumeration API. |
| Permissions | Settings and narrowly scoped `tccutil reset` where applicable | Reset may prompt again; it is not a permanent deny or a public universal TCC query/write interface. |
| Spotlight | `mdutil -s`, then selected-volume rebuild if needed | Index work is asynchronous and can be expensive. |
| Local snapshots | `tmutil` list/thin/delete, bound to a volume | Date deletion can span mounted disks; snapshot loss removes local restore history. |
| Malware | Optional ClamAV adapter with current signatures | Separate product and coverage; `spctl`/`codesign`/XProtect status is not a full on-demand antivirus scan. |
| Similar images | Bounded local decoding, candidate hashes, Vision feature prints | Similarity thresholds require fixture evaluation; never automatic personal-photo removal. |
| Photos library | PhotoKit authorization and change requests | Never directly delete library package internals; cloud synchronization matters. |
| Screen Cleaning Mode | Foreground native UI and validated input-control helper | Must release input independently of the agent and fail closed when permissions/control are insufficient. |

API support: [document versions](https://developer.apple.com/documentation/foundation/nsfileversion/removeotherversionsofitem(at:)),
[service management](https://support.apple.com/en-gb/guide/deployment/depdca572563/1/web/1.0),
[Vision](https://developer.apple.com/documentation/vision/vngenerateimagefeatureprintrequest),
[PhotoKit](https://developer.apple.com/documentation/photos/phassetchangerequest),
[ClamAV](https://docs.clamav.net/manual/Usage/Scanning.html).
Native command details were read from the installed `tmutil(8)`, `tccutil(1)`,
`launchctl(1)`, `mdutil(1)`, `purge(8)`, `diskutil(8)`, and `dscacheutil(1)` manuals.
Revalidate those on every supported OS; command availability is not a claim of
cross-version compatibility.

## Decisions that require research before mutation

- **Languages and universal binaries:** inventory is useful, but production app
  modifications need signing, launch, nested-code, and update validation. Never
  remove signatures or re-sign third-party software to make a check pass.
  [Apple code signing](https://developer.apple.com/library/archive/technotes/tn2206/_index.html)
- **Permission repair:** the installed `diskutil(8)` manual records removal of
  repairPermissions/verifyPermissions in OS X 10.11. CleanMyMac's proprietary
  implementation is not documented. A recursive chmod/chown is not a substitute.
- **RAM:** the installed `purge(8)` manual describes cold disk-cache testing and
  explicitly excludes anonymous allocations. Diagnose pressure instead of
  manufacturing “free RAM.” MacPaw limits its RAM task to Intel systems.
- **Periodic scripts:** MacPaw documents availability on Ventura 13 and earlier.
  Do not recreate removed legacy maintenance on a newer system.
- **Purgeable space:** macOS automatically manages local snapshots. Report
  storage truthfully and avoid disk-filling tricks to trigger eviction.
  [Maintenance](https://macpaw.com/support/cleanmymac/knowledgebase/maintenance-tasks),
  [Apple snapshots](https://support.apple.com/en-lb/102154)
- **Mail attachments:** metadata must prove the remote copy is recoverable and
  the local copy unmodified; unsupported layouts stay report-only.
  [Mail attachments](https://macpaw.com/support/cleanmymac/knowledgebase/mail-attachments)
- **Real-time antimalware:** a persistent native service needs separate lifecycle,
  entitlement, distribution, and detection work. Scheduled scanning is a useful
  distinct capability, not equivalent real-time protection.
  [Endpoint Security](https://developer.apple.com/documentation/endpointsecurity.md)

## Coverage and scope

The roadmap includes every module and all **21 My Tools entries** in the supplied
screenshots, the system-junk and maintenance subfeatures documented by MacPaw,
four cloud providers, browser and email workflows, and the Menu/health functions
found during research. Every feature has an implementation or feasibility issue.
The feature-to-issue map is in [roadmap.md](roadmap.md).

Commercial licensing, upgrade banners, marketing announcements, payment alerts,
MacPaw artwork, and the product's proprietary datasets are not CleanMac features.
Historical Shredder/Extensions/Photo Junk/Launch Services tasks from older
CleanMyMac editions are not silently added. Reopen scope if a current build or
the user supplies evidence that those are required. No secure-erase promise is
made for SSDs or APFS.

## Recommended delivery

Start with portable capability reporting, inventory, exact plans, and recovery.
Then deliver a complete, small storage journey: Downloads, large files, exact
duplicates, approved user cache rules, and a storage map. Add apps and validated
OS maintenance next. Keep cloud/email, sensitive data, app-bundle editing, and
persistent native companions behind their own acceptance evidence.

This sequencing delivers the conversational experience early without claiming
76 implemented capabilities merely because the catalog can describe them.
