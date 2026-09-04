#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Build corpus-profile.json — the b:sludgify skill's memory of the house narrative.

    ./build_profile.py --corpus ~/Developer/bloxwap/sludge/sludgify \
                       --manifest /tmp/corpus/corpus.json \
                       --out ../corpus-profile.json

The profile has two halves and this script owns both:

  CURATED   the theme taxonomy, the house POV, the rhetorical shapes, the reject
            list, the scoring rubric and the editorial rules. These are editorial
            judgements read out of 76 clips by hand. They live as constants in
            THIS FILE — the JSON is a build artifact, this script is the source.

  MEASURED  durations, word counts, speech rate, speaker mix, slug grammar, term
            frequencies, absence probes and the scorer calibration. Recomputed
            from the corpus on every run.

Every run also VALIDATES the curated half against the corpus and writes the
result into `validation`: exemplar slugs that no longer exist, lexicon terms that
never occur, themes that lost their clips, reject-list probes that started firing.
That is how you find out the profile went stale as the corpus grows.

Inputs, in order of preference for transcript text:
  --manifest FILE   corpus.json: [{file, speaker, slug, dur, words, text}, ...]
                    `dur` is the SPEECH BODY (CTA tail already trimmed).
  --transcripts DIR sidecars named {stem}.json (whisper output, uses .text) or
                    {stem}.txt. Body duration is then ffprobe total - --cta-tail.

Modes:
  (default)   write the profile
  --check     regenerate in memory, diff against the profile on disk, exit 1 on drift
  --probe-cuts  additionally scene-detect the top pane of every mp4 (slow, ffmpeg)
