---
name: x-post
description: |
  MANUAL TRIGGER ONLY: use when the user explicitly invokes x-post through their agent’s skill command or asks to run this skill.
  Research thought leaders on X, generate content (1 thread + 4 posts),
  and present for review. Uses research, strategy, and creation stages with a quality gate.
disable-model-invocation: true
---

# x-post: Content Pipeline

## Agent compatibility

Use this skill with Claude Code (`/b:x-post` as a plugin or `/x-post` locally), Codex (`$x-post`), or Kimi Code (`/skill:x-post`). Resolve bundled files relative to the directory containing this `SKILL.md`, following symlinks to the source directory. Use the host’s available file and shell tools; tool names are not requirements. Shell commands require a local execution environment and the listed dependencies.

Research what thought leaders are saying, generate informed X content, and present it for review.

## Setup

Requires `X_BEARER_TOKEN` environment variable for X API access. Falls back to the host’s web search capability if not set. If neither API access nor web search is available, report the missing capability before generating research-based content.

## Pipeline

```
Phase 1: Research → Phase 2: Strategy → Phase 3: Creation + Quality Gate → Present
```

## Step 1: Initialize

Set `XPOST_DIR` to the absolute, resolved directory containing this `SKILL.md`. Treat all `x-post/…` references below as relative to that directory (drop the `x-post/` prefix). Keep bundled assets read-only.

```bash
# Set this to the resolved skill directory before running the remaining commands.
XPOST_DIR="/absolute/path/to/x-post"
# Persistent outputs stay outside the skill installation. XPOST_DATA can override it.
XPOST_DATA="${XPOST_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/b-skills/x-post}"
TODAY=$(date +%Y-%m-%d)
WORKSPACE="$XPOST_DATA/workspace/$TODAY"
```

Create the workspace directory:

```bash
mkdir -p "$WORKSPACE"
```

Read the config file at `x-post/config.md` for the default topic. If the user provided a topic as an argument, use that instead.

Read `x-post/agents/SHARED.md` — this is prepended to every agent prompt.

## Step 2: Check for Resume

Read `$WORKSPACE/pipeline-state.md` if it exists. Check the last recorded phase:
- If `phase: complete` and `$WORKSPACE/approved-content.md` exists: just present the approved content to the user and stop.
- If a phase is incomplete: resume from that phase.
- If no pipeline-state.md: start from Phase 1.

## Execution across agents

Use the host’s subagent/delegation capability when available and permitted. Otherwise perform each role sequentially in the current session, reading the same role files and writing the same stage outputs. “Dispatch” below means either method; do not require a tool literally named `Agent` or a particular `subagent_type`. Give the research role shell/HTTP and web search access when available. Keep the review pass separate from drafting even when using one agent. This skill creates drafts for review; it does not publish posts.

## Step 3: Phase 1 — Research

Write to pipeline-state.md: `phase: research | status: started`

Dispatch the **Research Agent** using the execution method above:

**Prompt construction:**
1. Read `x-post/agents/SHARED.md`
2. Read `x-post/agents/research-agent/AGENT.md`
3. Read `x-post/agents/research-agent/SOUL.md`
4. Combine all three into the agent prompt
5. Append: "Topic: [topic]. Write your output to: $WORKSPACE/research.md"

Use the available shell tool for `curl` and the host’s web search tool for fallback research.

If the agent fails or times out, retry once. If it fails again, abort the pipeline and tell the user.

Write to pipeline-state.md: `phase: research | status: complete`

## Step 4: Phase 2 — Strategy

Write to pipeline-state.md: `phase: strategy | status: started`

Read `$WORKSPACE/research.md`.

Dispatch the **Content Strategist (strategy mode)** using the execution method above:

**Prompt construction:**
1. Read `x-post/agents/SHARED.md`
2. Read `x-post/agents/content-strategist/AGENT.md` — include only the **Strategy Mode** section
3. Read `x-post/agents/content-strategist/SOUL.md`
4. Combine all three into the agent prompt
5. Append the full contents of research.md as context
6. Append: "Write your output to: $WORKSPACE/strategy.md"

If the strategist produces empty output, abort and alert the user.

Write to pipeline-state.md: `phase: strategy | status: complete`

## Step 5: Phase 3 — Creation

Write to pipeline-state.md: `phase: creation | status: started`

Read `$WORKSPACE/strategy.md`.

Dispatch the **Content Creator** using the execution method above:

**Prompt construction:**
1. Read `x-post/agents/SHARED.md`
2. Read `x-post/agents/content-creator/AGENT.md`
3. Read `x-post/agents/content-creator/SOUL.md`
4. Combine all three into the agent prompt
5. Append the full contents of strategy.md as context
6. Append: "Write your output to: $WORKSPACE/drafts.md"

Write to pipeline-state.md: `phase: creation | status: complete`

## Step 6: Quality Gate

Write to pipeline-state.md: `phase: quality-gate | status: started | revision: 0`

Read `$WORKSPACE/drafts.md` and `$WORKSPACE/strategy.md`.

Dispatch the **Content Strategist (review mode)** using the execution method above:

**Prompt construction:**
1. Read `x-post/agents/SHARED.md`
2. Read `x-post/agents/content-strategist/AGENT.md` — include only the **Review Mode** section
3. Read `x-post/agents/content-strategist/SOUL.md` — include only the **As Reviewer** section
4. Combine into the agent prompt
5. Append the full contents of drafts.md and strategy.md
6. Append: "Write approved posts to: $WORKSPACE/approved-content.md. Write revision feedback to: $WORKSPACE/review-1.md. If all posts pass, only write approved-content.md."

### Revision Loop

After the quality gate:
- If `$WORKSPACE/approved-content.md` exists and no `review-N.md` was created: all posts passed. Proceed to Step 7.
- If `review-N.md` exists: some posts need revision.
  1. Read the revision count from pipeline-state.md
  2. If revision count < 2:
     - Re-dispatch the Content Creator with review-N.md as input instead of strategy.md
     - Append: "Only rewrite the posts listed in the review. Keep passing posts unchanged. Write to: $WORKSPACE/drafts.md"
     - Re-dispatch the Strategist reviewer with the updated drafts
     - Increment revision count in pipeline-state.md
  3. If revision count >= 2:
     - Write remaining unreviewed drafts to `$WORKSPACE/needs-review.md`
     - Tell the user: "Some posts didn't pass quality review after 2 revisions. See needs-review.md."

Write to pipeline-state.md: `phase: quality-gate | status: complete`

## Step 7: Present Results

Write to pipeline-state.md: `phase: complete | status: done`

Read `$WORKSPACE/approved-content.md` and present the content to the user.

Show each post with its type and scheduled time. Ask the user to review.

If `$WORKSPACE/needs-review.md` exists, mention it: "Note: [N] posts need manual review — see needs-review.md in today's workspace."

## Step 8: Cleanup

Delete workspace folders older than the retention period (from config.md, default 7 days):

```bash
find "$XPOST_DATA/workspace" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} \;
```
