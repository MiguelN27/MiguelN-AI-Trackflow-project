# RFP intake → Part 2 handoff contract (`rfp-intake/1`)

When an RFP intake ends `intake_complete`, Part 2 (proposal drafting) must be able to start **without re-parsing the PDF**. This contract is how. There is no second API: the handoff is a row in PostgreSQL.

## Where it lives

The `rfp_handoffs` table holds one row per completed ticket. It is written in the same transaction that stores the department sections and sets the ticket to `intake_complete` (`store.complete_intake`). So a ticket is never `intake_complete` without its handoff, and the reverse is also true.

| Column | Meaning |
|---|---|
| `ticket_id` | Primary key and foreign key to `rfp_tickets.id`. The briefing's `ticket_id`. |
| `contract_version` | `rfp-intake/1`. A breaking change to the payload gets a new version. |
| `next_stage` | `drafting`: the Part 2 stage that should pick it up. |
| `status` | `pending` until a Part 2 worker claims it, then `claimed`. |
| `payload` | JSONB, an `IntakeHandoff` (schema below). |
| `created_at`, `claimed_at`, `claimed_by` | When it was queued, and when and by whom it was claimed. |

The converted Markdown is also stored, on `rfp_tickets.markdown`, for a Part 2 step that needs more than the extracts. The PDF stays under `data/raw/rfp/<ticket_id>.pdf` (`payload.source.raw_pdf_path`) only as the original artifact.

## How Part 2 takes work

```python
from data.pipelines.rfp_intake.handoff import claim_next

claimed = claim_next(engine, claimed_by="proposal-drafting-worker-1")
if claimed is not None:
    handoff = claimed.handoff            # an IntakeHandoff
    for department in handoff.departments:
        ...                              # draft department.department_id's section
```

`claim_next` does three things in one transaction:

1. It selects the oldest `pending` row for `next_stage = "drafting"` with `FOR UPDATE SKIP LOCKED`, so two concurrent workers never get the same ticket.
2. It marks the row `claimed`.
3. It moves the ticket from `intake_complete` to `drafting` (briefing §2.3).

It returns `None` when the queue is empty. To read a handoff without claiming it, use `load_handoff(engine, ticket_id)`. From the command line, use `npm run rfp -- handoff <ticket_id>`.

## What the payload guarantees

