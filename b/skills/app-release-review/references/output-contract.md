# Audit and GitHub task contract

The helper renders `CHECKLIST.md`, `GITHUB-TASKS.md` and individual issue bodies. The reviewing agent supplies the evidence, policy interpretation, `RELEASE-REVIEW.md` and substantive materials. Structural validation is not an app audit or policy certification.

## Audit JSON

Use UTF-8 JSON with these fields:

- `schema_version`: `1`.
- `scope`: `app`, `version`, `reviewed_on` and `release_date` (ISO `YYYY-MM-DD`), `platforms` (nonempty subset of `ios`, `android`), `repository`, `commit`, `builds`, `markets`, `locales`, `devices`, `accounts`, `features`, `limitations`. Mark unavailable facts explicitly `unknown`; supplied desktop builds belong in limitations/reference evidence, not mobile build slots.
- `sources`: array of `{id, title, url, checked_on, status, section, effective, applicability, interpretation}`. `status` is `verified` or `unverified`. Use direct HTTP(S) primary URLs. Record an actual check/attempt date and relevant interpretation, not just a source index link. Never copy the bundled researched date as this audit's check date.
- `additional_checks`: optional array of catalog-shaped definitions with unique `EXTRA-*` IDs; include `platforms`, `area`, `title`, `obligation`, `trigger`, `verify`, `source_ids`. Add actual checks discovered through current console/policy research.
- `checks`: every catalog check/requested-platform pair once, plus additional checks. Each row has `{id, platform, status, obligation, trigger, evidence, source_ids, task_ids}`. Evidence is a nonempty string array containing resolved file/line, test/recording/build/console export or the exact missing evidence/exclusion reason. `source_ids` refer to this audit's refreshed source records. IDs are scoped by platform, e.g. `DATA-08@ios`. Obligation overrides need their reason in `trigger` and authoritative source interpretation.
- `tasks`: array defined below. Every `fail`/`unverified` row links to at least one task and every linked task lists that row in `check_refs`.
- `artifact_inventory`: array of `{path, status, owner_role, task_ids}` with status `draft`, `missing`, `verified` or `not-applicable`. Include platform listing and notes, capture/asset plan, privacy/disclosure worksheet, support/deletion docs, accessibility evidence and triggered compliance docs. Missing artifacts link to tasks. Claims of verified artifacts need inspection evidence in their corresponding checks.

## Task fields

| Field | Meaning |
|---|---|
| `id` | Safe stable ID, e.g. `REL-IOS-DELETE`; uppercase letters/digits/hyphens only, at most 80 characters |
| `title` | Concrete outcome; include store/platform when it changes the work |
| `platforms` | Nonempty subset of audit platforms |
| `check_refs` | Exact `CHECK-ID@platform` references this task resolves |
| `kind` | `native`, `backend`, `console`, `documentation`, `assets` or `verification` |
| `area` | Review domain, e.g. accessibility, privacy, payments, metadata or build |
| `priority` | `P0` immediate critical issue; `P1` before applicable release gate; `P2` quality/marketing improvement. Priority and mandate are separate. |
| `blocker`, `blocker_reason` | Boolean and explanation. Unresolved required/conditional checks are release gates pending proof. Recommended work blocks only with an explicit project rationale. |
| `blocking_platforms` | Exact stores/platforms blocked by this task; subset of `platforms`, empty when `blocker` is false. A shared task need not block both stores. |
| `owner_role` | Responsible role (e.g. iOS engineer, Android engineer, backend owner, publisher, designer); do not invent a GitHub assignee |
| `evidence` | Nonempty array of app-specific observations or exact missing evidence |
| `source_ids` | Refreshed sources supporting this task; covers linked checks' sources |
| `remediation` | Specific implementation/console/document/test work and expected outcome |
| `acceptance` | Nonempty array of concrete acceptance statements rendered as GitHub checkboxes |
| `depends_on` | IDs of prerequisite tasks; graph must be acyclic. Don't hide prerequisites only in prose. |
| `artifacts` | Expected output/evidence paths or console fields; may be empty if no saved artifact is needed |
| `dedupe_key` | Stable app/version/platform/check/outcome key. Persist on reruns; rendered visibly in the issue. |
| `existing_issue` | Optional confirmed `https://github.com/OWNER/REPO/issues/N` for equivalent existing work. Draft should retain acceptance/evidence. |

Example fragment (not a complete audit):

```json
{
  "id": "REL-ANDROID-DELETE-WEB",
  "title": "Provide an external account-deletion request page for Google Play",
  "platforms": ["android"],
  "check_refs": ["DATA-09@android"],
  "kind": "documentation",
  "area": "privacy",
  "priority": "P1",
  "blocker": true,
  "blocking_platforms": ["android"],
  "blocker_reason": "Account creation triggers Play's external deletion resource requirement.",
  "owner_role": "Web/backend owner",
  "evidence": ["Auth flow permits account creation; no deletion URL was supplied in console evidence."],
  "source_ids": ["G-DELETE"],
  "remediation": "Publish an app-specific request page backed by the tested deletion service and configure its URL in Play Console.",
  "acceptance": [
    "A signed-out browser can request deletion without installing the app.",
    "The page identifies the app/developer and explains affected data, retained exceptions and timing.",
    "A synthetic-account request reaches the deletion workflow; attach evidence and verify the console URL."
  ],
  "depends_on": ["REL-DELETE-BACKEND"],
  "artifacts": ["materials/deletion-flow.md", "Play Console: Data safety > Data deletion URL"],
  "dedupe_key": "example:v1:android:DATA-09:external-deletion"
}
```

Keep acceptance specific to the actual app, devices/build and evidence needed. A missing console export becomes a verification task; do not write "fix the failed declaration" without proof that it failed. A systemic fix can cover multiple rows when the same owner/outcome closes them all; a task may cover both platforms only with distinct platform acceptance. Keep native implementation separate from a console declaration/document outcome when different owners or prerequisites matter.

## Render and review

```bash
python3 <skill-dir>/scripts/render_tasks.py <audit-dir>/audit.json --out <audit-dir>
# Re-render the known generated files after editing audit.json:
python3 <skill-dir>/scripts/render_tasks.py <audit-dir>/audit.json --out <audit-dir> --replace
```

The helper validates before writing and refuses existing generated paths unless `--replace` is given. It leaves unrelated files and `published-issues.json` alone. On a rerun with changed task IDs, use a fresh output directory or reconcile obsolete issue drafts explicitly. No helper operation calls GitHub or a store.

Inspect every rendered task body for accurate obligation/applicability, reproducibility, source and checked date, prerequisites, acceptance and secret-free content. Validate materials visually where applicable. `CHECKLIST.md` includes passes and exclusions; recommendation tasks stay visibly distinguishable from store gates. `RELEASE-REVIEW.md` must state separately for each store: supported scope, actual tested build, unresolved gates, missing evidence and human readiness assessment. Do not use a coverage percentage as proof of release readiness.

## Publishing authorized tasks

Inspect repository conventions and existing issues. Reuse or update an existing issue only within authorization. Search using the visible `Release task key: ...` in each body. Create via structured connector arguments or `gh --body-file`; keep exact newlines. On uncertain responses, query that key before retrying to prevent duplicate issues. Record successful issue URL/number/key/task ID in `published-issues.json`; do not duplicate confirmed publications. Never put reviewer credentials in issue bodies, logs or local public artifacts.
