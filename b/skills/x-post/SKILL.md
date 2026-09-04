---
name: x-post
description: |
  MANUAL TRIGGER ONLY: invoke only when user types /b:x-post.
  Research thought leaders on X, generate content (1 thread + 4 posts),
  and present for review. Uses a 3-agent pipeline with quality gate.
---

# x-post: Content Pipeline

Research what thought leaders are saying, generate informed X content, and present it for review.

## Setup

Requires `X_BEARER_TOKEN` environment variable for X API access. Falls back to WebSearch if not set.

## Pipeline

```
Phase 1: Research → Phase 2: Strategy → Phase 3: Creation + Quality Gate → Present
```

## Step 1: Initialize

```bash
# Skill assets (agents/, config.md) live with the skill; read-only.
XPOST_DIR="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/skills/x-post}"
[ -d "$XPOST_DIR" ] || XPOST_DIR="$(find ~/.claude/skills ~/.claude/plugins -name 'x-post' -type d 2>/dev/null | head -1)"

# Workspace is written to, so it must survive plugin updates: prefer CLAUDE_PLUGIN_DATA.
XPOST_DATA="${CLAUDE_PLUGIN_DATA:-$XPOST_DIR}"
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

## Step 3: Phase 1 — Research

Write to pipeline-state.md: `phase: research | status: started`

Dispatch the **Research Agent** via the Agent tool:

**Prompt construction:**
1. Read `x-post/agents/SHARED.md`
2. Read `x-post/agents/research-agent/AGENT.md`
3. Read `x-post/agents/research-agent/SOUL.md`
4. Combine all three into the agent prompt
5. Append: "Topic: [topic]. Write your output to: $WORKSPACE/research.md"

Use `subagent_type: general-purpose` so the agent has access to Bash (for curl) and WebSearch.

If the agent fails or times out, retry once. If it fails again, abort the pipeline and tell the user.

Write to pipeline-state.md: `phase: research | status: complete`

## Step 4: Phase 2 — Strategy

Write to pipeline-state.md: `phase: strategy | status: started`

Read `$WORKSPACE/research.md`.

Dispatch the **Content Strategist (strategy mode)** via the Agent tool:

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

Dispatch the **Content Creator** via the Agent tool:

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

Dispatch the **Content Strategist (review mode)** via the Agent tool:

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
find "$XPOST_DATA/workspace" -maxdepth 1 -type d -mtime +7 -exec rm -rf {} \;
```