"""

from __future__ import annotations

import argparse
import collections
import datetime as _dt
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = "1.0.0"
PROFILE_VERSION = "1.0.0"

# =============================================================================
# CURATED — theme taxonomy
# =============================================================================
# rank is by corpus mass (number of clips the scorer assigns as primary theme).
# `core` terms weigh 3, `support` terms weigh 1. Exemplars are slugs that must
# exist in the corpus; the build validates them.

THEMES: dict[str, dict] = {
    "apps_over_infra": {
        "rank": 1,
        "title": "Apps Over Infrastructure",
        "claim": (
            "The L1/infrastructure cycle is over. Value and the unexplored design space are in "
            "real apps with real revenue and real distribution — and almost nobody is building "
            "good ones."
        ),
        "core": [
            "l1", "l1s", "l2", "l2s", "chain", "chains", "block space", "blockspace", "scaling",
            "apps", "app", "product", "products", "build", "building", "builders", "revenues",
            "revenue", "distribution", "pmf", "product market fit", "tam",
            "name one good product", "ship",
        ],
        "support": [
            "faster and cheaper", "infrastructure", "protocol", "design space", "experimentation",
            "developers", "startup", "startups", "founders", "users", "customer", "customers",
            "interface", "token", "incentive",
        ],
        "exemplars": [
            "apps-over-l1s", "bullish-on-apps-in-crypto", "creating-apps", "name-one-good-product",
            "mogging", "design-around-the-product", "i-love-the-idea-of-tokens",
            "is-there-something-real", "shilling-of-the-world", "crypto-gaming",
        ],
        "quotable": [
            "you can actually build real apps with real revenues, real distribution immediately now",
            "not a single good fucking app is being built in this industry",
            "I don't think you're gonna see the L1 bullshit happen again",
        ],
        "discriminator": None,
    },
    "everything_tradable": {
        "rank": 2,
        "title": "Everything Becomes Tradable",
        "claim": (
            "The crypto/TradFi line is gone. Perps, prediction markets, equities, gold, 24/7 — "
            "trading is being added everywhere, and the venue with the liquidity wins because "
            "liquidity cannot be copy-pasted."
        ),
        "core": [
            "perps", "perp", "perpetuals", "open interest", "prediction markets", "tokenization",
            "stablecoins", "equities", "equity perps", "24/7", "24-7", "tradfi", "spot", "options",
            "zero day", "0dte", "leverage", "levered", "liquidity", "volume", "volumes",
        ],
        "support": [
            "stocks", "gold", "silver", "commodities", "assets", "markets", "market", "hyperliquid",
            "polymarket", "robinhood", "robin hood", "coinbase", "exchange", "venue", "order book",
            "funding", "basis", "shitcoins", "crypto only",
        ],
        "exemplars": [
            "not-crypto-vs-tradfi", "perps-are-innovative", "perps", "perps-v2", "perps-are-better",
            "crypto-only-we-just-trade", "hyperliquid-cant-be-vamped", "monkey-pictures-to-equities",
            "1000x-onchain-volumes", "most-stocks-are-shitcoins", "crypto-is-about-money",
        ],
        "quotable": [
            "no longer to be this crypto versus tradfi, but how finance in general evolves",
            "Hyperliquid is sticky... can't just be vamped",
            "most stocks are shitcoins over a long enough period of time",
        ],
        "discriminator": None,
    },
    "traders_as_protagonist": {
        "rank": 3,
        "title": "Traders Are the New Celebrities",
        "claim": (
            "Trading is now a public identity and a status ladder. The trader replaced the "
            "founder/technologist as the culture's protagonist — and unlike other elites, the "
            "trader can teach you how."
        ),
        "core": [
            "traders", "trader", "celebrities", "celebrity", "influencers", "influencer", "p&l",
            "pnl", "somebody", "nobody", "make a name", "name for myself", "discourse", "shit post",
            "shitpost", "clout", "rite of passage", "right of passage", "blow up",
        ],
        "support": [
            "famous", "rich", "status", "following", "audience", "teach", "teach you", "verify",
            "on chain", "onchain", "leaderboard", "copy trade", "social trading", "meta shift",
        ],
        "exemplars": [
            "traders-are-celebrities", "traders-are-the-new-celebrities",
            "social-apps-proliferate-100x", "nobody-to-somebody", "blow-up-as-a-trader",
            "just-good-at-this-game", "biggest-pnl-verify-on-chain", "top-bottom",
        ],
        "quotable": [
            "LeBron cannot tell you how to be LeBron. They can literally teach you how they made 40 million.",
            "on chain has solidified itself as a venue for like a nobody to become a somebody",
            "cycle's not over till Ansem is married to a Kardashian",
        ],
        "discriminator": None,
    },
    "risk_is_a_craft": {
        "rank": 4,
        "title": "Risk Is a Craft, Not a Vice",
        "claim": (
            "Leverage and concentration are legitimate when they are earned with work and stated "
            "risk. The shame is being casual, not being levered."
        ),
        "core": [
            "risk tolerance", "risk capital", "designated account", "concentrated", "conviction",
            "position size", "sizing", "dedicate", "dedicated", "dedication", "full-time job",
            "full time job", "do the work", "put the work in", "get crushed", "gambling",
            "objectives", "order book",
        ],
        "support": [
            "leverage", "levered", "discipline", "attentive", "engaged", "homework", "documents",
            "time frame", "stop loss", "probability", "analysis", "hours", "craft",
            "understand the risk", "blow up",
        ],
        "exemplars": [
            "dedicate-yourself", "wage-cucking-job", "if-youre-wrong-youre-crushed",
            "objective-risk-tolerance", "concentrated-levered-bets", "designated-account-risk",
            "fully-attentive", "low-conviction-smaller-size",
        ],
        "quotable": [
            "if you dedicate yourself to trading crypto, you could be very successful. But you have to dedicate yourself.",
            "understand the risk, understand your objectives",
            "I do excessive amounts of probability analysis over a hundred hours of deep research",
        ],
        "discriminator": (
            "The corpus is simultaneously pro-speculation and anti-gambling. The reconciling rule "
            "is WORK AND STATED RISK: speculation with research and a sized position is craft; a "
            "slot machine is not. No regex captures this — read the span. A clip can be "
            "pro-leverage and anti-sports-betting in the same breath and both are on-narrative."
        ),
    },
    "trading_as_daily_habit": {
        "rank": 5,
        "title": "Trading as a Daily Habit / The Missing Interface",
        "claim": (
            "Trading becomes an everyday consumer app behaviour — like scrolling — and the "
            "interface for it has not been built yet. There is still an opening against the "
            "incumbents."
        ),
        "core": [
            "interface", "interfaces", "ux", "scroll", "scrolling", "open the app", "muscle memory",
            "experience", "permissionless", "permissionlessness", "new platform", "robinhood",
            "robin hood", "interactive brokers", "coinbase", "onboarding", "retention",
        ],
        "support": [
            "mobile", "phone", "app", "apps", "social media", "tiktok", "instagram", "feed",
            "habit", "daily", "every day", "keeps them coming back", "young", "platform", "network",
        ],
        "exemplars": [
            "trading", "hypothesis-to-execution", "new-interface-young-winners", "new-players-vs-old",
            "more-fun-trading-interfaces", "great-experience",
        ],
        "quotable": [
            "the same way people open X and you're like, oh, I go scroll... you open pump fun",
            "a new competitor to Robinhood, Interactive Brokers, Coinbase",
            "eventually it becomes muscle memory",
        ],
        "discriminator": (
            "'Permissionless' is the shape this corpus performs but the word it demotes. sunnydece: "
            "'while permissionlessness and the 24/7 access is important... what keeps them coming "
            "back is that repeated experience.' Access is the theme; the jargon is not the point."
        ),
    },
    "psychology_of_the_hit": {
        "rank": 6,
        "title": "The Psychology of the Hit",
        "claim": (
            "Traders are not addicted to money, they are addicted to being right — and the number "
            "that would be enough never arrives."
        ),
        "core": [
            "addicted", "addiction", "emotional hit", "dopamine", "dopamine hit", "feel it",
            "feeling", "being right", "getting it right", "personal relationship",
            "dating your stock", "never be over", "enough", "ego",
        ],
        "support": [
            "emotional", "emotion", "psychology", "lie", "lies", "tell themselves", "chilling",
            "scale up", "greed", "fear", "excitement", "hit", "gut",
        ],
        "exemplars": [
            "addicted-to-emotional-hit", "might-never-be-over", "low-conviction-smaller-size",
            "great-traders-act-on-their-feelings", "not-dating-your-stock", "just-good-at-this-game",
        ],
        "quotable": [
            "one of the biggest lies that investors tell themselves is that they're addicted to making money... we're addicted to the emotional hit of getting it right",
            "You're not dating your stock. Stop it.",
            "it might never be over",
        ],
        "discriminator": None,
    },
    "ai_as_market_regime": {
        "rank": 7,
        "title": "AI Is a Market Regime, Not a Product",
        "claim": (
            "AI matters here only because it makes everything unpredictable, reprices mega-caps "
            "like shitcoins, and commoditizes intelligence. Never as technology to admire."
        ),
        "core": [
            "ai trade", "ai stocks", "nobody can predict", "repriced", "commoditized",
            "intelligence becomes", "human in the loop", "automation", "flood cycle",
        ],
        "support": [
            "ai", "semis", "semi", "chips", "memory chips", "nvidia", "amazon", "apple", "marvel",
            "sandisk", "ibm", "deltas", "volatility", "unknown", "moats", "barriers to entry",
        ],
        "exemplars": [
            "nobody-can-predict", "attention-trade", "creatives-become-valuable", "semi-explosion",
            "liquid-capital-markets", "premium-for-human-in-the-loop",
        ],
        "quotable": [
            "Amazon and Apple are getting repriced 15% on a date. These things are trading like shitcoins.",
            "what skill sets become valuable when intelligence becomes commoditized",
            "we're going to pay a premium for humans in the loop",
        ],
        "discriminator": (
            "Fires ONLY when AI is the CAUSE OF A MARKET CONDITION. If AI is the subject — models, "
            "benchmarks, agents, training runs — the `ai_tech_talk` veto fires instead. This is the "
            "noisiest boundary in the rubric and AI-heavy podcasts are exactly the videos most "
            "likely to be fed to sludgify. Read the span, do not trust the score alone."
        ),
    },
    "volatility_is_the_product": {
        "rank": 8,
        "title": "Volatility Is the Product",
        "claim": (
            "People show up for volatility, excitement and speculation. Dampening it kills the "
            "market. The winning product is whatever is most exciting."
        ),
        "core": [
            "volatility", "dampen", "dampening", "excitement", "exciting", "speculation",
            "speculative", "retail", "gamified", "gamify", "roller coaster", "short duration",
            "meme", "memes", "craziest",
        ],
        "support": [
            "institutions", "institutional", "fun", "culture", "attention", "hype", "adrenaline",
            "action", "degens", "degen", "casino", "jump on it", "showed up",
        ],
        "exemplars": [
            "we-love-volatility", "what-the-people-want", "culture-of-the-market",
            "design-around-the-product", "speculation", "single-accounts-manipulate",
        ],
        "quotable": [
            "we thought we wanted volatility dampening — actually we hate volatility dampening. We loved the volatility because that's why retail showed up.",
            "trade the craziest products... with as much leverage as you can. And that's what the people want.",
        ],
        "discriminator": None,
    },
    "retail_can_win": {
        "rank": 9,
        "title": "Retail Can Actually Win",
        "claim": (
            "Ordinary people can and do beat the market. S&P-and-chill is passivity. Take your "
            "outcome into your own hands."
        ),
        "core": [
            "retail", "regular people", "s&p", "s p", "index", "beat the market", "outperform",
            "active investing", "own hands", "accessible", "my mom", "get in the game",
            "anybody can", "anyone can", "ordinary people",
        ],
        "support": [
            "everyone", "normal", "people out there", "returns", "compound", "portfolio", "account",
            "start with", "small", "approachable", "intuitive", "engaged",
        ],
        "exemplars": [
            "ton-of-people-beat-market", "active-investing-own-outcome", "if-investing-youre-winning",
            "my-mom-could-understand", "money-in-capital-markets", "everyone-has-time-connect-dots",
        ],
        "quotable": [
            "There are a ton of people that consistently beat the market... just regular people",
            "people do not feel comfortable with the role of their outcome on by the S&P and chill",
            "my mom could understand what was happening",
        ],
        "discriminator": (
            "Held against its own opposite: the corpus also says retail is extremely bad at calling "
            "the top. Reconciled by AGENCY — retail-as-individual-who-does-work wins; "
            "retail-as-herd is wrong at extremes. Do not resolve this tension away."
        ),
    },
    "noise_vs_ground_truth": {
        "rank": 10,
        "title": "Noise vs Ground Truth",
        "claim": (
            "The crowd trades noise. The edge is independent observation of the real world — and "
            "it is not mysterious; anyone paying attention can do it."
        ),
        "core": [
            "noise", "ground truth", "independent thinker", "independent thinking", "alpha",
            "connecting dots", "connect the dots", "black box", "mysterious", "observing",
            "observation", "research", "conviction",
        ],
        "support": [
            "consensus", "crowd", "everybody", "perception", "narrative", "truthful", "information",
            "assess", "figure out", "edge", "common sense", "makes sense", "first principles",
        ],
        "exemplars": [
            "ground-truth-you-win", "independent-thinker", "not-mysterious-no-black-box",
            "everyone-has-time-connect-dots", "attention-trade",
        ],
        "quotable": [
            "the easiest trade right now is just not to pay attention to the noise and just actually try to assess the ground truth in something, anything",
            "I don't have some black box mysterious trading system. It just freaking makes sense.",
            "everyone has time to start observing the world and connecting dots",
        ],
        "discriminator": None,
    },
    "unloved_moment": {
        "rank": 11,
        "title": "Build in the Unloved Moment",
        "claim": (
            "Negative sentiment is exactly when the best opportunities appear and the most research "
            "should happen. The crowd arrives after."
        ),
        "core": [
            "negative perception", "best opportunities", "most research", "before it's popular",
            "working on for years", "counter narrative", "commoditized", "juncture", "correction",
            "momentum", "fundamentals", "zoom out",
        ],
        "support": [
            "cycle", "early", "unloved", "hated", "bear", "sentiment", "emerging", "see it coming",
            "get popular", "surprised", "overexposed", "exposed",
        ],
        "exemplars": [
            "best-opportunities-most-research", "gonna-be-brutal", "creatives-become-valuable",
            "premium-for-human-in-the-loop", "fortunate",
        ],
        "quotable": [
            "this current juncture in crypto where a lot of people have a negative perception of crypto is usually when the best opportunities arise and we should be doing the most research",
            "the counter narrative to automation",
        ],
        "discriminator": None,
    },
    "raw_pnl_over_ideology": {
        "rank": 12,
        "title": "Raw P&L Over Ideology",
        "claim": (
            "Results are the only credential. Decentralization talk, crypto ideals and technical "
            "purity are noise; verified money and solved pain are the scoreboard."
        ),
        "core": [
            "raw p&l", "raw pnl", "cut the noise", "decentralization", "ideals", "maxi", "maxis",
            "solving a pain", "solve a pain", "value proposition", "price go up",
            "not here to discuss", "who makes real money",
        ],
        "support": [
            "real", "actually", "verify", "results", "scoreboard", "proof", "legit", "bullshit",
            "theater", "purity", "ideology", "principles", "matter", "care about",
        ],
        "exemplars": [
            "i-just-want-raw-pnl", "users-dont-care-about-decentralization",
            "biggest-pnl-verify-on-chain", "is-there-something-real", "crypto-is-about-money",
            "i-love-the-idea-of-tokens",
        ],
        "quotable": [
            "Cut the noise... I'm not here to discuss decentralization. I'm not here to discuss crypto ideals. I just want raw P&L.",
            "who makes real money, who has the biggest P&Ls and you could verify it on chain",
            "Users don't care about decentralization.",
        ],
        "discriminator": (
            "Ranks last by PRIMARY-clip count but is the corpus's ideological SPINE. Its vocabulary "
            "is spread thin, so it works best as a SECONDARY theme — the 0.55 second-theme bonus is "
            "what surfaces it. A span whose secondary theme is raw_pnl_over_ideology is more "
            "on-narrative than its raw score suggests."
        ),
    },
}

# =============================================================================
# CURATED — house POV
# =============================================================================

HOUSE_POV = {
    "believes": [
        {"claim": "Trading is the main event of the culture, not a sideshow.",
         "note": "Finance is entertainment, identity and status simultaneously."},
        {"claim": "The scoreboard is verifiable P&L — not followers, takes or credentials.",
         "note": "'who makes real money, who has the biggest P&Ls and you could verify it on chain'"},
        {"claim": "Crypto was always about money.",
         "note": "'Bitcoin was about money. Ethereum was about contracts. And DeFi, it's got finance in the name.'"},
        {"claim": "Apps and products win; chains are commodity.",
         "note": "'It's not just enough to be faster and cheaper.'"},
        {"claim": "Volatility is the product, not a defect.",
         "note": "Institutions dampening it is a loss, not a maturation."},
        {"claim": "Retail belongs here and can win — with work.",
         "note": "Passive indexing is treated as a surrender of agency."},
        {"claim": "Leverage and concentration are honorable when earned.",
         "note": "Sizing, homework and stated risk are the license."},
        {"claim": "The barrier is attention and dedication, not credentials.",
         "note": "No degrees, no desks, no gatekeepers appear anywhere in 76 clips."},
        {"claim": "Everything converges into one 24/7 tradable surface.",
         "note": "The crypto/TradFi distinction is treated as already dead."},
        {"claim": "Ordinary observation beats institutional research.",
         "note": "'That's just living life.'"},
        {"claim": "The interface hasn't been built yet — incumbents are beatable.",
         "note": "Robinhood / Interactive Brokers / Coinbase named as targets, not models."},
        {"claim": "Honest confession of motive is welcome.",
         "note": "Admitting the addiction, the luck, the moving goalposts is IN character."},
    ],
    "dismisses": [
        {"claim": "Decentralization ideology, crypto ideals, maxis.", "note": "Named and rejected outright."},
        {"claim": "Infrastructure discourse.", "note": "Block space debates, L2 scaling, 'faster and cheaper', 'the L1 bullshit'."},
        {"claim": "Institutions as a truth-source.", "note": "They appear almost only as the thing that dampened volatility."},
        {"claim": "Passive indexing as an identity.", "note": "'S&P and chill.'"},
        {"claim": "Token incentives as a substitute for product.",
         "note": "'if you're giving away money for free, a lot of people use your product — that says nothing.'"},
        {"claim": "Influencer clout without P&L.", "note": "'not the OG crypto influencer crowd that's funny or that shit post.'"},
        {"claim": "Bad crypto products, named.", "note": "'moonshot is a trash fucking app... name one good product, bruh.'"},
        {"claim": "Emotional attachment to positions.", "note": "'You're not dating your stock. Stop it.'"},
        {"claim": "Pure gambling outside markets.", "note": "Sports betting is 'the worst imaginable thing for humans.'"},
        {"claim": "Mystification of trading.", "note": "The black box, the guru system."},
        {"claim": "Doomerism.", "note": "'crypto's going to zero' appears only as the thing being contradicted."},
    ],
    "held_tensions": [
        {"tension": "Anti-gambling vs pro-speculation",
         "resolution": "Work. Speculation with research and stated risk is craft; a slot machine is not.",
         "warning": "A lexicon that treats 'gambling' as purely negative rejects legitimate risk_is_a_craft clips; one that treats it as positive accepts sports-betting content. No regex captures this — the agent must read."},
        {"tension": "'Retail can win' vs 'retail is extremely bad at calling the top'",
         "resolution": "Agency. Retail-as-individual-who-does-work wins; retail-as-herd is wrong at extremes.",
         "warning": "Both sides are on-narrative. Do not reject one for contradicting the other."},
        {"tension": "Trading as casual daily habit vs trading as full-time dedication",
         "resolution": "Both are endorsed. What is never endorsed is CASUAL LEVERAGE.",
         "warning": "The contradiction is the corpus's actual position, not an inconsistency to fix."},
    ],
}

# =============================================================================
# CURATED — rhetorical shapes
# =============================================================================

RHETORICAL_SHAPES = [
    {"id": "contrarian_reversal", "name": "Contrarian reversal", "n": 16,
     "signature": "'we thought we wanted X — actually we hate X'; 'the biggest lie X tell themselves'",
     "exemplars": ["we-love-volatility", "addicted-to-emotional-hit", "most-stocks-are-shitcoins"]},
    {"id": "second_person_instruction", "name": "Second-person instruction", "n": 14,
     "signature": "'you have to', 'if you don't', imperative opener",
     "exemplars": ["ground-truth-you-win", "dedicate-yourself", "not-dating-your-stock"]},
    {"id": "prediction", "name": "Prediction", "n": 14,
     "signature": "'is gonna', 'the future is', 'in ten years'",
     "exemplars": ["perps", "trading", "more-fun-trading-interfaces"]},
    {"id": "named_contrast", "name": "Named contrast", "n": 10,
     "signature": "X vs Y with real names / entities",
     "exemplars": ["hyperliquid-cant-be-vamped", "new-players-vs-old", "biggest-pnl-verify-on-chain"]},
    {"id": "insider_mechanism", "name": "Insider mechanism", "n": 9,
     "signature": "'the reason is', 'they have the...', a causal chain",
     "exemplars": ["perps-v2", "dedicate-yourself", "single-accounts-manipulate"]},
    {"id": "before_after_progression", "name": "Before/after progression", "n": 7,
     "signature": "'started with... then... now'; 'for the first time ever'",
     "exemplars": ["monkey-pictures-to-equities", "social-apps-proliferate-100x"]},
    {"id": "confession", "name": "Confession", "n": 6,
     "signature": "admits motive, luck, or that it never ends",
     "exemplars": ["might-never-be-over", "fortunate", "addicted-to-emotional-hit"]},
    {"id": "rant_indictment", "name": "Rant / indictment", "n": 6,
     "signature": "profanity, 'name one', 'stop it'",
     "exemplars": ["name-one-good-product", "mogging", "worst-imaginable-thing"]},
    {"id": "permissioned_to_permissionless", "name": "Permissioned → permissionless", "n": 5,
     "signature": "an access / status ladder, but NOT the jargon",
     "exemplars": ["nobody-to-somebody", "my-mom-could-understand"]},
]

DOMINANT_COMBINATION = (
    "Reversal + second-person instruction. The canonical sludge states the consensus, flips it, and "
    "points at YOU. Clips carry 1-3 shapes; score compound shapes higher than single ones. 56/76 "
    "(74%) contain second-person address at 3.83 'you' per 100 words; 45/76 carry negation."
)

# =============================================================================
# CURATED — reject list (the conspicuous absences)
# =============================================================================
# corpus_hits is the measured occurrence count across all 4,656 corpus words at
# build time. The build re-measures; if any of these starts firing the corpus has
# changed shape and the veto needs revisiting.

REJECT_LIST = [
    {"id": "macro_fed", "label": "Macro / Fed / rates / inflation / CPI / debasement / yields",
     "severity": "soft", "corpus_hits_at_build": 0,
     "note": "Zero regex hits. The nearest thing in 4,656 words is 'half a trillion dollar deltas' — a size, not a macro thesis, and no pattern fires on it. Hayes appears 4x and NEVER for his liquidity-macro thesis. That is the clearest proof this is a deliberate editorial filter, not a topic sample.",
     "pattern": r"\b(fed|federal reserve|powell|interest rates?|rate cuts?|inflation|cpi|jobs report|quantitative easing|debasement|bond yields?|treasur(?:y|ies))\b"},
    {"id": "regulation_politics", "label": "Regulation / SEC / lawsuits / politics / policy / compliance / KYC",
     "severity": "soft", "corpus_hits_at_build": 0, "note": "Zero hits in 4,656 words.",
     "pattern": r"\b(sec|cftc|regulator|regulation|regulatory|lawsuit|subpoena|congress|senate|the bill|kyc|aml|compliance|election|president|administration)\b"},
    {"id": "price_target", "label": "Price targets, cycle tops, ATHs, 'to the moon', '$X by Y'",
     "severity": "soft", "corpus_hits_at_build": 0,
     "note": "This corpus predicts BEHAVIOUR and STRUCTURE, never a number on a date.",
     "pattern": r"\b(price target|cycle top|all[- ]time high|\bath\b|bottom is in|to the moon|going to \$\s?\d)"},
    {"id": "technical_analysis", "label": "Technical analysis of any kind",
     "severity": "soft", "corpus_hits_at_build": 0, "note": "Zero hits.",
     "pattern": r"\b(support level|resistance|moving average|\brsi\b|macd|fibonacci|head and shoulders|trend ?line|candlestick|chart pattern|golden cross)\b"},
    {"id": "tokenomics", "label": "Tokenomics: FDV, unlocks, vesting, emissions, cap tables, raise valuations",
     "severity": "soft", "corpus_hits_at_build": 0,
     "note": "Tokens appear as PRODUCT ('I love the idea of tokens'), never as a cap table.",
     "pattern": r"\b(tokenomics|token unlock|vesting|\bfdv\b|fully diluted|emissions schedule|airdrop farm|cap table|series [ab]|raised at)\b"},
    {"id": "scam_drama", "label": "Scams, rugs, hacks, exploits, fraud, court cases, personality feuds",
     "severity": "soft", "corpus_hits_at_build": 0, "note": "Zero hits. This corpus never does drama.",
     "pattern": r"\b(scam|rug ?pull|rugged|exploit|hacked|hack|fraud|ponzi|\bsbf\b|\bftx\b|indicted|jail|sued|drama|beef)\b"},
    {"id": "hard_money", "label": "Bitcoin maximalism / sound money / store of value / halving",
     "severity": "soft", "corpus_hits_at_build": 0,
     "note": "Zero regex hits. Bitcoin appears once in the whole corpus, as one clause of a history ('Bitcoin was about money') — never as an ideology, so no hard-money pattern fires.",
     "pattern": r"\b(sound money|fix the money|digital gold|store of value|the halving|somebody'?s liability|fiat debasement)\b"},
    {"id": "non_finance_chain", "label": "Non-finance blockchain: ZK, DePIN, sharding, validators, gas, consensus",
     "severity": "soft", "corpus_hits_at_build": 0, "note": "Zero hits.",
     "pattern": r"\b(supply chain|self[- ]sovereign|zero ?knowledge|\bzk\b|depin|sharding|validator set|consensus mechanism|node operator|gas fees?)\b"},
    {"id": "ai_tech_talk", "label": "AI-as-technology: models, benchmarks, context windows, agents, training runs",
     "severity": "soft", "corpus_hits_at_build": 0,
     "note": "Shares surface vocabulary with the ai_as_market_regime THEME. Discriminator: is AI the cause of a market condition (theme) or the subject itself (veto)?",
     "pattern": r"\b(agi|benchmark|\bgpt-?\d|\bllm\b|model weights|training run|transformer|context window|prompt engineering|open ?source model)\b"},
    {"id": "lifestyle_flex", "label": "Lifestyle flex: lambos, net worth, houses, 'I made $X'",
     "severity": "soft", "corpus_hits_at_build": 0,
     "note": "Money appears as SCOREBOARD ('$100,000', 'made 40 million' as proof of teachability), never as consumption.",
     "pattern": r"\b(lambo|mansion|yacht|private jet|my net worth|i made \$\d)\b"},
    {"id": "institutional_desk", "label": "Institutional desk mechanics: AUM, LP/GP, mandates, allocations, diligence",
     "severity": "soft", "corpus_hits_at_build": 0, "note": "Zero hits.",
     "pattern": r"\b(asset allocation|mandate|limited partners?|\blps?\b|family office|pension fund|endowment|\baum\b|fund manager|due diligence)\b"},
    {"id": "housekeeping", "label": "Podcast housekeeping: welcomes, sponsors, subscribe, 'this episode'",
     "severity": "hard", "corpus_hits_at_build": 0, "note": "Zero hits. 0/76 clips open on any greeting.",
     "pattern": r"\b(welcome back|thanks for having me|subscribe|sponsored by|this episode|before we (?:get )?(?:start|into)|shout ?out to our)\b"},
    {"id": "interviewer", "label": "Host questions and interview scaffolding",
     "severity": "hard", "corpus_hits_at_build": 0,
     "note": "One speaker holds the floor for the whole clip in all 76. Any span containing a host turn is disqualified regardless of score.",
     "pattern": r"\b(what do you think about|how do you see|let me ask you|can you talk about|tell us about|my question is|walk me through)\b"},
    {"id": "recitation", "label": "On-topic recitation: a timeline with correct vocabulary and zero claim",
     "severity": "hard", "corpus_hits_at_build": 0,
     "note": "One of the two hardest negatives to catch — correct vocabulary, no argument ('we launched perps in March, then spot in June, then the app...'). Needs the recitation veto AND the no_claim gate.",
     "pattern": r"\band then (?:we|i|they)\b[^.?!]{0,120}\band then (?:we|i|they)\b"},
    {"id": "doom_capitulation", "label": "Doom / capitulation / 'it's over' as a sincere stance",
     "severity": "judgement", "corpus_hits_at_build": 0,
     "note": "The corpus's one doom mention quotes it as the WRONG crowd's view. Not regex-catchable — the same words carry both stances. Read the span: is the speaker holding the doom or mocking it?",
     "pattern": None},
    {"id": "biographical_anecdote", "label": "Biographical anecdote / where-I-grew-up / origin story",
     "severity": "judgement", "corpus_hits_at_build": 0,
     "note": "Caught by the off_topic gate rather than a pattern — an anecdote has no core terms. Listed so the agent recognises it while reading.",
     "pattern": None},
    {"id": "on_brand_rhetoric_off_brand_topic", "label": "On-brand rhetoric, off-brand topic (generic startup/hiring advice)",
     "severity": "judgement", "corpus_hits_at_build": 0,
     "note": "The second of the two hardest negatives. Heavy second-person and imperatives, zero core terms. Caught by the off_topic gate's distinct-core-term floor, not by any veto.",
     "pattern": None},
]

# =============================================================================
# CURATED — scoring rubric
# =============================================================================

STANCE_PATTERNS = {
    "reversal": [
        r"\b(?:but|and) (?:actually|really)\b",
        r"\bactually\b(?=[^.?!]{0,40}\b(?:hate|love|don't|isn't|is not|the opposite)\b)",
        r"\bthat'?s not (?:how|true|it|the)\b",
        r"\b(?:biggest|greatest) (?:lie|myth|misconception)\b",
        r"\b(?:everyone|everybody|people|most people|they) (?:think|thinks|say|says|believe|believes)\b[^.?!]{0,60}\b(?:but|actually|no|wrong)\b",
        r"\bi don'?t (?:think|believe)\b",
        r"\bit'?s not (?:just )?(?:enough|about|that)\b",
        r"\bwe thought we wanted\b",
        r"\bturns out\b",
        r"\bcounter ?narrative\b",
        r"\bcontrary to\b",
        r"\bmost people (?:don'?t|are wrong|get this wrong)\b",
        r"\bnot a single\b",
        r"\bnobody\b",
        r"\bwhat if\b",
    ],
    "named_contrast": [
        r"\b\w+ (?:vs\.?|versus) \w+\b",
        r"\bnot (?:the )?[a-z]+,? (?:it'?s|but) \b",
        r"\bwhy isn'?t \w+",
        r"\brather than\b",
        r"\binstead of\b",
        r"\b(?:unlike|compared to)\b",
        r"\bis no longer\b",
        r"\bused to be\b[^.?!]{0,60}\bnow\b",
    ],
    "imperative": [
        r"^(?:stop|start|go|look|listen|understand|forget|do|don'?t|just|think about|name one|cut)\b",
        r"\byou (?:have to|need to|should|gotta|got to|must)\b",
        r"\bif you (?:want|don'?t|can|are|'re)\b",
        r"\bstop (?:it|having|doing)\b",
        r"\bunderstand (?:the|your)\b",
    ],
    "dismissal": [
        r"\b(?:bullshit|trash|garbage|crap|nonsense|stupid|dumb|joke|meaningless|worthless)\b",
        r"\b(?:i'?m )?not here to\b",
        r"\bdon'?t care about\b",
        r"\bwho cares\b",
        r"\bdrives me nuts\b",
        r"\bi hate\b",
        r"\bworst\b",
        r"\bdoesn'?t matter\b",
        r"\bnot that great\b",
        r"\bdid not deserve\b",
    ],
    "confession": [
        r"\bi(?:'| a)m (?:just )?(?:addicted|not|actually|honestly|gonna be honest)\b",
        r"\bthe (?:biggest )?lie(?:s)? (?:that )?\w+ tell\b",
        r"\bwe were (?:super )?fortunate\b",
        r"\bi (?:used to|stopped|realized|realize)\b",
        r"\bto be honest\b",
        r"\bi'?ll admit\b",
        r"\bmy most confident view\b",
        r"\bit might never\b",
    ],
    "prediction": [
        r"\b(?:is|are|'s|'re) (?:gonna|going to) (?:be|get|happen|see|change|proliferate)\b",
        r"\bin (?:\d+|five|ten|three) years\b",
        r"\bthe future is\b",
        r"\bwill (?:be|get|change|come|continue)\b",
        r"\bi think (?:we'?re|there'?s|it'?s) (?:gonna|going to)\b",
        r"\bnext cycle\b",
        r"\blong term\b",
    ],
    "progression": [
        r"\bstarted with\b[^.?!]{0,80}\b(?:then|now)\b",
        r"\bwe graduated\b",
        r"\bused to\b[^.?!]{0,60}\bnow\b",
        r"\b(?:pre|post)[- ]?(?:circa )?20\d\d\b",
        r"\bfor the first time (?:ever)?\b",
        r"\ball of a sudden\b",
    ],
    "mechanism": [
        r"\bthe reason (?:that|why|is)\b",
        r"\bbecause (?:they|you|it|there|all)\b",
        r"\bwhat happens (?:is|generally|when)\b",
        r"\bhere'?s (?:how|why|what)\b",
        r"\bthey have the\b",
        r"\bit'?s happening because\b",
        r"\bwhich means\b",
    ],
}

ENTITIES = [
    "hyperliquid", "polymarket", "robinhood", "coinbase", "solana", "ethereum", "eth", "bitcoin",
    "pump fun", "pumpfun", "phoenix", "moonshot", "binance", "s&p", "nasdaq", "amazon", "aws",
    "apple", "nvidia", "marvel", "sandisk", "ibm", "telegram", "tiktok", "instagram", "twitter",
    "x", "kalshi", "interactive brokers", "yc", "lebron", "kardashian", "morgan stanley", "defi",
    "uniswap",
]

NUM_RE = r"(?:\$\s?\d[\d,\.]*|\b\d[\d,\.]*\s?(?:x|%|k|bn|billion|million|trillion|dollars?)\b|\b(?:19|20)\d\d\b|\b\d+\s?(?:day|hour|year|month)s?\b)"
FILLER_RE = r"\b(um|uh|you know|i mean|like|kind of|sort of|right\?|basically|literally)\b"
YOU_RE = r"\b(you|you're|your|yourself|you've|you'll|yall|y'all)\b"
CLAIM_EVIDENCE_RES = [
    r"\b(not|never|nobody|none|nothing|no one|n't)\b",
    r"\b(biggest|best|worst|most|only|easiest|hardest|greatest|number one|first time)\b",
    r"\b(everyone|everybody|nobody|all of|every|a ton of|a lot of people|most people|people are|people want|users|always|the core of|has been)\b",
    r"\b(gonna|going to|will|would|could|has to|have to|needs? to)\b",
]
# sludge's own auto-emphasis trigger: $amount, N%, Nx
AUTO_EMPHASIS_RE = r"(?:\$\s?\d|\b\d[\d,\.]*\s?%|\b\d[\d,\.]*\s?x\b|\b(?:hundred|thousand|100)\s?x\b)"

WEIGHTS = {
    "theme_fit": {"max": 30, "core_weight": 3.0, "support_weight": 1.0, "per_theme_cap": 18.0,
                  "per_100_words_scale": 1.6, "second_theme_bonus": 0.55,
                  "formula": "raw = 3*core_hits + 1*support_hits; theme_pts = min(raw*(100/W)*1.6, 18); theme_fit = min(best + 0.55*second_best, 30)",
                  "why": "The best corpus clips braid TWO themes (apps-over-l1s = apps_over_infra + everything_tradable; nobody-to-somebody = traders_as_protagonist + retail_can_win). The second-theme bonus is deliberate, not slack.",
                  "tie_break": "Equal theme scores resolve to the LOWER `rank` (more corpus mass wins). Ties are common on short spans; without an explicit rule the primary_theme label would depend on dict ordering.",
                  "primary_theme_caveat": "theme_fit (the score) is order-independent — on a tie, best and second are the same number either way. Only the primary_theme LABEL moves. Use the label for diversity capping and register selection, never as a claim about what a clip is about."},
    "stance": {"max": 20,
               "formula": "min(6*min(reversal,2) + 3*min(named_contrast,2) + 3*min(dismissal,2) + 4*min(confession,1), 20)"},
    "address": {"max": 12,
                "formula": "min(you_count*(100/W)*1.6, 8) + min(2*imperative_hits, 4)",
                "why": "Corpus baseline is 3.83 'you' per 100 words; 74% of clips have some."},
    "concreteness": {"max": 12,
                     "formula": "min(2*number_hits, 6) + min(2*entity_hits, 6)",
                     "why": "Both halves capped so a stat-dump cannot win on numbers alone."},
    "claim_shape": {"max": 12,
                    "formula": "+3 prediction, +3 progression, +3 mechanism, +1.5 declarative copular clause, +1.5 does not end mid-clause; cap 12"},
    "delivery": {"max": 8,
                 "formula": "start 8; -4 if wps outside [2.0,4.4]; -1.5 if outside [2.4,4.1]; -3 if filler>12/100w; -1.5 if filler>8/100w",
                 "why": "Doubles as the whisper-priming sanity check. A span reporting <2.0 wps against visible speech means the transcript got SWALLOWED — re-run with --no-prime before believing the score."},
    "duration_fit": {"max": 6, "measured_on": "speech body, NOT finished length",
                     "bands": [{"range_s": [15.0, 25.0], "pts": 6.0},
                               {"range_s": [12.0, 15.0], "pts": 4.0},
                               {"range_s": [25.0, 29.0], "pts": 4.0},
                               {"range_s": [9.0, 12.0], "pts": 2.0},
                               {"range_s": [29.0, 34.0], "pts": 2.0}],
                     "else_pts": 0.0},
}

GATES = {
    "off_topic": {"rule": "distinct_core >= 2 OR (distinct_core >= 1 AND distinct_support >= 3)",
                  "scope": "union of all 12 theme lexicons",
                  "multiplier": 0.60,
                  "catches": "on-brand rhetoric, off-brand topic — generic startup/hiring advice with heavy second-person"},
    "no_claim": {"rule": "claim_evidence > 0",
                 "counts": "negations + superlatives + quantifiers + modals + every stance and shape regex hit",
                 "multiplier": 0.70,
                 "catches": "on-topic recitation — correct vocabulary, no argument"},
}

VETO_MULTIPLIERS = {"soft": 0.62, "hard": 0.35}

THRESHOLDS = {
    "reject": {"below": 30, "action": "do not clip"},
    "shortlist": {"from": 30, "to": 38, "action": "consider only if the frame-check loves it"},
    "strong": {"from": 38, "to": 45, "action": "normal candidate — corpus p25 to median"},
    "flagship": {"from": 45, "action": "corpus upper half — lead with these"},
}

# =============================================================================
# CURATED — editorial / structural rules
# =============================================================================

ONE_PARAGRAPH = (
    "A good clip is 18-24 seconds and about 60 words of one person making one argument to the "
    "viewer. It opens on the claim inside the first sentence with at most two words of lead-in — "
    "mid-sentence entry is fine, preamble never is. It needs no context: no unresolved pronouns, no "
    "names the audience doesn't already know, no reference to the room. It lands on its own thesis "
    "in the last three words, on a complete clause, and the CTA cuts in immediately. One EDL span "
    "if the material is contiguous, never more than three; sludge's silence removal supplies the "
    "3-6 visible cuts on its own. One face for the whole clip, frame-checked at every cut."
)

EDITORIAL = {
    "duration": {
        "measured_on": "SPEECH BODY. corpus.json `dur` and every number here exclude the CTA tail.",
        "body_target_s": [18.0, 24.0],
        "body_ok_s": [15.0, 25.0],
        "body_hard_s": [12.0, 30.0],
        "words_target": [46, 76],
        "words_floor": 40,
        "words_absolute_floor": 19,
        "wps_ok": [2.0, 4.3],
        "wps_sanity": [2.0, 4.5],
        "sludge_target_duration_flag": 20,
        "rules": [
            "Target a BODY of 18-24s. Hard floor 12s, hard ceiling 30s — only 2/76 exceed 30s while 24% are under 15s, so short is safe and long is not.",
            "If the good material only adds to 13s, ship 13s. Never pad.",
            "Target ~60 spoken words. Reject a span under 40 words unless it is a genuine one-liner.",
            "A plan reporting <2.0 or >4.5 words/sec is a WHISPER FAILURE, not a slow or fast talker. Re-run with --no-prime before believing it.",
            "Pass --target-duration 20 to --plan. sludge's default of 25 plans against total; 20s of body is the corpus centre.",
            "TRAP: the finished-mp4 durations (median 26.4s) are body + a 6.4s CTA tail. Wiring --target-duration off the finished median ships ~32s clips, above the corpus p75.",
        ],
    },
    "opening": {
        "hook_in_first_sentence_rate": 0.89,
        "lead_in_words_max": 4,
        "lead_in_words_typical": 1,
        "setup_sentences_max": 1,
        "mid_clause_entry_ok": True,
        "mid_clause_entry_rate": 0.21,
        "lead_in_tokens_to_trim": ["so", "and", "like", "but", "um", "uh", "well", "okay", "right",
                                   "yeah", "just", "actually", "basically", "obviously", "again",
                                   "you know", "i mean"],
        "permitted_hooks": [
            {"kind": "flat_claim", "n": 59},
            {"kind": "imperative_to_viewer", "n": 4},
            {"kind": "question_the_speaker_answers_himself", "n": 3},
            {"kind": "hard_stat", "n": 2},
        ],
        "preferred_grammars": [
            {"kind": "named_subject_declarative", "example": "Users don't care about decentralization"},
            {"kind": "contrarian_negation", "example": "I don't think you're gonna see the L1 bullshit happen again"},
            {"kind": "superlative_or_first_ever", "example": "This is the biggest moment in the history of capital markets"},
        ],
        "hedged_opening_rate": 0.105,
        "auto_reject": [
            "a greeting", "a self-introduction", "'um' / 'uh'", "'that's a great question'",
            "the host's voice or an audible off-camera question",
            "any sentence whose subject is the conversation ('so what we were talking about earlier')",
            "a hedge stack ('I think maybe it's sort of')",
        ],
        "rules": [
            "The hook is in the FIRST SENTENCE — 89% of the corpus does this.",
            "LEAD-IN BUDGET: at most 4 words, preferably 1, before the first content word. Median is 1 word (~0.3s); 96% are <=2; nothing exceeds 4.",
            "AT MOST ONE setup sentence, and only if it is a concrete scenario. Two setup sentences = reject. Only 11% of the corpus uses even one.",
            "STARTING MID-CLAUSE IS CORRECT PRACTICE, not a defect. 21% of the corpus does it. 'is that, like, the core of crypto has always been about money and finance' is a shipped opening. The test is not grammaticality; it is whether the first three words carry content. DO NOT build an opener-cleanliness filter.",
            "If the best claim answers a host's question, keep it only if the SPEAKER restates the question himself. Otherwise start after the restatement, on the claim.",
        ],
    },
    "landing": {
        "thesis_in_final_quarter_rate": 0.64,
        "thesis_in_last_three_words_rate": 0.41,
        "thesis_is_final_word_rate": 0.25,
        "lands_on_payoff_rate": 0.61,
        "snap_close_rate": 0.26,
        "snap_close_max_words": 9,
        "genuine_mid_word_stop_rate": [0.04, 0.07],
        "close_shapes": [
            {"kind": "thesis_restatement", "n": 17}, {"kind": "punchline_or_joke", "n": 9},
            {"kind": "snap_summary", "n": 5}, {"kind": "reason_payoff", "n": 4},
            {"kind": "bookend", "n": 4}, {"kind": "reversal", "n": 2},
            {"kind": "payoff", "n": 2}, {"kind": "contrast_payoff", "n": 1},
            {"kind": "escalation", "n": 1},
        ],
        "never_close_on": ["a summary of the summary", "'so yeah'", "a hand-off to a host",
                           "a hedge", "dead air", "a rhetorical question (only 2/76)"],
        "rules": [
            "END ON THE THESIS. The phrase the clip is ABOUT must be in the final quarter of the body and ideally in the last three spoken words. If the best line is at 60% through the span, TRIM THE SPAN so it ends there.",
            "Pick one of the six close shapes deliberately: thesis restatement, punchline, snap summary, reason-payoff, bookend, reversal.",
            "RESTATE IN DIFFERENT WORDS. Verbatim bookending is 1/62 in this corpus. Reusing >=3 content words from the opening sentence reads as a loop, not a landing.",
            "The snap close is a real weapon: 26% close on <=9 words after a longer build. When the body ran long, close short.",
            "END ON A COMPLETE CLAUSE. Do NOT adopt mid-word stopping as a style.",
        ],
        "measurement_trap": (
            "corpus.json's transcripts appear to show 24% of clips trailing off mid-word. That is an "
            "ARTIFACT of the ~6.4s CTA trim clipping the final word before transcription, not an "
            "editorial choice. Re-transcribed real tails were complete 6/6: '...so those are good "
            "PERPS', '...when it corrects, it's gonna be BRUTAL', '...I'm also TRADING'. Anyone "
            "re-deriving this must verify tails against the mp4, never the corpus text. Encoding "
            "'end mid-word for punch' would ship broken endings."
        ),
    },
    "self_containedness": {
        "zero_proper_noun_rate": 0.61,
        "three_plus_proper_noun_rate": 0.04,
        "addresses_you_or_we_rate": 0.84,
        "known_entities_only": ["Hyperliquid", "Solana", "Bitcoin", "Ethereum", "Polymarket",
                                "Coinbase", "Robinhood", "Amazon", "AWS", "S&P 500", "LeBron",
                                "Kardashian", "TikTok", "Instagram", "X", "Morgan Stanley", "IBM",
                                "SanDisk", "Interactive Brokers"],
        "tests": [
            {"id": "D1", "name": "PRONOUN TEST",
             "rule": "Every it/this/that/they/these/those/he/she resolves inside the span, or names the clip's own topic. A bare demonstrative in the first sentence is allowed only when the SAME sentence supplies the referent.",
             "pass_example": "This is the biggest moment in the history of capital markets",
             "fail_example": "That's what I was saying about their model",
             "automation": "READING INSTRUCTION, NOT A REGEX. A loose demonstrative probe hits 32/76 and most of those resolve fine on a human read. Do not automate this."},
            {"id": "D2", "name": "PROPER NOUN TEST",
             "rule": "Any name kept must be one a crypto/markets audience already knows. If the span needs a name introduced earlier in the source video, reject the span."},
            {"id": "D3", "name": "ROOM TEST",
             "rule": "Reject 'like I said earlier', 'to your point', 'going back to what you asked', 'as we discussed'. References to the interview appear in only 4/76 and are always incidental."},
            {"id": "D4", "name": "STANDALONE READ",
             "rule": "Read the span cold with no source context. If the first sentence needs a preceding sentence to parse, reject — do NOT fix it by extending backwards into preamble."},
            {"id": "D5", "name": "VIEWER ADDRESS (positive signal)",
             "rule": "84% of clips address 'you' or 'we' directly. Talking TO the viewer is itself the self-containment device — it replaces missing context with the viewer's own situation. Prefer it, all else equal."},
        ],
    },
    "cut_structure": {
        "edl_spans": {"prefer": 1, "max": 3, "hard_max": 4},
        "min_span_s": 4.0,
        "visible_segments_per_body": {"p25": 3, "median": 4, "p75": 5, "max": 8},
        "seconds_per_visible_segment": 4.4,
        "reorder_policy": "conservative",
        "rules": [
            "Prefer ONE span. Every corpus transcript reads as continuous prose in source order; the only proven EDL work is REMOVAL (dropping a sentence from the middle of a take), not assembly of scattered fragments.",
            "Minimum span 4s. Do not select a 2s fragment and hope it joins.",
            "MERGE CONTIGUOUS SPANS INTO ONE CUT. Never emit two EDL entries that share a boundary — the tail guard makes them overlap and the overlap replays as an audible stutter.",
            "Do not reorder unless promoting a hook to the front. If you do reorder, say so when reporting, and never reorder to invert the speaker's meaning.",
            "Let sludge produce the cut RHYTHM. Its silence removal (--max-gap 0.35) yields 3-6 visible framing changes in a 20s body — exactly the corpus rhythm. Do not chase a cut count in the EDL; chase a clean 18-24s of argument and the rhythm comes free.",
            "FRAME-CHECK EVERY CUT, not the clip. One top-pane frame per cut, hstacked, read as a strip: same person, same background, same head size.",
        ],
        "tension_with_sludge_skill": (
            "sludge's own SKILL.md says reordering 'is not just allowed, it is the main lever'. "
            "sludgify NARROWS that on purpose: reordering interacts badly with the butt-joined-cut "
            "stutter and with misrepresenting real people on camera. Evidence is also weak in both "
            "directions — a well-executed reorder is invisible in a transcript by construction. "
            "Default conservative; reorder only to promote a hook, and disclose it."
        ),
        "measurement_caveat": (
            "Cut counts are a PROXY. No EDLs were preserved; these are visible framing changes from "
            "top-pane scene detection (crop=1080:960:0:0). scene>0.18 gives median 4 segments; "
            "scene>0.06 gives median 6 with false positives up to 22 on high-motion sources. The "
            "split between sludge's automatic jump cuts and hand-authored EDL cuts is inferred."
        ),
    },
    "message_policy": {
        "verbatim_floor": 0.90,
        "alignment_threshold": 0.60,
        "words_for_20s_body": [55, 80],
        "rules": [
            "The --message must be the NEAR-VERBATIM transcript of the selected span — >=90% of the spoken words. It is fed to whisper as --initial_prompt.",
            "If you only have a slogan or a headline, pass NO message. Captions then come from the transcript and are correct. NEVER pass a paraphrase.",
            "A message below 60% alignment gets replaced by the transcript; a message that clears 60% gets captioned INSTEAD of the speech — so any speech the paraphrase does not cover renders with NO CAPTIONS AT ALL. Verbatim messages make both failure modes impossible.",
            "--no-prime is the escape when the transcript comes back swallowed.",
            "After rendering, transcribe the OUTPUT's own audio and read it. If it reads as a non-sequitur, fix the EDL, not the captions.",
        ],
    },
    "emphasis_policy": {
        "auto_emphasis": "ON",
        "auto_emphasis_hit_rate": 0.17,
        "clips_with_any_digit_rate": 0.24,
        "max_manual_emphasis_per_video": 2,
        "rules": [
            "Leave auto-emphasis ON. It fires on only 17% of corpus-like clips ($ / % / Nx), so it structurally cannot over-fire. Do not pass --no-auto-emphasis.",
            "Do NOT add asterisks to a span that already contains a number — the auto rule has it, and doubling up burns two beats of reading time.",
            "For the 83% of spans with no number: AT MOST ONE asterisk, on the single most screenshot-able word in the CLOSING clause — normally the slug word itself. Add none if no word clearly dominates.",
            "TIE-BREAKER: between two otherwise equal spans, take the one containing a number, a dollar amount or an Nx multiple — it reads with the sound off. But do not manufacture one; the corpus's best lines ('Users don't care about decentralization', 'Cut the noise') have none.",
            "Keep caption defaults: --caption-words 3, --caption-chars 24, uppercase, centred on the seam.",
        ],
    },
    "naming": {
        "filename": "sludge-{speaker}-{slug}.mp4",
        "speaker": "a bare lowercase handle or surname, no spaces",
        "slug_words": [3, 5],
        "slug_words_range_observed": [1, 6],
        "slug_chars_max": 38,
        "slug_from_last_sentence_rate": 0.57,
        "slug_from_first_sentence_rate": 0.49,
        "slug_verbatim_phrase_rate": 0.33,
        "slug_word_coverage_target": 0.80,
        "rules": [
            "The slug is a NEAR-VERBATIM LIFT of the punchline, not a topic label. Extract the payoff phrase; do not summarize.",
            "Prefer the clause carrying the reversal or the imperative.",
            ">=80% of slug words must appear in the transcript (holds for 54/76; 25/76 appear as an exact contiguous phrase).",
            "If you cannot write the slug from the CLOSING line, the clip does not land — re-cut it.",
        ],
    },
    "selection_policy": {
        "max_clips_per_primary_theme_per_video": 2,
        "rules": [
            "Enforce theme diversity: cap at 2 clips per primary theme per video, so one podcast does not ship five everything_tradable clips.",
            "Prefer spans that braid 2 themes over spans that max out 1.",
            "Prefer 15-25s of speech; accept 9-12s only when the span is a complete reversal or rant with a hard payoff (12 corpus clips are that short; i-just-want-raw-pnl is 9.9s and scores 56).",
            "ONE SPEAKER PER CLIP, ALWAYS. Any span containing a host turn is disqualified regardless of score.",
            "The score ORDERS candidates; it cannot ship one. Present a shortlist for judgement, never auto-cut the top N.",
            "Log every candidate's score and veto list on the first real run so the thresholds can be re-fit against actual rejected spans instead of hand-written negatives.",
        ],
    },
}

SLUDGE_INVOCATION = {
    "plan_defaults": {"--target-duration": 20},
    "render_defaults": {"--max-gap": 0.35, "--tail-pad": 0.25, "--cut-pad": 0.07, "--min-cut": 0.4,
                        "--caption-words": 3, "--caption-chars": 24},
    "conditional": [
        {"flag": "--headroom 1.4", "when": "the source is a multicam podcast, or any span crossing a camera-angle change",
         "why": "a cut from a wider angle otherwise locks at a lower scale and renders letterboxed while coverage still reports 100%"},
        {"flag": "--no-prime", "when": "the primed transcript comes back with implausible words/sec",
         "why": "a short --message used as --initial_prompt can swallow a whole take"},
    ],
    "never": [
        {"flag": "--no-auto-emphasis", "why": "auto-emphasis fires on only 17% of corpus-like clips; it cannot over-fire"},
        {"flag": "--tail-pad 0.5", "why": "documented in the sludge SKILL as having caused real overlap-stutter"},
        {"flag": "--max-gap 0.15", "why": "same — documented overlap-stutter"},
    ],
    "expected_noise": [
        "Beat match fails on this music bed (92.3 BPM, strength 0.28). Expected, not a bug. Do not chase it.",
        "Cut-edge WARNINGs are usually FALSE POSITIVES — they check raw whisper spans. Confirm with a 10-20ms RMS envelope: a continuous -10..-20 dB run is a real mid-word cut; a dip below -40 dB is silence and the warning is noise.",
    ],
}

PRODUCTION_LESSONS = [
    {"id": 1, "lesson": "Frame-check every cut for speaker identity.",
     "symptom": "Multicam podcasts cut between guest, hosts, wide 3-shots and b-roll. The head lock follows whatever face is on screen, reports 100% detector coverage, and warns about nothing.",
     "cost": "One batch shipped 24s of the wrong man's face. Another had sludge's own `suggested` selection pointing at segments where a HOST was speaking.",
     "fix": "Extract a frame at the midpoint of EACH segment, hstack them, confirm the same person. 100% coverage proves the tracker found A face, not the RIGHT face."},
    {"id": 2, "lesson": "Whisper priming can swallow a take.",
     "symptom": "The --message is fed as --initial_prompt. A short paraphrase can destroy the transcript.",
     "cost": "A 10s span came back as the two words 'when i'; unprimed it was 104 words.",
     "fix": "Check words-per-second sanity (2.0-4.5). Write --message near-verbatim. --no-prime is the escape."},
    {"id": 3, "lesson": "Never butt-join two EDL cuts.",
     "symptom": "Adjacent segments sharing a boundary always overlap once the tail guard runs.",
     "cost": "The overlap replays as an audible stutter.",
     "fix": "Merge contiguous segments into ONE cut."},
    {"id": 4, "lesson": "Letterbox trap on camera-angle changes.",
     "symptom": "A cut from a wider camera angle locks at a much lower scale than its neighbours and renders with black bars; coverage still says 100%.",
     "cost": "Black bars in a shipped clip.",
     "fix": "--headroom 1.4"},
    {"id": 5, "lesson": "Caption gaps from a partial message.",
     "symptom": "If a message clears 60% alignment the MESSAGE is captioned, not the speech.",
     "cost": "Speech the paraphrase doesn't cover renders with no captions at all.",
     "fix": "Near-verbatim message, or no message."},
    {"id": 6, "lesson": "Cut-edge warnings are usually false positives.",
     "symptom": "They check raw whisper spans, which are imprecise at boundaries.",
     "cost": "Wasted re-cuts chasing a non-problem.",
     "fix": "Confirm with a 10-20ms RMS envelope: continuous -10..-20 dB = real mid-word cut; a dip below -40 dB = silence, warning is false."},
    {"id": 7, "lesson": "Beat match fails on this music bed.",
     "symptom": "92.3 BPM, strength 0.28.", "cost": "None if recognised.",
     "fix": "Expected. Not a bug. Do not chase it."},
    {"id": 8, "lesson": "Renders run concurrently — every temp file needs a unique per-clip prefix.",
     "symptom": "Concurrent runs clobber generic render.log / plan.json in a shared scratchpad.",
     "cost": "Lost or crossed plans between clips.",
     "fix": "Prefix every temp file with the clip slug."},
]

# =============================================================================
# CURATED — negative controls (regression suite for the rubric)
# =============================================================================
# Spans a generic finance clipper WOULD take but this corpus never does, plus
# podcast housekeeping, host turns, filler, and two on-topic near-misses.
# (label, body_seconds, text)

NEGATIVE_CONTROLS = [
    ("macro_fed", 26, "So if you look at what the Fed is doing here, I think we get two more cuts by the end of the year. Inflation is coming in softer than expected, the CPI print was benign, and once real rates come down you're going to see liquidity flood back into risk assets. That's the whole ballgame for me."),
    ("price_target", 22, "My target is 250k on Bitcoin by the end of this cycle. I think we take out the all time high in Q2, we consolidate, and then the real blow off top comes at the end of next year. That's when you want to be taking profits."),
    ("regulation", 24, "The SEC has completely changed its posture. With the new administration you've got a Congress that's actually willing to pass a market structure bill, and once that lands the compliance overhang that's been hanging over this industry for four years finally goes away."),
    ("tokenomics", 23, "The problem with that token is the FDV. You've got a two billion dollar fully diluted valuation with eight percent float, the VCs are in at a two hundred million dollar valuation, and the unlock cliff hits in March. It's structurally impossible for that chart to go up."),
    ("technical_analysis", 20, "It's holding the 200 day moving average right here, and if you look at the weekly RSI it's the most oversold it's been since 2022. I want to see it reclaim that resistance level and then retest it as support before I add."),
    ("scam_drama", 25, "Look, everybody knew that was a rug. The team pulled the liquidity, the founder had already been sued twice, and the whole thing was a ponzi from day one. People lost eight hundred million dollars and nobody went to jail for it."),
    ("bitcoin_maxi", 24, "Bitcoin is the only sound money that has ever existed. Everything else is a security, everything else is somebody's liability. Fix the money, fix the world. The halving is a supply shock that the market structurally cannot price in ahead of time."),
    ("zk_infra", 26, "The zero knowledge proving costs have come down about a hundred x in eighteen months. Once you can prove a full block in under a second, the rollup can post to L1 continuously, and the validator set doesn't need to re-execute anything. That's the endgame for scaling."),
    ("ai_tech", 24, "The new model is beating every benchmark on the board. It's a two hundred thousand token context window, the training run was somewhere north of a hundred million dollars, and honestly the coding agents built on top of it are already better than most junior engineers."),
    ("institutional", 25, "Our mandate is fairly narrow. We're running about four billion in AUM, mostly pension and endowment money, so our allocation to alternatives is capped and the diligence cycle on any new manager is nine to twelve months minimum."),
    ("housekeeping", 18, "Welcome back to the show everybody. Before we get into it I want to thank our sponsor for making this episode possible. If you're enjoying this please subscribe and leave us a review, it genuinely helps."),
    ("host_question", 16, "So let me ask you about that. What do you think about where the market goes from here, and how do you see the next twelve months playing out for the space?"),
    ("filler_nothing", 21, "Yeah, I mean, you know, it's kind of one of those things where like, I don't know, it's hard to say. I think, um, we'll kind of see. It's, you know, it could go either way honestly. Right? Like, yeah."),
    ("personal_anecdote", 27, "So I grew up in New Jersey, my dad was a plumber, and I went to school for accounting because that's what you did. I worked at a firm for about six years and I hated every single day of it, and then a friend of mine called me up one afternoon."),
    ("lifestyle_flex", 20, "I bought the house in Miami, I've got the cars, I did the whole thing. And honestly after about six months none of it moves the needle for you anymore. It's just numbers going up in an account at that point."),
    ("generic_startup", 24, "Hiring is the hardest part of building a company. You have to get the first ten people right because they set the culture for everyone who comes after, and if you get one bad hire in that first ten it takes you two years to recover."),
    ("crosstalk", 14, "Right, right. Yeah, exactly. No, totally. And that's, I think that's exactly right. Yeah, a hundred percent. Go ahead, sorry."),
    ("ontopic_no_claim", 24, "So we launched the perps product in March, and then we added spot in June, and then we did the mobile app in September. And then in October we added a few more markets and the volumes have been fine, they've been steady."),
    ("ontopic_hedged_mush", 25, "I think trading is probably going to be important. It could go a lot of different ways honestly. There's a lot of products out there and some of them will probably work and some of them probably won't, it's hard to say which ones right now."),
]

# =============================================================================
# SCORER — must stay byte-identical in behaviour to the calibrated reference
# =============================================================================

_HARD_VETOES = {r["id"] for r in REJECT_LIST if r["severity"] == "hard"}
_ACTIVE_VETOES = {r["id"]: r["pattern"] for r in REJECT_LIST if r.get("pattern")}


def _term_hits(text: str, terms: list[str]) -> tuple[int, list[str]]:
    n, found = 0, []
    for t in terms:
        c = text.count(t) if " " in t else len(re.findall(r"\b" + re.escape(t) + r"\b", text))
        if c:
            n += c
            found.append(t)
    return n, found


def _rx_hits(text: str, pats: list[str]) -> int:
    return sum(len(re.findall(p, text, flags=re.M)) for p in pats)


def score_span(text: str, dur: float | None = None) -> tuple[float, float, str, dict]:
    """Return (score, base, primary_theme, debug). Plain regex, no models."""
    t = re.sub(r"\s+", " ", text.lower()).strip()
    W = max(len(re.findall(r"[a-z0-9'$%&]+", t)), 1)
    dur = dur or W / 3.13
    R = 100.0 / W
    dbg: dict = {}

    tf = WEIGHTS["theme_fit"]
    theme_pts_by_name = {}
    for name, lex in THEMES.items():
        c, _ = _term_hits(t, lex["core"])
        s, _ = _term_hits(t, lex["support"])
        raw = tf["core_weight"] * c + tf["support_weight"] * s
        theme_pts_by_name[name] = min(raw * R * tf["per_100_words_scale"], tf["per_theme_cap"])
    # Ties are common (short spans hit few terms). Break them by curated rank so the
    # primary_theme label — and therefore the per-video diversity cap — is deterministic
    # and does NOT depend on the order the THEMES dict happens to be written in.
    ranked = sorted(theme_pts_by_name.items(), key=lambda kv: (-kv[1], THEMES[kv[0]]["rank"]))
    theme_fit = min(ranked[0][1] + tf["second_theme_bonus"] * ranked[1][1], float(tf["max"]))
    dbg["themes"] = [(n, round(v, 1)) for n, v in ranked[:3]]

    rev = _rx_hits(t, STANCE_PATTERNS["reversal"])
    nc = _rx_hits(t, STANCE_PATTERNS["named_contrast"])
    dis = _rx_hits(t, STANCE_PATTERNS["dismissal"])
    con = _rx_hits(t, STANCE_PATTERNS["confession"])
    stance = min(6.0 * min(rev, 2) + 3.0 * min(nc, 2) + 3.0 * min(dis, 2) + 4.0 * min(con, 1), 20.0)
    dbg["stance"] = {"reversal": rev, "contrast": nc, "dismissal": dis, "confession": con}

    you = len(re.findall(YOU_RE, t))
    imp = _rx_hits(t, STANCE_PATTERNS["imperative"])
    addr = min(you * R * 1.6, 8.0) + min(2.0 * imp, 4.0)
    dbg["address"] = {"you": you, "imperative": imp}

    nums = len(re.findall(NUM_RE, t))
    ents, entf = _term_hits(t, ENTITIES)
    conc = min(2.0 * nums, 6.0) + min(2.0 * ents, 6.0)
    dbg["concrete"] = {"numbers": nums, "entities": entf}

    pr = _rx_hits(t, STANCE_PATTERNS["prediction"])
    pg = _rx_hits(t, STANCE_PATTERNS["progression"])
    mc = _rx_hits(t, STANCE_PATTERNS["mechanism"])
    shape = (3.0 if pr else 0.0) + (3.0 if pg else 0.0) + (3.0 if mc else 0.0)
    if re.search(r"\b(is|are|'s|'re|was|were)\b [^.?!]{6,}", t):
        shape += 1.5
    if not re.search(r"\b(and|but|so|because|that|the|a|to|of|with|it's|like)\s*$", t):
        shape += 1.5
    shape = min(shape, 12.0)
    dbg["shape"] = {"prediction": pr, "progression": pg, "mechanism": mc}

    wps = W / dur if dur else 3.13
    d = 8.0
    if wps < 2.0 or wps > 4.4:
        d -= 4.0
    elif wps < 2.4 or wps > 4.1:
        d -= 1.5
    fill = len(re.findall(FILLER_RE, t)) * R
    if fill > 12:
        d -= 3.0
    elif fill > 8:
        d -= 1.5
    delivery = max(d, 0.0)
    dbg["wps"] = round(wps, 2)
    dbg["filler_per_100w"] = round(fill, 1)

    if 15.0 <= dur <= 25.0:
        dur_pts = 6.0
    elif 12.0 <= dur < 15.0 or 25.0 < dur <= 29.0:
        dur_pts = 4.0
    elif 9.0 <= dur < 12.0 or 29.0 < dur <= 34.0:
        dur_pts = 2.0
    else:
        dur_pts = 0.0

    base = theme_fit + stance + addr + conc + shape + delivery + dur_pts

    vetoes = [k for k, p in _ACTIVE_VETOES.items() if re.search(p, t)]
    mult = 1.0
    for v in vetoes:
        mult *= VETO_MULTIPLIERS["hard"] if v in _HARD_VETOES else VETO_MULTIPLIERS["soft"]
    dbg["veto"] = vetoes

    gates = []
    all_core, all_sup = set(), set()
    for lx in THEMES.values():
        all_core |= set(lx["core"])
        all_sup |= set(lx["support"])
    dc = sum(1 for x in all_core if (x in t if " " in x else re.search(r"\b" + re.escape(x) + r"\b", t)))
    ds = sum(1 for x in all_sup if (x in t if " " in x else re.search(r"\b" + re.escape(x) + r"\b", t)))
    if not (dc >= 2 or (dc >= 1 and ds >= 3)):
        mult *= GATES["off_topic"]["multiplier"]
        gates.append("off_topic")
    claim = sum(len(re.findall(p, t)) for p in CLAIM_EVIDENCE_RES)
    claim += rev + nc + dis + con + pr + pg + mc + imp
    if claim == 0:
        mult *= GATES["no_claim"]["multiplier"]
        gates.append("no_claim")
    dbg["gates"] = gates
    dbg["core_terms"] = dc
    dbg["support_terms"] = ds
    dbg["claim_evidence"] = claim

    return round(base * mult, 1), round(base, 1), ranked[0][0], dbg


# =============================================================================
# MEASUREMENT
# =============================================================================

STEM_RE = re.compile(r"^sludge-([^-]+)-(.+)$")


def pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(int(q * len(s)), len(s) - 1)
    return s[i]


def dist(vals: list[float], nd: int = 1) -> dict:
    if not vals:
        return {}
    s = sorted(vals)
    return {
        "n": len(s),
        "min": round(s[0], nd), "p10": round(pct(s, 0.10), nd), "p25": round(pct(s, 0.25), nd),
        "median": round(statistics.median(s), nd), "p75": round(pct(s, 0.75), nd),
        "p90": round(pct(s, 0.90), nd), "max": round(s[-1], nd),
        "mean": round(statistics.mean(s), nd),
    }


def ffprobe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return None


def probe_top_pane_cuts(path: Path, threshold: float = 0.18) -> int | None:
    """Count visible framing changes in the head-locked TOP PANE (crop 1080x960 at 0,0)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             "-vf", f"crop=1080:960:0:0,select='gt(scene,{threshold})',metadata=print",
             "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300)
        return len(re.findall(r"lavfi\.scene_score", out.stderr + out.stdout)) + 1
    except Exception:
        return None


