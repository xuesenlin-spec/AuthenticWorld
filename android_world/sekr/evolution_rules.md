# SEKR Evolution Rules

These are global constraints that govern how the SEKR knowledge evolution engine
should analyze failures and generate new knowledge entries. They are injected into
the LLM prompt during each evolution step.

## Core Principles

1. **Heuristics over Answers**: Only generate reusable operational heuristics (e.g.,
   "In Simple Gallery Pro, search is unreliable; try manually browsing DCIM folder").
   Never store task-specific answers (e.g., "the amount is $50").

2. **No Noise from Transient Errors**: If the failure is caused by network timeout,
   system crash, or app not installed, do NOT generate new knowledge. These are
   environmental issues, not operational knowledge gaps.

3. **App-Specific Guidance**: Each new entry must reference the specific app name
   and provide actionable steps. Vague rules like "try a different approach" are
   not useful.

4. **Avoid Redundancy**: Before proposing a new entry, check the existing knowledge
   base. If a similar rule already exists, do NOT add a duplicate. Minor wording
   changes do not constitute a new rule.

5. **Actionable Guidance**: The `guidance` field must be specific enough that an
   agent can follow it directly. It should describe WHAT to do and WHERE to do it.

6. **Evaluator Judgments Are Ground Truth**: The evaluator's judgment reasons are
   objective and fair. Any task that fails is due to a deficiency in the LLM Agent's
   behavior, not an unfair evaluation criterion. Do NOT dismiss a failure by claiming
   the evaluation is too strict. Instead, actively analyze the root cause and propose
   concrete actions that would improve the success rate.

7. **Pro Expense App Amount Unit**: When adding expenses to the **Pro Expense** app
   (package `com.arduia.expense`), the `amount` field must be entered in **cents**,
   not dollars. For example, if the amount is $45.50, the value stored must be `4550`.
   The agent sees dollar amounts in the source file (e.g., "$45.50") but must convert
   to cents (multiply by 100) before inputting into the app. Failure to convert
   (e.g., entering "45.50" instead of "4550") will cause validation to fail.
