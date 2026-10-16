"""Kafka EventBus skeleton. Not wired. See adr/0001-kafka-event-bus.md.

The production swap keeps `publish` writing the outbox inside the caller's
database transaction. A separate publisher process claims rows and produces
to `catalogguard.events` with key `{sku}|{store_id}`. Consumers replace
`drain`. This class documents that shape and fails loudly if constructed
and called, so a half-wired broker cannot look healthy.
"""

from catalogguard.application.ports import OutboxRepository
from catalogguard.domain.events import DomainEvent


class KafkaEventBus:
    def __init__(self, *, bootstrap_servers: str, topic: str = "catalogguard.events") -> None:
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic

    async def publish(self, event: DomainEvent, outbox: OutboxRepository) -> None:
        del event, outbox
        raise NotImplementedError(
            "KafkaEventBus is a skeleton. Write the event to event_outbox in the "
            "state-change transaction, then publish from an outbox worker. "
            f"Topic {self.topic}, brokers {self.bootstrap_servers or '(unset)'}. "
            "See adr/0001-kafka-event-bus.md."
        )

    async def drain(self) -> None:
        raise NotImplementedError(
            "Kafka consumers replace in-process drain. See adr/0001-kafka-event-bus.md."
        )

    def subscribe(self, event_type: str, handler: object) -> None:
        del event_type, handler
        raise NotImplementedError(
            "Register handlers on the consumer, not on this skeleton. "
            "See adr/0001-kafka-event-bus.md."
        )