def load_clips(corpus_dir: Path, manifest: Path | None, transcripts: Path | None,
               cta_tail: float, probe: bool) -> list[dict]:
    manifest_by_stem: dict[str, dict] = {}
    if manifest and manifest.exists():
        for row in json.loads(manifest.read_text()):
            manifest_by_stem[row.get("file") or row.get("stem")] = row

    clips = []
    mp4s = sorted(corpus_dir.glob("*.mp4"))
    if not mp4s and manifest_by_stem:
        mp4s = [corpus_dir / f"{s}.mp4" for s in sorted(manifest_by_stem)]

    for mp4 in mp4s:
        stem = mp4.stem
        m = STEM_RE.match(stem)
        if not m:
            print(f"  skip (unparseable name): {stem}", file=sys.stderr)
            continue
        speaker, slug = m.group(1), m.group(2)

        text, body, words, src = None, None, None, None
        row = manifest_by_stem.get(stem)
        if row:
            text, body, words, src = row["text"], float(row["dur"]), row.get("words"), "manifest"
        elif transcripts:
            j, txt = transcripts / f"{stem}.json", transcripts / f"{stem}.txt"
            if j.exists():
                text, src = json.loads(j.read_text()).get("text", "").strip(), "whisper-json"
            elif txt.exists():
                text, src = txt.read_text().strip(), "txt"
        if not text:
            print(f"  skip (no transcript): {stem}", file=sys.stderr)
            continue

        total = ffprobe_duration(mp4) if (probe and mp4.exists()) else None
        if body is None:
            body = round((total - cta_tail), 2) if total else None
        if body is None:
            print(f"  skip (no duration): {stem}", file=sys.stderr)
            continue
        if words is None:
            words = len(re.findall(r"[A-Za-z0-9']+", text))

        clips.append({"stem": stem, "speaker": speaker, "slug": slug, "text": text,
                      "body_s": round(body, 2), "total_s": round(total, 2) if total else None,
                      "words": words, "text_source": src})
    return clips


