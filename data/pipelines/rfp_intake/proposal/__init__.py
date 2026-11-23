"""Milestone 9 Part 2: proposal drafting and self-evaluation, per department.

It takes Part 1's handoff (`rfp_handoffs`, `rfp-intake/1`) and drafts each
active department's section of the pricing proposal (one generator agent per
department). Three evaluators check every draft in parallel: readability,
relevance, and compliance with briefing §5. A failing section goes back to
its generator with the evaluators' feedback, up to `RFP_MAX_DRAFT_ITERATIONS`.
A section that still fails ships as `needs_human_review`. Every section,
passed or not, goes to Part 3 in `rfp_proposal_handoffs` (`rfp-proposal/1`)
with its content and its structured `EvaluationResult`.

It lives inside `rfp_intake` because briefing §2.4 puts the RFP pipeline
there. Nothing here imports `services/`. Design: `docs/rfp/proposal-drafting.md`.
"""
