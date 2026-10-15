"""Files a person attached to a message, as the model is given them. Needs
``forge-common[adk]``.

forge-ui's chat (``useAdkAssistant``) sends an attachment as an inline part of
the message, its name in its MIME type: ``application/pdf; name="report.pdf"``.
ADK's web client keeps only a part's type and data, both ways, so that's how
the chat still names the file when the conversation loads again.
:class:`AttachmentsPlugin` goes on the ``App``::

    app = App(name="ca_x", root_agent=root, plugins=[AttachmentsPlugin()])

Before each model call it gives the model each such part with its bare type,
after a line of text naming the file. The conversation keeps the part as it
was sent: only the request's copy changes.
"""

from __future__ import annotations

from email.message import Message
from email.utils import collapse_rfc2231_value
from typing import TYPE_CHECKING

from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse

__all__ = ["AttachmentsPlugin", "attachment_name", "bare_type"]


def _parsed(mime_type: str) -> Message:
    message = Message()
    message["content-type"] = mime_type
    return message


def bare_type(mime_type: str) -> str:
    """:return: The type without its parameters, ``application/pdf``."""
    return mime_type.split(";", 1)[0].strip().lower()


def attachment_name(mime_type: str | None) -> str | None:
    """:return: The file's name its type carries (``name="report.pdf"``), if any."""
    if not mime_type or ";" not in mime_type:
        return None
    name = _parsed(mime_type).get_param("name")
    if name is None:
        return None
    text = collapse_rfc2231_value(name)
    return text.strip() or None


def _for_model(part: types.Part) -> list[types.Part]:
    """:return: The part as the model takes it: its bare type, after its name."""
    blob = part.inline_data
    if blob is None or not blob.mime_type or ";" not in blob.mime_type:
        return [part]
    name = attachment_name(blob.mime_type)
    # A new blob: the request's parts share theirs with the conversation's.
    bare = part.model_copy(
        update={"inline_data": blob.model_copy(update={"mime_type": bare_type(blob.mime_type)})}
    )
    return [types.Part(text=f"Attached file: {name}"), bare] if name else [bare]


class AttachmentsPlugin(BasePlugin):
    """Gives the model attachments with their bare types, each after its name."""

    def __init__(self, name: str = "forge_attachments") -> None:
        super().__init__(name)

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        contents = []
        for content in llm_request.contents:
            parts = content.parts or []
            if any(p.inline_data and ";" in (p.inline_data.mime_type or "") for p in parts):
                content = content.model_copy(
                    update={"parts": [made for p in parts for made in _for_model(p)]}
                )
            contents.append(content)
        llm_request.contents = contents
        return None