def measure(clips: list[dict], probe_cuts_dir: Path | None) -> dict:
    bodies = [c["body_s"] for c in clips]
    totals = [c["total_s"] for c in clips if c["total_s"]]
    tails = [round(c["total_s"] - c["body_s"], 2) for c in clips if c["total_s"]]
    words = [c["words"] for c in clips]
    wps = [c["words"] / c["body_s"] for c in clips if c["body_s"] > 0]

    sent_counts, sent_lens, filler_rates, you_rates = [], [], [], []
    propn_counts, digit_clips, autoemph_clips = [], 0, 0
    for c in clips:
        sents = [s.strip() for s in re.split(r"(?<=[.?!])\s+", c["text"]) if s.strip()]
        sent_counts.append(len(sents))
        for s in sents:
            sent_lens.append(len(re.findall(r"[A-Za-z0-9']+", s)))
        low = c["text"].lower()
        w = max(c["words"], 1)
        filler_rates.append(100.0 * len(re.findall(FILLER_RE, low)) / w)
        you_rates.append(100.0 * len(re.findall(YOU_RE, low)) / w)
        # proper nouns: capitalised tokens that are not sentence-initial and not "I"
        pn = 0
        for s in sents:
            toks = re.findall(r"[A-Za-z][A-Za-z'&\.]*", s)
            for tok in toks[1:]:
                if tok[0].isupper() and tok not in ("I", "I'm", "I've", "I'd", "I'll"):
                    pn += 1
        propn_counts.append(pn)
        if re.search(r"\d", c["text"]):
            digit_clips += 1
        if re.search(AUTO_EMPHASIS_RE, low):
            autoemph_clips += 1

    n = len(clips)
    speakers = collections.Counter(c["speaker"] for c in clips)

    # slug grammar
    slug_words, slug_chars, cov80, verbatim, in_last3 = [], [], 0, 0, 0
    for c in clips:
        parts = c["slug"].split("-")
        slug_words.append(len(parts))
        slug_chars.append(len(c["slug"]))
        low = re.sub(r"[^a-z0-9 ]", " ", c["text"].lower())
        toks = low.split()
        present = sum(1 for p in parts if p in toks)
        if parts and present / len(parts) >= 0.80:
            cov80 += 1
        if " ".join(parts) in " ".join(toks):
            verbatim += 1
        tail3 = set(toks[-3:])
        if any(p in tail3 for p in parts):
            in_last3 += 1

    # term frequency, content words only
    STOP = set("""a an and are as at be been but by can could do does did for from get got had has have
        he her him his how i if in into is it its just like me my no not of on or our out she so some
        than that the their them then there these they this those to too up us was we were what when
        where which while who why will with would you your yeah okay right um uh really very much more
        most all any because been being about after also am another around back even down go going
        good great here how i'm it's don't that's there's you're we're kind sort mean know thing things
        way lot really actually gonna wanna nothing something anything everything im dont thats
        theres youre were""".split())
    tf = collections.Counter()
    for c in clips:
        for w in re.findall(r"[a-z']{3,}", c["text"].lower()):
            if w not in STOP:
                tf[w] += 1

    # absence probes: how many clips fire each reject pattern
    absence = {}
    for r in REJECT_LIST:
        if not r.get("pattern"):
            absence[r["id"]] = {"clips": None, "hits": None, "method": "judgement, no pattern"}
            continue
        hits = sum(len(re.findall(r["pattern"], c["text"].lower())) for c in clips)
        cl = sum(1 for c in clips if re.search(r["pattern"], c["text"].lower()))
        absence[r["id"]] = {"clips": cl, "hits": hits,
                            "regressed": hits > max(2, r["corpus_hits_at_build"] + 2)}

    out = {
        "n_clips": n,
        "total_body_s": round(sum(bodies), 1),
        "total_body_min": round(sum(bodies) / 60.0, 1),
        "total_words": sum(words),
        "body_duration_s": dist(bodies),
        "finished_duration_s": dist(totals) if totals else None,
        "cta_tail_s": dist(tails) if tails else None,
        "words_per_clip": dist([float(w) for w in words], 0),
        "words_per_second": dist(wps, 2),
        "sentences_per_clip": dist([float(x) for x in sent_counts], 0),
        "words_per_sentence": dist([float(x) for x in sent_lens], 0),
        "filler_per_100w": dist(filler_rates, 1),
        "you_per_100w": dist(you_rates, 2),
        "proper_nouns_per_clip_heuristic": dist([float(x) for x in propn_counts], 1),
        "zero_proper_noun_rate_heuristic": round(sum(1 for x in propn_counts if x == 0) / n, 3),
        "clips_with_any_digit_rate": round(digit_clips / n, 3),
        "auto_emphasis_hit_rate_regex": round(autoemph_clips / n, 3),
        "body_in_15_25s_rate": round(sum(1 for b in bodies if 15 <= b <= 25) / n, 3),
        "body_under_15s_rate": round(sum(1 for b in bodies if b < 15) / n, 3),
        "body_over_30s_rate": round(sum(1 for b in bodies if b > 30) / n, 3),
        "speakers": dict(speakers.most_common()),
        "speaker_count": len(speakers),
        "top2_speaker_share": round(sum(c for _, c in speakers.most_common(2)) / n, 3),
        "singleton_speakers": [s for s, c in speakers.items() if c == 1],
        "slug": {
            "words": dist([float(x) for x in slug_words], 0),
            "chars": dist([float(x) for x in slug_chars], 0),
            "word_coverage_80pct_rate": round(cov80 / n, 3),
            "verbatim_phrase_rate": round(verbatim / n, 3),
            "slug_word_in_last_3_spoken_rate": round(in_last3 / n, 3),
        },
        "top_content_terms": [{"term": t, "n": c} for t, c in tf.most_common(40)],
        "absence_probes": absence,
        "measurement_notes": {
            "zero_proper_noun_rate_heuristic": (
                "Counts any capitalised non-sentence-initial token, so it OVER-counts (mid-sentence "
                "'I', whisper's inconsistent casing, sentence-split errors). Reads ~0.49 where the "
                "hand count in editorial.self_containedness.zero_proper_noun_rate is 0.61. Trust the "
                "curated figure; this one is a drift tripwire, not a measurement."),
            "auto_emphasis_hit_rate_regex": (
                "Approximates sludge's $ / N% / Nx auto-emphasis trigger. Reads ~0.16 against a "
                "verified hand count of 0.17 (13/76). Close enough to detect drift, not exact."),
            "absence_probes": (
                "Regex counts only. Two reject-list entries carry a hand-judged 'near hit' in their "
                "`note` (a macro-shaped size phrase, one historical Bitcoin clause) that no regex "
                "fires on — which is why corpus_hits_at_build is 0 for them while the prose says 1."),
            "slug_word_in_last_3_spoken_rate": (
                "Computed against the TRIMMED body text, whose final word is clipped by the ~6.4s CTA "
                "trim. The true rate is slightly higher than reported."),
            "words_per_second": (
                "Computed against the trimmed body, which may be short by up to ~1s of real speech at "
                "the tail, so the true rate is very slightly lower. Treat 3.0-3.2 as the centre and "
                "2.0/4.5 as sanity bounds rather than trusting the median to two decimals."),
        },
    }

    if probe_cuts_dir:
        counts = []
        for c in clips:
            p = probe_top_pane_cuts(probe_cuts_dir / f"{c['stem']}.mp4")
            if p:
                counts.append(float(p))
        if counts:
            out["visible_segments_per_clip"] = dist(counts, 0)
            out["visible_segments_method"] = "ffmpeg crop=1080:960:0:0 select=gt(scene,0.18)"
    return out


