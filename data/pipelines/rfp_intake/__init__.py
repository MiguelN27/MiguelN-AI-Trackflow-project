"""RFP intake and routing (Milestone 9 Part 1): the dedicated `rfp_intake` graph.

An uploaded PDF becomes a ticket. The graph converts it to Markdown, decides
whether it is a TrackFlow RFP, extracts its metadata and readability metrics,
splits the analysis across the departments the requested scope needs
(orchestrator -> workers -> synthesizer), and leaves a handoff for Part 2.

Entry points: `runner.run_intake` (API and CLI), `store` (persistence),
`handoff` (the contract Part 2 reads). Nothing here imports `services/`.
The design is in `docs/rfp/rfp-intake.md`.
"""
