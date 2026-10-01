from __future__ import annotations

from typing import Protocol

from event_bus.core.event import Event


class Middleware(Protocol):
    """
    Provides a protocol for implementing middleware to process events in a
    publish-subscribe system.

    This protocol defines the structure for middleware that can intercept
    events before they are published and execute custom logic after an event
    has been successfully published. Middleware implementations can leverage
    this structure to transform, validate, or monitor events and their
    publishing process.
    """

    async def before_publish(self, event: Event) -> Event:
        """
        Handles preprocessing of an event before publishing it.

        This method is invoked to perform any necessary modifications or checks
        on the provided event before it is published. It allows injecting
        custom logic to augment or validate the event object, ensuring it meets
        required criteria for successful handling downstream.

        :param event: The event instance to process before publishing.
                      Must be of type `Event`.
        :return: The modified or validated event, ready for publishing.
        :rtype: Event
        """
        ...

    async def after_publish(self, event: Event, stream_id: str) -> None:
        """
        This asynchronous method is executed after a publish operation is performed.
        It takes an event object and a stream identifier as parameters and performs
        necessary operations post-publishing.

        :param event: The event object containing the data relevant to the publishing operation.
        :type event: Event
        :param stream_id: A unique identifier for the stream involved in the publish operation.
        :return: This method does not return any value.
        :rtype: None
        """
        ...