def calibrate(clips: list[dict]) -> dict:
    scored = []
    for c in clips:
        s, b, th, dbg = score_span(c["text"], c["body_s"])
        scored.append({"slug": c["slug"], "speaker": c["speaker"], "score": s, "base": b,
                       "primary_theme": th, "veto": dbg["veto"], "gates": dbg["gates"]})
    neg = []
    for label, dur, text in NEGATIVE_CONTROLS:
        s, b, th, dbg = score_span(text, dur)
        neg.append({"label": label, "score": s, "primary_theme": th, "veto": dbg["veto"],
                    "gates": dbg["gates"]})

    cs = [r["score"] for r in scored]
    ns = [r["score"] for r in neg]
    bands = {}
    for name, spec in THRESHOLDS.items():
        floor = spec.get("from", spec.get("below"))
        if name == "reject":
            continue
        bands[name] = {
            "floor": floor,
            "corpus_recall": round(sum(1 for x in cs if x >= floor) / len(cs), 3),
            "negatives_admitted": sum(1 for x in ns if x >= floor),
        }
    return {
        "corpus": dist(cs, 1),
        "negatives": dist(ns, 1),
        "n_negative_controls": len(neg),
        "at_reject_threshold": {
            "threshold": THRESHOLDS["reject"]["below"],
            "corpus_recall": round(sum(1 for x in cs if x >= THRESHOLDS["reject"]["below"]) / len(cs), 3),
            "negatives_rejected": round(sum(1 for x in ns if x < THRESHOLDS["reject"]["below"]) / len(ns), 3),
        },
        "bands": bands,
        "primary_theme_distribution": dict(collections.Counter(r["primary_theme"] for r in scored).most_common()),
        "top_scoring": [{"slug": r["slug"], "score": r["score"], "primary_theme": r["primary_theme"]}
                        for r in sorted(scored, key=lambda r: -r["score"])[:10]],
        "below_reject_threshold": [{"slug": r["slug"], "score": r["score"]}
                                   for r in sorted(scored, key=lambda r: r["score"])
                                   if r["score"] < THRESHOLDS["reject"]["below"]],
        "negative_controls": sorted(neg, key=lambda r: -r["score"]),
        "per_clip": sorted(scored, key=lambda r: -r["score"]),
    }


