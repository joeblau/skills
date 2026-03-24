# TODOS

## TODO 1: Freshness gate
**What:** Pre-post check that verifies approved content isn't stale or contradicted by breaking news before posting.
**Why:** Research happens once per day but posting happens throughout the day. A take that was accurate at 9am could be wrong by 3pm.
**Pros:** Prevents posting stale/wrong content. Builds trust in the pipeline.
**Cons:** Adds an API call per post. Extra tokens for the check.
**Context:** Flagged by Codex during eng review. Matters more for V2 (auto-posting) than V1 (user reviews). When implementing, the Strategist could do a quick web search before each post to check if the take still holds.
**Depends on:** V2 auto-posting implementation.

## TODO 2: Variable output volume
**What:** Let the Content Strategist decide how many posts to produce based on research quality, instead of hardcoding "1 thread + 4 posts."
**Why:** Some days have less to say. Forcing 5 pieces of content when research is thin produces filler.
**Pros:** Higher content quality. More authentic presence.
**Cons:** Unpredictable output volume. Harder to schedule.
**Context:** Flagged by Codex. The Strategist already evaluates research quality — it should also decide whether to produce 2 posts or 5.
**Depends on:** Nothing — can be added to V1 as an enhancement.

## TODO 3: Auto-posting via browser automation
**What:** Community Manager agent + orchestrator handle posting via gstack `/browse`. Autonomous posting to X without user intervention.
**Why:** V1 requires user to review and post manually. V2 closes the loop with automated posting throughout the day.
**Pros:** Fully autonomous content pipeline. Set it and forget it.
**Cons:** Risk of posting bad content. Browser automation is fragile (session expiry, UI changes, CAPTCHA). Needs freshness gate (TODO 1) first.
**Context:** Deferred from V1 per Codex feedback — autonomous posting is the riskiest piece and should come after the generate+review loop is proven. Requires re-adding the Community Manager agent (deleted in V1), `/loop` integration, and posting schedule logic.
**Depends on:** V1 pipeline proven stable. TODO 1 (freshness gate) recommended first.
