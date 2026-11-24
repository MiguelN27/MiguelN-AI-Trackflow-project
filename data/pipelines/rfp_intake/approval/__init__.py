"""Part 3 of the RFP workflow: human approval per department, arbitration, final document.

Part 2 queues every drafted proposal in `rfp_proposal_handoffs` (`rfp-proposal/1`).
This package consumes that queue with one LangGraph graph per ticket,
checkpointed in PostgreSQL (thread `rfp-<ticket_id>`):

- each department's section goes to its owner (briefing §2.1) through a real
  `interrupt`; the pause holds only that department's branch, so the others
  keep moving;
- an arbitration node applies the briefing's §7 rules, under the named arbiter,
  whenever the sections contradict each other;
- once every owner has approved, the final document is composed from the
  approved sections, and the ticket is `done`.

Nothing here imports `services/`. See `docs/rfp/approval-completion.md`.
"""