def validate(clips: list[dict], measurements: dict) -> dict:
    slugs = {c["slug"] for c in clips}
    corpus_text = " ".join(c["text"].lower() for c in clips)
    problems, notes = [], []

    dead_core: dict[str, list[str]] = {}
    for tid, th in THEMES.items():
        missing = [s for s in th["exemplars"] if s not in slugs]
        if missing:
            problems.append(f"theme {tid}: exemplar slug(s) not in corpus: {', '.join(missing)}")
        dead = sorted(t for t in th["core"]
                      if not (t in corpus_text if " " in t
                              else re.search(r"\b" + re.escape(t) + r"\b", corpus_text)))
        if dead:
            dead_core[tid] = dead
        live = len(th["core"]) - len(dead)
        if live < 3:
            problems.append(
                f"theme {tid}: only {live} core term(s) actually occur in the corpus — "
                f"this lexicon is guessing, not observing")

    n_dead = sum(len(v) for v in dead_core.values())
    if n_dead:
        notes.append(
            f"{n_dead} core terms across {len(dead_core)} themes never occur in this corpus "
            f"(see validation.dead_core_terms). That is GENERALIZATION HEADROOM, not decay — the "
            f"lexicon covers synonyms a new speaker may use. Only worry when a theme drops below 3 "
            f"live terms, or when a dead term is a typo/variant the corpus writes differently "
            f"(whisper spells Robinhood 'robin hood', so both forms are listed).")

    for sh in RHETORICAL_SHAPES:
        missing = [s for s in sh["exemplars"] if s not in slugs]
        if missing:
            problems.append(f"shape {sh['id']}: exemplar slug(s) not in corpus: {', '.join(missing)}")

    dist_ = measurements.get("primary_theme_distribution", {})
    for tid in THEMES:
        if dist_.get(tid, 0) < 2:
            notes.append(f"theme {tid}: only {dist_.get(tid, 0)} primary clip(s) — consider merging or dropping")

    for rid, probe in measurements.get("absence_probes", {}).items():
        if probe.get("regressed"):
            notes.append(f"reject-list {rid}: now fires on {probe['clips']} clip(s) — the corpus changed shape, revisit this veto")

    return {"ok": not problems, "problems": problems, "notes": notes,
            "dead_core_terms": dead_core,
            "checked": {"exemplar_slugs": sum(len(t["exemplars"]) for t in THEMES.values())
                        + sum(len(s["exemplars"]) for s in RHETORICAL_SHAPES),
                        "themes": len(THEMES), "shapes": len(RHETORICAL_SHAPES),
                        "reject_entries": len(REJECT_LIST)}}


