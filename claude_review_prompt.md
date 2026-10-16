# Review prompt

You are reviewing the CatalogGuard repository in the current working directory. Read the code, tests, prompts, migration, and docs yourself. This prompt is orientation only. If it disagrees with the repository, trust the repository and say so.

Write the review to a new file in the repository root named `catalogguard_review_opus.md`. Do not modify application code, tests, or docs except to create that file. Do not implement fixes.

## What this project is

CatalogGuard is a product-catalog and per-store inventory service for a multi-store retailer. Inventory is tracked per (SKU, store). When on-hand quantity crosses below an effective threshold, the service:

1. Asks an LLM for a store-scoped SEO listing and stores it as a new versioned draft. It does not overwrite the live product SEO fields by itself.
2. Queues a low-stock email draft for a downstream mailer. It does not send email.

An operator can later publish a ready draft onto a store-scoped live listing (`store_listings`).

Stack: Python 3.12, FastAPI, SQLAlchemy 2, Pydantic v2, SQLite by default, Postgres via Docker Compose, Alembic for the production schema path. The default LLM is a deterministic stub. An OpenAI-compatible provider exists and defaults to the SpaceXAI / xAI endpoint.

Out of scope by design: authentication, real email delivery, a UI, multi-tenancy beyond stores, payments, and carts.

## Where to look

Start with `README.md`, then follow the code rather than the README's claims.

- `src/catalogguard/domain/` — threshold resolution, downward crossing, cooldown, listing length and citation rules, entities, events
- `src/catalogguard/application/` — inventory upsert, threshold handler, listing generation, email drafts, ports
- `src/catalogguard/infrastructure/` — SQLAlchemy, outbox bus, LLM providers, HTTP API
- `prompts/listing_rewrite/v1.md`, `prompts/email_urgency/v1.md`
- `evals/listing_guardrail.json`
- `adr/0001-kafka-event-bus.md`
- `migrations/`, `alembic.ini`, `src/catalogguard/migrate.py`
- `tests/`, `docker-compose.yml`, `Dockerfile`, `Makefile`, `.env.example`

Decisions that were intentional, so review the consequences rather than relitigating the choice itself:

- Effective threshold is the most specific defined value: (SKU, store) override, else SKU override, else the global default. Low stock means `quantity < threshold`.
- A low-stock event fires only on a downward crossing, and only outside a per-(SKU, store) cooldown. A crossing during the cooldown is dropped, not deferred.
- Listing rewrites are store-scoped drafts. Live product SEO is not auto-updated. Publish is a separate operator action.
- Inventory upserts take an idempotency key and an expected version.
- Email recipients come from a seeded `RecentInteraction` table (opaque customer ids, `view` or `add_to_cart`). That is a deliberate stand-in for a CRM. The email body is a template, not an LLM call.
- The in-process bus writes an outbox row in the caller's transaction, then leases rows (`claimed_at`, and `SKIP LOCKED` on Postgres) before dispatch. `KafkaEventBus` is a skeleton.

## How to review

Keep the review open. Do not score the project against a private checklist and stop. Form your own view of what matters for a production-grade catalog and inventory service, including things this prompt does not mention. You may disagree with the design, the tests, the docs, or this prompt. Say when you are unsure, and say what evidence would change your mind.

Be constructive, and give the reasoning.

- For each issue, explain why it matters, under what conditions it shows up, and what a reasonable response would be. A response can be a code change, a test, an operational guard, or a decision to leave it alone. Do not prescribe a patch as if it were the only acceptable fix.
- Separate defects from tradeoffs. A tradeoff that is documented and tested is not the same thing as a bug that the docs talk around.
- Name what is solid and why, not as padding. Future work should not undo those parts by accident.
- Prefer a few important findings over a long list of nits. Put style nits in a short appendix only if they affect maintenance.
- Do not treat "the tests passed" or "the README says so" as evidence that a behavior is correct. Read the assertion and the code path.
- Do not invent runtime results. If you did not run a command, say you did not run it. You may run `make test` or `make lint` if the environment allows. Docker was not available in the environment that built this repo, so do not assume Compose was executed.

## What to put in `catalogguard_review_opus.md`

Use this shape, in prose with headings. Tables are fine when they compare a few options.

1. **Context.** What you read and what you ran, if anything.
2. **What is in good shape.** The parts you would keep, and why they earn that.
3. **Findings.** Ordered by how much they matter. For each: what you observed, why it matters, and a constructive direction. Include severity only as a label (`high`, `medium`, `low`), after the reasoning.
4. **Open questions.** Design choices where more than one responsible answer exists. Give the tradeoff, not a verdict dressed up as a question.
5. **Suggested next reviews.** What you would look at next if you had another pass, and why that pass would be worth it.

Do not add a numeric score. End the file with a short note of what you deliberately did not review.
