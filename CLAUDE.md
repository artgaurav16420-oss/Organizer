Karpathy Guidelines v3.7 MANDATORY: For all AI operations in this project, you MUST follow karpathy-guidelines.md as the primary behavioral ruleset.

# CLAUDE.md

Project context for AI coding agents. Behavioral safety rules in karpathy-guidelines.md.

## Verification default
- Default mode: USER_VERIFY (ND1). AUTO_VERIFY requires explicit approval; never self-promote.

## Approval gates (ND1-ND6)
- ND1: DEFAULT_MODE = USER_VERIFY. AUTO_VERIFY needs explicit approval.
- ND2: EXTERNAL_STATE_APPROVAL before modifying db/cloud/auth/payments/user_data/file_storage.
- ND3: VERIFICATION_REQUIREMENT for every fix (reproduction + verification command).
- ND4: EXPLAIN_BEFORE_RISK for STANDARD/COMPLEX tasks.
- ND5: PRODUCTION_GUARD (no auto deploy).
- ND6: DESTRUCTIVE_OPS_GUARD (see karpathy-guidelines.md 4.3).
