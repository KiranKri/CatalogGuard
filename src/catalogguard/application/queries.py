"""Read models that are not owned by a write use case."""

from catalogguard.application.ports import UnitOfWorkFactory
from catalogguard.domain.models import DeadLetter


class DeadLetterService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def list(self, *, limit: int, offset: int) -> list[DeadLetter]:
        async with self._uow_factory() as uow:
            return await uow.dead_letters.list(limit=limit, offset=offset)
