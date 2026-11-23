# Proposal drafting → Part 3 handoff contract (`rfp-proposal/1`)

When Part 2 has drafted and evaluated every department's section of the pricing proposal, it hands the result to Part 3 (approval by each department owner). This contract is that handoff. There is no second API: the handoff is a row in PostgreSQL, like Part 1's ([handoff-contract.md](handoff-contract.md)).

The ticket's acceptance criterion: *the handoff to Part 3 must include, for every department, both the generated content and a structured evaluation result. Sections that exhausted the iteration limit ship with `needs_human_review`; they are not dropped.*

## Where it lives

The `rfp_proposal_handoffs` table holds one row per drafted ticket. It is written in the same transaction that sets the ticket's final Part 2 status (`proposal/store.complete_drafting`). So a ticket is never `waiting_for_approval` or `needs_human_review` without its proposal handoff, and the reverse is also true.

| Column | Meaning |
|---|---|
| `ticket_id` | Primary key and foreign key to `rfp_tickets.id`. |
| `contract_version` | `rfp-proposal/1`. A breaking change to the payload gets a new version. |
| `next_stage` | `approval`: the Part 3 stage that should pick it up. |
| `status` | `pending` until a Part 3 worker claims it, then `claimed`. |
| `needs_human_review` | True when at least one section did not pass within the iteration limit. |
| `payload` | JSONB, a `ProposalHandoff` (schema below). |
| `created_at`, `claimed_at`, `claimed_by` | When it was queued, and when and by whom it was claimed. |

Every department section is also stored on its `rfp_department_sections` row:
- `draft_content` holds the Markdown;
- `evaluation_results` holds the latest `EvaluationResult`;
- `draft_status`, `draft_iterations` and `draft_data` (the structured draft) hold the rest of its state;
- `approval_status` is `pending`.

Every attempt of the generator-evaluator loop is kept in `rfp_section_drafts`.

## How Part 3 takes work

```python
from data.pipelines.rfp_intake.proposal.handoff import claim_next

claimed = claim_next(engine, claimed_by="approval-worker-1")
if claimed is not None:
    proposal = claimed.proposal                  # a ProposalHandoff
    for section in proposal.sections:
        ...                                      # ask section.owner to approve section.draft_content
```

`claim_next` takes the oldest `pending` row for `next_stage = "approval"`, using `FOR UPDATE SKIP LOCKED`, and marks it `claimed`. It leaves the ticket's status to Part 3, which owns `waiting_for_approval` and `done`. To read a proposal without claiming it, use `load_proposal_handoff(engine, ticket_id)`. From the command line, use `npm run rfp -- proposal <ticket_id>`.

## What the payload guarantees

- **`status`:** `waiting_for_approval` when every section passed its three checks, `needs_human_review` when at least one did not. **Part 3 must accept both.** The ticket has the same status.
- **`sections` has one entry per department of the intake handoff**, in the briefing's order (`warehouse`, `lastmile`, `reverse`). None is ever dropped. Each entry is that department's "for approval" ticket:
  - `owner`: the briefing §2.1 owner who approves it;
  - `approval_status`: `pending`;
  - `assignment_summary`: one line on what was drafted and how the checks went;
  - `draft_content`: the Markdown;
  - `evaluation`: the structured `EvaluationResult`.
- **`evaluation` has the ticket's shape:**
  - `readability {pass, score, details}`;
  - `relevance {pass, missing_aspects}`;
  - `compliance {pass, rule_ids, violations}`;
  - `overall_pass`, `feedback_for_generator`.

  It is the evaluation of the shipped draft (its `iteration`). `compliance.rule_ids` are the violated rules from briefing §5:
  - `currency-by-country`;
  - `on-time-sla-stated`;
  - `returns-min-48h`;
  - `volume-discount-table`;
  - `no-carrier-rates`.
- **A `needs_human_review` section** is the last draft with its own evaluation. `review_note` explains when a technical failure stopped a later attempt.
- **`facts`** is what each section commits to, as data rather than prose. Part 3's conflict triggers (§7) read it:
  - `currencies` (`currency-mismatch`);
  - `returns_turnaround_hours` (`returns-sla-breach`);
  - `committed_capacity` and `assumed_monthly_volume` (`volume-vs-capacity`);
  - `on_time_sla_pct`.
