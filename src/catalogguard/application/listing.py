"""Generate a store-scoped listing draft. Never writes the live product SEO fields."""

import logging
from dataclasses import replace
from uuid import uuid4

from catalogguard.application.commands import GenerateListing
from catalogguard.application.listing_validation import (
    parse_llm_json,
    render_customer_copy,
    text_field,
    validate_listing,
)
from catalogguard.application.ports import Clock, LLMProvider, Metrics, UnitOfWorkFactory
from catalogguard.application.prompts import PromptTemplate, fill_template, listing_prompt_values
from catalogguard.domain.errors import (
    ConflictError,
    CostLimitError,
    NotFoundError,
    OptimisticConflictError,
)
from catalogguard.domain.models import ListingDraft, StoreListing, StoreListingRevision

log = logging.getLogger(__name__)


def _published_text(value: str) -> str:
    rendered = render_customer_copy(value)
    if not rendered:
        raise ConflictError("Publishing would leave a listing field empty")
    return rendered


class ListingService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        llm: LLMProvider,
        template: PromptTemplate,
        metrics: Metrics,
        clock: Clock,
        *,
        daily_cap: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._llm = llm
        self._template = template
        self._metrics = metrics
        self._clock = clock
        self._daily_cap = daily_cap

    async def generate(self, cmd: GenerateListing) -> ListingDraft:
        async with self._uow_factory() as uow:
            existing = await uow.drafts.get_by_trigger(cmd.trigger_event_id)
            if existing is not None:
                return existing
            product = await uow.products.get(cmd.sku)
            store = await uow.stores.get(cmd.store_id)
        if product is None:
            raise NotFoundError(f"Product {cmd.sku} not found")
        if store is None:
            raise NotFoundError(f"Store {cmd.store_id} not found")
        await self._reserve_llm_call(cmd.sku)

        user_prompt = fill_template(
            self._template.user,
            listing_prompt_values(product, store),
        )
        log.info(
            "listing_rewrite_requested sku=%s store=%s prompt=%s trigger=%s",
            cmd.sku,
            cmd.store_id,
            self._template.prompt_version,
            cmd.trigger_event_id,
        )
        response = await self._llm.complete(system=self._template.system, user=user_prompt)
        data = parse_llm_json(response.content)
        violations = validate_listing(data, dict(product.attributes))
        status = "ready" if not violations else "validation_failed"
        cited_raw = data.get("cited_attributes")
        cited = (
            [item for item in cited_raw if isinstance(item, str)]
            if isinstance(cited_raw, list)
            else []
        )
        draft = ListingDraft(
            id=str(uuid4()),
            sku=cmd.sku,
            store_id=cmd.store_id,
            version=0,
            prompt_version=self._template.prompt_version,
            model=response.model,
            status=status,
            reason=cmd.reason,
            title=text_field(data, "title"),
            short_description=text_field(data, "short_description"),
            long_description=text_field(data, "long_description"),
            meta_title=text_field(data, "meta_title"),
            meta_description=text_field(data, "meta_description"),
            cited_attributes=cited,
            violations=violations,
            raw_output=response.content[:20000],
            trigger_event_id=cmd.trigger_event_id,
            correlation_id=cmd.correlation_id,
            created_at=self._clock.now(),
        )
        async with self._uow_factory() as uow:
            existing = await uow.drafts.get_by_trigger(cmd.trigger_event_id)
            if existing is not None:
                return existing
            version = await uow.drafts.next_version(cmd.sku, cmd.store_id)
            draft = replace(draft, version=version)
            await uow.drafts.add(draft)
            await uow.processed.mark(cmd.trigger_event_id, "listing_rewrite", draft.created_at)
            await uow.commit()
        self._metrics.rewrite("succeeded" if status == "ready" else "failed")
        log.info(
            "listing_draft_stored sku=%s store=%s version=%s status=%s violations=%s",
            draft.sku,
            draft.store_id,
            draft.version,
            draft.status,
            len(draft.violations),
        )
        return draft

    async def _reserve_llm_call(self, sku: str) -> None:
        if self._daily_cap <= 0:
            return
        usage_date = self._clock.now().date().isoformat()
        async with self._uow_factory() as uow:
            allowed = await uow.usage.try_consume(sku, usage_date, self._daily_cap)
            await uow.commit()
        if not allowed:
            raise CostLimitError(f"Daily LLM cap of {self._daily_cap} calls reached for SKU {sku}")

    async def publish(self, draft_id: str, expected_version: int) -> StoreListing:
        """Copy a ready draft onto the live store listing. Product SEO stays put."""

        async with self._uow_factory() as uow:
            draft = await uow.drafts.get(draft_id)
            if draft is None:
                raise NotFoundError(f"Listing draft {draft_id} not found")
            if draft.status == "validation_failed":
                raise ConflictError("A draft that failed validation cannot be published")
            product = await uow.products.get(draft.sku)
            if product is None:
                raise NotFoundError(f"Product {draft.sku} not found")
            current_violations = validate_listing(
                {
                    "title": draft.title,
                    "short_description": draft.short_description,
                    "long_description": draft.long_description,
                    "meta_title": draft.meta_title,
                    "meta_description": draft.meta_description,
                    "cited_attributes": list(draft.cited_attributes),
                },
                dict(product.attributes),
            )
            if current_violations:
                raise ConflictError(
                    "Draft no longer matches the product: " + ", ".join(current_violations)
                )
            current = await uow.live_listings.get(draft.sku, draft.store_id)
            if current is not None and current.published_draft_id == draft.id:
                if expected_version != current.version:
                    raise OptimisticConflictError(
                        "Live listing version does not match",
                        current_version=current.version,
                    )
                return current
            now = self._clock.now()
            saved = StoreListing(
                sku=draft.sku,
                store_id=draft.store_id,
                version=1 if current is None else current.version + 1,
                title=_published_text(draft.title),
                short_description=_published_text(draft.short_description),
                long_description=_published_text(draft.long_description),
                meta_title=_published_text(draft.meta_title),
                meta_description=_published_text(draft.meta_description),
                published_draft_id=draft.id,
                published_at=now,
            )
            if current is None:
                if expected_version != 0:
                    raise OptimisticConflictError(
                        "No live listing exists; expected_version must be 0",
                        current_version=0,
                    )
                await uow.live_listings.add(saved)
            else:
                if expected_version != current.version:
                    raise OptimisticConflictError(
                        "Live listing version does not match",
                        current_version=current.version,
                    )
                ok = await uow.live_listings.compare_and_set(
                    expected_version=current.version,
                    listing=saved,
                )
                if not ok:
                    latest = await uow.live_listings.get(draft.sku, draft.store_id)
                    raise OptimisticConflictError(
                        "Live listing version does not match",
                        current_version=latest.version if latest is not None else 0,
                    )
            # History records the stripped text just stored live, in the same transaction.
            await uow.listing_revisions.add(
                StoreListingRevision(
                    id=str(uuid4()),
                    sku=saved.sku,
                    store_id=saved.store_id,
                    version=saved.version,
                    published_draft_id=saved.published_draft_id,
                    title=saved.title,
                    short_description=saved.short_description,
                    long_description=saved.long_description,
                    meta_title=saved.meta_title,
                    meta_description=saved.meta_description,
                    published_at=saved.published_at,
                )
            )
            if draft.status != "published":
                await uow.drafts.set_status(draft.id, "published")
            await uow.commit()
        log.info(
            "listing_published sku=%s store=%s version=%s draft=%s",
            saved.sku,
            saved.store_id,
            saved.version,
            saved.published_draft_id,
        )
        return saved

    async def rollback(
        self,
        *,
        sku: str,
        store_id: str,
        revision_version: int,
        expected_version: int,
    ) -> StoreListing:
        """Republish the draft behind an earlier live version, through `publish`."""

        async with self._uow_factory() as uow:
            current = await uow.live_listings.get(sku, store_id)
            if current is None:
                raise NotFoundError(f"No live listing for {sku} at {store_id}")
            revision = await uow.listing_revisions.get(sku, store_id, revision_version)
            if revision is None:
                raise NotFoundError(
                    f"No revision {revision_version} for the {sku} listing at {store_id}"
                )
        if revision.version == current.version:
            raise ConflictError("That revision is already live")
        return await self.publish(revision.published_draft_id, expected_version)

    async def revisions(
        self, *, sku: str, store_id: str, limit: int, offset: int
    ) -> list[StoreListingRevision]:
        async with self._uow_factory() as uow:
            return await uow.listing_revisions.list(
                sku=sku,
                store_id=store_id,
                limit=limit,
                offset=offset,
            )

    async def get_live(self, sku: str, store_id: str) -> StoreListing:
        async with self._uow_factory() as uow:
            listing = await uow.live_listings.get(sku, store_id)
        if listing is None:
            raise NotFoundError(f"No live listing for {sku} at {store_id}")
        return listing

    async def list(
        self,
        *,
        sku: str | None,
        store_id: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[ListingDraft]:
        async with self._uow_factory() as uow:
            return await uow.drafts.list(
                sku=sku,
                store_id=store_id,
                status=status,
                limit=limit,
                offset=offset,
            )