def build(clips: list[dict], corpus_dir: Path, manifest: Path | None,
          probe_cuts_dir: Path | None) -> dict:
    meas = measure(clips, probe_cuts_dir)
    cal = calibrate(clips)
    meas["primary_theme_distribution"] = cal["primary_theme_distribution"]
    val = validate(clips, meas)

    fingerprint = hashlib.sha256("\n".join(sorted(c["stem"] for c in clips)).encode()).hexdigest()[:16]

    themes_out = {}
    for tid, th in THEMES.items():
        themes_out[tid] = {
            "rank": th["rank"], "title": th["title"], "claim": th["claim"],
            "primary_clips": cal["primary_theme_distribution"].get(tid, 0),
            "core": th["core"], "support": th["support"],
            "exemplars": th["exemplars"], "quotable": th["quotable"],
            "discriminator": th["discriminator"],
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "profile_version": PROFILE_VERSION,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "generator": "sludgify/scripts/build_profile.py",
        "provenance": {
            "corpus_dir": str(corpus_dir),
            "manifest": str(manifest) if manifest else None,
            "n_clips": len(clips),
            "total_body_min": meas["total_body_min"],
            "total_words": meas["total_words"],
            "corpus_fingerprint": fingerprint,
            "speakers": meas["speakers"],
            "top2_speaker_share": meas["top2_speaker_share"],
            "curated_sections": ["one_paragraph", "themes", "house_pov", "rhetorical_shapes",
                                 "reject_list", "scoring", "editorial", "sludge_invocation",
                                 "production_lessons"],
            "measured_sections": ["measurements", "scoring.calibration",
                                  "themes.*.primary_clips", "validation"],
            "staleness": {
                "regenerate_when": "the corpus grows by >=15 clips, a new speaker dominates a batch, or 6 months pass",
                "command": "./scripts/build_profile.py --corpus <dir> --manifest <corpus.json> --out corpus-profile.json",
                "known_limits": [
                    "38/76 clips are two speakers (choppingblock, chriscamillo) — parts of the lexicon are their idiolect ('ground truth', 'connecting dots', 'designated account' are chriscamillo; 'vamped', 'meta shift', 'rite of passage' are choppingblock). On a new speaker with different diction theme fit UNDER-fires; lean on the support tier, the second-theme bonus, and read the 30-37 shortlist band by hand.",
                    "The rubric was tuned against hand-written negative controls, not real off-narrative spans from real podcasts. The 100%-rejection figure is a floor-check, not a field measurement.",
                    "Theme assignment is lexical and WILL mislabel (the luck/confession clip `fortunate` classifies as apps_over_infra on the words 'build' and 'technology'). primary_theme is for diversity capping and register selection — never show it to the user as a claim about what a clip is about.",
                    "themes.*.rank is CURATED (the hand-read ordering by corpus mass) while themes.*.primary_clips is MEASURED by the lexical scorer. They will not be perfectly monotonic — a hand-read rank-3 theme can measure fewer primary clips than rank 4, because a clip is assigned to whichever lexicon its words happen to hit. Neither number is wrong; they answer different questions.",
                    "Ten corpus clips score below the reject threshold. They were picked on delivery and punchline, not vocabulary. The rubric is an ordering tool with a hard floor, not a complete model of the user's taste.",
                ],
            },
        },
        "one_paragraph": ONE_PARAGRAPH,
        "themes": themes_out,
        "house_pov": HOUSE_POV,
        "rhetorical_shapes": RHETORICAL_SHAPES,
        "dominant_combination": DOMINANT_COMBINATION,
        "reject_list": REJECT_LIST,
        "scoring": {
            "implementation": "sludgify/scripts/build_profile.py :: score_span(text, body_seconds) -> (score, base, primary_theme, debug)",
            "weights": WEIGHTS,
            "stance_patterns": STANCE_PATTERNS,
            "entities": ENTITIES,
            "number_regex": NUM_RE,
            "filler_regex": FILLER_RE,
            "second_person_regex": YOU_RE,
            "auto_emphasis_regex": AUTO_EMPHASIS_RE,
            "claim_evidence_regexes": CLAIM_EVIDENCE_RES,
            "veto_multipliers": VETO_MULTIPLIERS,
            "gates": GATES,
            "thresholds": THRESHOLDS,
            "calibration": {k: v for k, v in cal.items() if k != "per_clip"},
            "post_score_gates": [
                "The score ORDERS candidates. It cannot ship one. Before any --edl is written:",
                "1. FRAME-CHECK every cut for speaker identity. 100% head-lock coverage proves nothing.",
                "2. MERGE contiguous segments into ONE cut. Never emit two EDL entries sharing a boundary.",
                "3. --headroom 1.4 whenever the span crosses a camera-angle change.",
                "4. --message near-verbatim, never a paraphrase. Verify >=60% word overlap against an unprimed pass; --no-prime is the escape.",
                "5. Cut-edge WARNINGs are unproven until an RMS envelope confirms them.",
                "6. Beat-match failure on the 92.3 BPM / 0.28-strength bed is expected.",
                "7. Prefix every temp file with the clip slug — renders run concurrently.",
            ],
        },
        "editorial": EDITORIAL,
        "sludge_invocation": SLUDGE_INVOCATION,
        "production_lessons": PRODUCTION_LESSONS,
        "measurements": meas,
        "validation": val,
        "per_clip_scores": cal["per_clip"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Regenerate corpus-profile.json from a corpus of finished sludge mp4s.")
    here = Path(__file__).resolve().parent
    ap.add_argument("--corpus", type=Path, required=True, help="directory of sludge-{speaker}-{slug}.mp4")
    ap.add_argument("--manifest", type=Path, default=None, help="corpus.json: [{file,speaker,slug,dur,words,text}] — dur is the SPEECH BODY")
    ap.add_argument("--transcripts", type=Path, default=None, help="directory of {stem}.json (whisper) or {stem}.txt sidecars")
    ap.add_argument("--out", type=Path, default=here.parent / "corpus-profile.json")
    ap.add_argument("--cta-tail", type=float, default=6.4, help="seconds of CTA tail to subtract from ffprobe totals when no manifest dur is available")
    ap.add_argument("--no-probe", action="store_true", help="skip ffprobe (finished/tail stats will be omitted)")
    ap.add_argument("--probe-cuts", action="store_true", help="scene-detect the top pane of every mp4 (slow)")
    ap.add_argument("--check", action="store_true", help="regenerate in memory and diff against --out; exit 1 on drift")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if not a.corpus.is_dir():
        print(f"error: --corpus {a.corpus} is not a directory", file=sys.stderr)
        return 2
    if not a.manifest and not a.transcripts:
        print("error: need --manifest or --transcripts for transcript text", file=sys.stderr)
        return 2

    clips = load_clips(a.corpus, a.manifest, a.transcripts, a.cta_tail, not a.no_probe)
    if not clips:
        print("error: no usable clips found", file=sys.stderr)
        return 2

    profile = build(clips, a.corpus, a.manifest, a.corpus if a.probe_cuts else None)

    if a.check:
        if not a.out.exists():
            print(f"DRIFT: {a.out} does not exist", file=sys.stderr)
            return 1
        old = json.loads(a.out.read_text())
        drift = []
        if old.get("provenance", {}).get("corpus_fingerprint") != profile["provenance"]["corpus_fingerprint"]:
            drift.append(f"corpus fingerprint: {old.get('provenance', {}).get('corpus_fingerprint')} -> {profile['provenance']['corpus_fingerprint']}")
        if old.get("provenance", {}).get("n_clips") != profile["provenance"]["n_clips"]:
            drift.append(f"n_clips: {old.get('provenance', {}).get('n_clips')} -> {profile['provenance']['n_clips']}")
        if json.dumps(old.get("measurements"), sort_keys=True) != json.dumps(profile["measurements"], sort_keys=True):
            drift.append("measurements block changed")
        if json.dumps(old.get("scoring", {}).get("calibration"), sort_keys=True) != json.dumps(profile["scoring"]["calibration"], sort_keys=True):
            drift.append("scoring.calibration changed")
        for d in drift:
            print(f"DRIFT: {d}")
        for p in profile["validation"]["problems"]:
            print(f"INVALID: {p}")
        if drift or profile["validation"]["problems"]:
            return 1
        print("profile is current")
        return 0

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(profile, indent=1, ensure_ascii=False) + "\n")

    if not a.quiet:
        v, m = profile["validation"], profile["measurements"]
        c = profile["scoring"]["calibration"]
        print(f"wrote {a.out}  ({a.out.stat().st_size / 1024:.1f} KB)")
        print(f"  clips {m['n_clips']}  body {m['total_body_min']} min  words {m['total_words']}  "
              f"speakers {m['speaker_count']}  fingerprint {profile['provenance']['corpus_fingerprint']}")
        print(f"  body_s   median {m['body_duration_s']['median']}  p25 {m['body_duration_s']['p25']}  p75 {m['body_duration_s']['p75']}")
        print(f"  words    median {m['words_per_clip']['median']}   wps median {m['words_per_second']['median']}")
        print(f"  score    corpus median {c['corpus']['median']}  p25 {c['corpus']['p25']}  |  negatives median {c['negatives']['median']}  max {c['negatives']['max']}")
        print(f"  at reject<{c['at_reject_threshold']['threshold']}: corpus recall {c['at_reject_threshold']['corpus_recall']:.0%}, negatives rejected {c['at_reject_threshold']['negatives_rejected']:.0%}")
        print(f"  validation: {'OK' if v['ok'] else 'PROBLEMS'}  ({v['checked']['exemplar_slugs']} exemplar slugs checked)")
        for p in v["problems"]:
            print(f"    PROBLEM: {p}")
        for nt in v["notes"]:
            print(f"    note: {nt}")
    return 0 if profile["validation"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