- **`departments` lists only the departments the requested scope needs** (briefing §2.1), in the briefing's order (`warehouse`, `lastmile`, `reverse`), and holds at least one. `owner` is the briefing's owner, who approves that section in Part 3.
- **Every `key_aspects[].source_quote` was checked to appear verbatim** in the RFP or its extracted metadata.
- **No figure appears in the key aspects, needs, open questions, asks or brief unless the RFP states it.** A missing volume is an open question, never an estimate (briefing §2.3).
- **`client.currency` is derived from `client.country`** (US → USD, ES → EUR, briefing §5). It is `null` only when the country could not be established; then `sales_brief.client_questions` asks for it.
- **`deadline` is a date only when the RFP gives the day, month and year.** `deadline_text` keeps the RFP's own words ("around August 24th").
- **`sales_brief.unassigned_requirements`** lists what no department covers (for example ModaViva's ERP integration), so the proposal does not drop it silently.

## JSON Schema

Generated from `IntakeHandoff` by `npm run rfp -- handoff-schema --write`. `tests/pipelines/rfp_intake/test_store_and_handoff.py` fails when this block and the model drift apart.

<!-- handoff-schema:begin -->
```json
{
  "$defs": {
    "HandoffBrief": {
      "additionalProperties": false,
      "properties": {
        "summary": {
          "title": "Summary",
          "type": "string"
        },
        "client_questions": {
          "items": {
            "type": "string"
          },
          "title": "Client Questions",
          "type": "array"
        },
        "risks": {
          "items": {
            "type": "string"
          },
          "title": "Risks",
          "type": "array"
        },
        "unassigned_requirements": {
          "items": {
            "type": "string"
          },
          "title": "Unassigned Requirements",
          "type": "array"
        }
      },
      "required": [
        "summary",
        "client_questions",
        "risks",
        "unassigned_requirements"
      ],
      "title": "HandoffBrief",
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
    "HandoffDepartment": {
      "additionalProperties": false,
      "properties": {
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
          "description": "The department owner who approves this section in Part 3",
          "title": "Owner",
          "type": "string"
        },
        "objective": {
          "title": "Objective",
          "type": "string"
        },
        "ask": {
          "description": "What Sales asks the owner to provide",
          "title": "Ask",
          "type": "string"
        },
        "key_aspects": {
          "items": {
            "$ref": "#/$defs/HandoffKeyAspect"
          },
          "title": "Key Aspects",
          "type": "array"
        },
        "needed_from_department": {
          "items": {
            "type": "string"
          },
          "title": "Needed From Department",
          "type": "array"
        },
        "open_questions": {
          "items": {
            "type": "string"
          },
          "title": "Open Questions",
          "type": "array"
        }
      },
      "required": [
        "department_id",
        "department_name",
        "owner",
        "objective",
        "ask",
        "key_aspects",
        "needed_from_department",
        "open_questions"
      ],
      "title": "HandoffDepartment",
      "type": "object"
    },
    "HandoffKeyAspect": {
      "additionalProperties": false,
      "properties": {
        "aspect": {
          "title": "Aspect",
          "type": "string"
        },
        "detail": {
          "title": "Detail",
          "type": "string"
        },
        "source_quote": {
          "description": "Verbatim passage of the RFP or its metadata",
          "title": "Source Quote",
          "type": "string"
        }
      },
      "required": [
        "aspect",
        "detail",
        "source_quote"
      ],
      "title": "HandoffKeyAspect",
      "type": "object"
    },
    "HandoffReadability": {
      "additionalProperties": false,
      "properties": {
        "word_count": {
          "title": "Word Count",
          "type": "integer"
        },
        "estimated_tokens": {
          "title": "Estimated Tokens",
          "type": "integer"
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
        "processing_cost": {
          "enum": [
            "low",
            "medium",
            "high"
          ],
          "title": "Processing Cost",
          "type": "string"
        }
      },
      "required": [
        "word_count",
        "estimated_tokens",
        "flesch_kincaid_grade",
        "processing_cost"
      ],
      "title": "HandoffReadability",
      "type": "object"
    },
    "HandoffSource": {
      "additionalProperties": false,
      "properties": {
        "raw_pdf_path": {
          "title": "Raw Pdf Path",
          "type": "string"
        },
        "pdf_sha256": {
          "title": "Pdf Sha256",
          "type": "string"
        },
        "markdown_chars": {
          "title": "Markdown Chars",
          "type": "integer"
        },
        "markdown_truncated": {
          "title": "Markdown Truncated",
          "type": "boolean"
        }
      },
      "required": [
        "raw_pdf_path",
        "pdf_sha256",
        "markdown_chars",
        "markdown_truncated"
      ],
      "title": "HandoffSource",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "description": "What an RFP intake hands to Part 2 (proposal drafting).",
  "properties": {
    "contract_version": {
      "const": "rfp-intake/1",
      "default": "rfp-intake/1",
      "title": "Contract Version",
      "type": "string"
    },
    "next_stage": {
      "const": "drafting",
      "default": "drafting",
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
    "client": {
      "$ref": "#/$defs/HandoffClient"
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
      "description": "Orders per month; null when the RFP omits it",
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
    "budget_range": {
      "anyOf": [
        {
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "title": "Budget Range"
    },
    "departments": {
      "items": {
        "$ref": "#/$defs/HandoffDepartment"
      },
      "minItems": 1,
      "title": "Departments",
      "type": "array"
    },
    "sales_brief": {
      "$ref": "#/$defs/HandoffBrief"
    },
    "readability": {
      "$ref": "#/$defs/HandoffReadability"
    },
    "source": {
      "$ref": "#/$defs/HandoffSource"
    },
    "created_at": {
      "format": "date-time",
      "title": "Created At",
      "type": "string"
    }
  },
  "required": [
    "ticket_id",
    "rfp_id",
    "client",
    "services_requested",
    "monthly_volume",
    "deadline",
    "deadline_text",
    "budget_range",
    "departments",
    "sales_brief",
    "readability",
    "source",
    "created_at"
  ],
  "title": "IntakeHandoff",
  "type": "object"
}
```
<!-- handoff-schema:end -->
