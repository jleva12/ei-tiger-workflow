"""What tool calls made together write to an intake's record or a
conversation's requests, kept whole.

ADK runs the calls of one model answer together, then keeps their writes to
the state as one event, deep-merging each call's: a key one call removed
(a field cleared from a record) comes back from another call's copy. Those
calls take turns changing a record (each reads what the one before wrote,
:func:`forge_agent_runtime.intake.latest`), so the session's state has the
last of them, whole: that's what's kept.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

from google.adk.plugins.base_plugin import BasePlugin

from forge_agent_runtime.intake import STATE_PREFIX
from forge_agent_runtime.requests import STATE_KEY

if TYPE_CHECKING:
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.events.event import Event

#: The state keys kept whole: intakes' records, and requests (and their snapshot).
WHOLE = (STATE_PREFIX, STATE_KEY)


class WholeStatePlugin(BasePlugin):
    """Keeps the records tool calls made together wrote whole, as the session has them last."""

    def __init__(self, prefixes: tuple[str, ...] = WHOLE) -> None:
        super().__init__(name="forge_whole_state")
        self.prefixes = prefixes

    async def on_event_callback(self, *, invocation_context: InvocationContext, event: Event) -> Event | None:
        delta = event.actions.state_delta if event.actions else None
        if not delta or not event.get_function_responses():
            return None
        state = invocation_context.session.state
        for key in list(delta):
            if key.startswith(self.prefixes) and key in state:
                delta[key] = copy.deepcopy(state[key])
        return None