- **Exactly one section has `is_sla_section: true`.** It is `lastmile` when last mile is in scope, otherwise the first active section. It states the on-time delivery SLA.
- **Every price comes from the rate card `rate_card_version`.** The rate card holds the knowledge base's figures plus Commercial's indicative list prices, to be confirmed. No figure appears that is not in the RFP, the rate card or the knowledge base.
- **`metrics.average_iterations_per_section`** is the briefing §3 KPI (target: fewer than 2).

## JSON Schema

Generated from `ProposalHandoff` by `npm run rfp -- proposal-schema --write`. `tests/pipelines/rfp_intake/proposal/test_store_and_handoff.py` fails when this block and the model drift apart.

<!-- proposal-schema:begin -->
```json
{
  "$defs": {
    "ComplianceResult": {
      "additionalProperties": false,
      "properties": {
        "pass": {
          "title": "Pass",
          "type": "boolean"
        },
        "rule_ids": {
          "description": "The rules this section violates",
          "items": {
            "enum": [
              "currency-by-country",
              "on-time-sla-stated",
              "returns-min-48h",
              "volume-discount-table",
              "no-carrier-rates"
            ],
            "type": "string"
          },
          "title": "Rule Ids",
          "type": "array"
        },
        "violations": {
          "items": {
            "$ref": "#/$defs/Violation"
          },
          "title": "Violations",
          "type": "array"
        },
        "checked_rule_ids": {
          "items": {
            "enum": [
              "currency-by-country",
              "on-time-sla-stated",
              "returns-min-48h",
              "volume-discount-table",
              "no-carrier-rates"
            ],
            "type": "string"
          },
          "title": "Checked Rule Ids",
          "type": "array"
        },
        "reviewer": {
          "default": "off",
          "enum": [
            "used",
            "off",
            "unavailable"
          ],
          "title": "Reviewer",
          "type": "string"
        }
      },
      "required": [
        "pass",
        "rule_ids",
        "violations"
      ],
      "title": "ComplianceResult",
      "type": "object"
    },
    "EvaluationResult": {
      "additionalProperties": false,
      "properties": {
        "section_id": {
          "format": "uuid",
          "title": "Section Id",
          "type": "string"
        },
        "department_id": {
          "enum": [
            "warehouse",
            "lastmile",
            "reverse"
          ],
          "title": "Department Id",
          "type": "string"
        },
        "iteration": {
          "minimum": 1,
          "title": "Iteration",
          "type": "integer"
        },
        "readability": {
          "$ref": "#/$defs/ReadabilityResult"
        },
        "relevance": {
          "$ref": "#/$defs/RelevanceResult"
        },
        "compliance": {
          "$ref": "#/$defs/ComplianceResult"
        },
        "overall_pass": {
          "title": "Overall Pass",
          "type": "boolean"
        },
        "feedback_for_generator": {
          "description": "Concrete, actionable fixes for the next draft; empty when it passed",
          "title": "Feedback For Generator",
          "type": "string"
        },
        "evaluated_at": {
          "format": "date-time",
          "title": "Evaluated At",
          "type": "string"
        }
      },
      "required": [
        "section_id",
        "department_id",
        "iteration",
        "readability",
        "relevance",
        "compliance",
        "overall_pass",
        "feedback_for_generator",
        "evaluated_at"
      ],
      "title": "EvaluationResult",
      "type": "object"
    },
    "FactCapacity": {
      "additionalProperties": false,
      "properties": {
        "pallet_positions": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Pallet Positions"
        },
        "monthly_orders": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Monthly Orders"
        }
      },
      "required": [
        "pallet_positions",
        "monthly_orders"
      ],
      "title": "FactCapacity",
      "type": "object"
    },
    "FactCarriers": {
      "additionalProperties": false,
      "properties": {
        "destination": {
          "title": "Destination",
          "type": "string"
        },
        "carriers": {
          "items": {
            "type": "string"
          },
          "title": "Carriers",
          "type": "array"
        }
      },
      "required": [
        "destination",
        "carriers"
      ],
      "title": "FactCarriers",
      "type": "object"
    },
    "FactPrice": {
      "additionalProperties": false,
      "properties": {
        "item_id": {
          "title": "Item Id",
          "type": "string"
        },
        "label": {
          "title": "Label",
          "type": "string"
        },
        "amount": {
          "title": "Amount",
          "type": "number"
        },
        "currency": {
          "title": "Currency",
          "type": "string"
        },
        "unit": {
          "title": "Unit",
          "type": "string"
        },
        "source": {
          "enum": [
            "knowledge_base",
            "commercial_indicative"
          ],
          "title": "Source",
          "type": "string"
        }
      },
      "required": [
        "item_id",
        "label",
        "amount",
        "currency",
        "unit",
        "source"
      ],
      "title": "FactPrice",
      "type": "object"
    },
    "FactTier": {
      "additionalProperties": false,
      "properties": {
        "min_monthly_volume": {
          "title": "Min Monthly Volume",
          "type": "integer"
        },
        "max_monthly_volume": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Max Monthly Volume"
        },
        "discount_pct": {
          "title": "Discount Pct",
          "type": "number"
        }
      },
      "required": [
        "min_monthly_volume",
        "max_monthly_volume",
        "discount_pct"
      ],
      "title": "FactTier",
      "type": "object"
    },
    "HandoffClient": {
      "additionalProperties": false,
      "properties": {
        "name": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "title": "Name"
        },
        "country": {
          "anyOf": [
            {
              "enum": [
                "US",
                "ES"
              ],
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "title": "Country"
        },
        "currency": {
          "anyOf": [
            {
              "enum": [
                "USD",
                "EUR"
              ],
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "description": "USD for US operations, EUR for Spain (§5)",
          "title": "Currency"
        }
      },
      "required": [
        "name",
        "country",
        "currency"
      ],
      "title": "HandoffClient",
      "type": "object"
    },
    "ProposalMetrics": {
      "additionalProperties": false,
      "properties": {
        "sections": {
          "title": "Sections",
          "type": "integer"
        },
        "average_iterations_per_section": {
          "description": "Briefing §3 KPI: fewer than 2",
          "title": "Average Iterations Per Section",
          "type": "number"
        },
        "passed_first_attempt": {
          "title": "Passed First Attempt",
          "type": "integer"
        },
        "needs_human_review": {
          "title": "Needs Human Review",
          "type": "integer"
        }
      },
      "required": [
        "sections",
        "average_iterations_per_section",
        "passed_first_attempt",
        "needs_human_review"
      ],
      "title": "ProposalMetrics",
      "type": "object"
    },
    "ProposalSection": {
      "additionalProperties": false,
      "description": "One department's assignment ticket \"for approval\" (visual reference).",
      "properties": {
        "section_id": {
          "format": "uuid",
          "title": "Section Id",
          "type": "string"
        },
        "department_id": {
          "enum": [
            "warehouse",
            "lastmile",
            "reverse"
          ],
          "title": "Department Id",
          "type": "string"
        },
        "department_name": {
          "title": "Department Name",
          "type": "string"
        },
        "owner": {
          "description": "Approves this section in Part 3 (briefing §2.1)",
          "title": "Owner",
          "type": "string"
        },
        "is_sla_section": {
          "description": "This section states the proposal's on-time SLA",
          "title": "Is Sla Section",
          "type": "boolean"
        },
        "draft_status": {
          "enum": [
            "passed",
            "needs_human_review"
          ],
          "title": "Draft Status",
          "type": "string"
        },
        "needs_human_review": {
          "title": "Needs Human Review",
          "type": "boolean"
        },
        "iterations": {
          "minimum": 1,
          "title": "Iterations",
          "type": "integer"
        },
        "max_iterations": {
          "title": "Max Iterations",
          "type": "integer"
        },
        "approval_status": {
          "const": "pending",
          "default": "pending",
          "title": "Approval Status",
          "type": "string"
        },
        "assignment_summary": {
          "title": "Assignment Summary",
          "type": "string"
        },
        "draft_content": {
          "title": "Draft Content",
          "type": "string"
        },
        "facts": {
          "$ref": "#/$defs/SectionFacts"
        },
        "evaluation": {
          "$ref": "#/$defs/EvaluationResult"
        },
        "knowledge_source": {
          "title": "Knowledge Source",
          "type": "string"
        },
        "review_note": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Review Note"
        }
      },
      "required": [
        "section_id",
        "department_id",
        "department_name",
        "owner",
        "is_sla_section",
        "draft_status",
        "needs_human_review",
        "iterations",
        "max_iterations",
        "assignment_summary",
        "draft_content",
        "facts",
        "evaluation",
        "knowledge_source"
      ],
      "title": "ProposalSection",
      "type": "object"
    },
    "ReadabilityDetails": {
      "additionalProperties": false,
      "properties": {
        "scored": {
          "description": "False when the scores could not be computed",
          "title": "Scored",
          "type": "boolean"
        },
        "flesch_reading_ease": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "title": "Flesch Reading Ease"
        },
        "flesch_kincaid_grade": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "title": "Flesch Kincaid Grade"
        },
        "gunning_fog": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "title": "Gunning Fog"
        },
        "word_count": {
          "title": "Word Count",
          "type": "integer"
        },
        "sentence_count": {
          "title": "Sentence Count",
          "type": "integer"
        },
        "words_per_sentence": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "title": "Words Per Sentence"
        },
        "longest_sentence": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "title": "Longest Sentence"
        },
        "min_ease": {
          "title": "Min Ease",
          "type": "number"
        },
        "max_grade": {
          "title": "Max Grade",
          "type": "number"
        },
        "note": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Note"
        }
      },
      "required": [
        "scored",
        "flesch_reading_ease",
        "flesch_kincaid_grade",
        "gunning_fog",
        "word_count",
        "sentence_count",
        "words_per_sentence",
        "longest_sentence",
        "min_ease",
        "max_grade"
      ],
      "title": "ReadabilityDetails",
      "type": "object"
    },
    "ReadabilityResult": {
      "additionalProperties": false,
      "properties": {
        "pass": {
          "title": "Pass",
          "type": "boolean"
        },
        "score": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "description": "Flesch reading ease of the section's prose",
          "title": "Score"
        },
        "details": {
          "$ref": "#/$defs/ReadabilityDetails"
        }
      },
      "required": [
        "pass",
        "score",
        "details"
      ],
      "title": "ReadabilityResult",
      "type": "object"
    },
    "RelevanceResult": {
      "additionalProperties": false,
      "properties": {
        "pass": {
          "title": "Pass",
          "type": "boolean"
        },
        "missing_aspects": {
          "description": "What the RFP or the briefing asks this section for and it does not answer",
          "items": {
            "type": "string"
          },
          "title": "Missing Aspects",
          "type": "array"
        },
        "covered_aspects": {
          "items": {
            "type": "string"
          },
          "title": "Covered Aspects",
          "type": "array"
        },
        "not_applicable": {
          "items": {
            "type": "string"
          },
          "title": "Not Applicable",
          "type": "array"
        }
      },
      "required": [
        "pass",
        "missing_aspects"
      ],
      "title": "RelevanceResult",
      "type": "object"
    },
    "SectionFacts": {
      "additionalProperties": false,
      "description": "What the section commits to, as data (for Part 3's §7 conflict triggers).",
      "properties": {
        "currencies": {
          "description": "Every currency the section's prices are in",
          "items": {
            "type": "string"
          },
          "title": "Currencies",
          "type": "array"
        },
        "price_lines": {
          "items": {
            "$ref": "#/$defs/FactPrice"
          },
          "title": "Price Lines",
          "type": "array"
        },
        "discount_tiers": {
          "items": {
            "$ref": "#/$defs/FactTier"
          },
          "title": "Discount Tiers",
          "type": "array"
        },
        "on_time_sla_pct": {
          "anyOf": [
            {
              "type": "number"
            },
            {
              "type": "null"
            }
          ],
          "title": "On Time Sla Pct"
        },
        "returns_turnaround_hours": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Returns Turnaround Hours"
        },
        "onboarding_business_days": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Onboarding Business Days"
        },
        "committed_capacity": {
          "anyOf": [
            {
              "$ref": "#/$defs/FactCapacity"
            },
            {
              "type": "null"
            }
          ]
        },
        "assumed_monthly_volume": {
          "anyOf": [
            {
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "title": "Assumed Monthly Volume"
        },
        "carriers_by_destination": {
          "items": {
            "$ref": "#/$defs/FactCarriers"
          },
          "title": "Carriers By Destination",
          "type": "array"
        },
        "open_questions": {
          "items": {
            "type": "string"
          },
          "title": "Open Questions",
          "type": "array"
        },
        "assumptions": {
          "items": {
            "type": "string"
          },
          "title": "Assumptions",
          "type": "array"
        }
      },
      "required": [
        "currencies",
        "price_lines",
        "discount_tiers",
        "on_time_sla_pct",
        "returns_turnaround_hours",
        "onboarding_business_days",
        "committed_capacity",
        "assumed_monthly_volume",
        "carriers_by_destination",
        "open_questions",
        "assumptions"
      ],
      "title": "SectionFacts",
      "type": "object"
    },
    "Violation": {
      "additionalProperties": false,
      "properties": {
        "rule_id": {
          "enum": [
            "currency-by-country",
            "on-time-sla-stated",
            "returns-min-48h",
            "volume-discount-table",
            "no-carrier-rates"
          ],
          "title": "Rule Id",
          "type": "string"
        },
        "message": {
          "title": "Message",
          "type": "string"
        },
        "quote": {
          "anyOf": [
            {
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "description": "The offending passage, verbatim",
          "title": "Quote"
        },
        "source": {
          "default": "check",
          "enum": [
            "check",
            "reviewer"
          ],
          "title": "Source",
          "type": "string"
        },
        "retryable": {
          "default": true,
          "title": "Retryable",
          "type": "boolean"
        }
      },
      "required": [
        "rule_id",
        "message"
      ],
      "title": "Violation",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "description": "What Part 2 hands to Part 3 (approval by each department owner).",
  "properties": {
    "contract_version": {
      "const": "rfp-proposal/1",
      "default": "rfp-proposal/1",
      "title": "Contract Version",
      "type": "string"
    },
    "next_stage": {
      "const": "approval",
      "default": "approval",
      "title": "Next Stage",
      "type": "string"
    },
    "ticket_id": {
      "format": "uuid",
      "title": "Ticket Id",
      "type": "string"
    },
    "rfp_id": {
      "format": "uuid",
      "title": "Rfp Id",
      "type": "string"
    },
    "status": {
      "enum": [
        "waiting_for_approval",
        "needs_human_review"
      ],
      "title": "Status",
      "type": "string"
    },
    "client": {
      "$ref": "#/$defs/HandoffClient"
    },
    "currency": {
      "anyOf": [
        {
          "enum": [
            "USD",
            "EUR"
          ],
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Currency"
    },
    "services_requested": {
      "items": {
        "enum": [
          "warehousing",
          "last_mile",
          "reverse_logistics"
        ],
        "type": "string"
      },
      "title": "Services Requested",
      "type": "array"
    },
    "monthly_volume": {
      "anyOf": [
        {
          "type": "integer"
        },
        {
          "type": "null"
        }
      ],
      "title": "Monthly Volume"
    },
    "deadline": {
      "anyOf": [
        {
          "format": "date",
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Deadline"
    },
    "deadline_text": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Deadline Text"
    },
    "sla_department": {
      "enum": [
        "warehouse",
        "lastmile",
        "reverse"
      ],
      "title": "Sla Department",
      "type": "string"
    },
    "rate_card_version": {
      "title": "Rate Card Version",
      "type": "string"
    },
    "max_iterations": {
      "title": "Max Iterations",
      "type": "integer"
    },
    "sections": {
      "items": {
        "$ref": "#/$defs/ProposalSection"
      },
      "minItems": 1,
      "title": "Sections",
      "type": "array"
    },
    "metrics": {
      "$ref": "#/$defs/ProposalMetrics"
    },
    "client_questions": {
      "items": {
        "type": "string"
      },
      "title": "Client Questions",
      "type": "array"
    },
    "unassigned_requirements": {
      "items": {
        "type": "string"
      },
      "title": "Unassigned Requirements",
      "type": "array"
    },
    "generated_at": {
      "format": "date-time",
      "title": "Generated At",
      "type": "string"
    }
  },
  "required": [
    "ticket_id",
    "rfp_id",
    "status",
    "client",
    "currency",
    "services_requested",
    "monthly_volume",
    "deadline",
    "deadline_text",
    "sla_department",
    "rate_card_version",
    "max_iterations",
    "sections",
    "metrics",
    "client_questions",
    "unassigned_requirements",
    "generated_at"
  ],
  "title": "ProposalHandoff",
  "type": "object"
}
```
<!-- proposal-schema:end -->
