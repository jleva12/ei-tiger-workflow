"""Files attached in the chat reach the model with their bare types, each
after a line naming it; the conversation keeps them as they were sent."""

from typing import Any

from google.adk.models.llm_request import LlmRequest
from google.genai import types

from forge_common.adk.attachments import AttachmentsPlugin, attachment_name, bare_type

PDF = b"%PDF-1.4 test"


def blob(mime_type: str) -> types.Part:
    return types.Part(inline_data=types.Blob(mime_type=mime_type, data=PDF))


async def before_model(contents: list[types.Content]) -> LlmRequest:
    request = LlmRequest(contents=contents)
    plugin = AttachmentsPlugin()
    callback: Any = None
    assert (
        await plugin.before_model_callback(callback_context=callback, llm_request=request) is None
    )
    return request


def test_a_type_carries_the_files_name() -> None:
    assert attachment_name('application/pdf; name="report.pdf"') == "report.pdf"
    assert (
        attachment_name('application/pdf; name="Q3 \\"final\\" plan.docx"')
        == 'Q3 "final" plan.docx'
    )
    assert attachment_name('application/pdf; name="résumé.pdf"') == "résumé.pdf"
    assert attachment_name("application/pdf") is None
    assert attachment_name("text/plain; charset=utf-8") is None
    assert attachment_name(None) is None
    assert bare_type('Application/PDF; name="a.pdf"') == "application/pdf"


async def test_the_model_gets_the_bare_type_after_the_name() -> None:
    sent = blob('application/pdf; name="report.pdf"')
    message = types.Content(role="user", parts=[types.Part(text="Summarize it"), sent])
    request = await before_model([message])

    [content] = request.contents
    texts = [part.text for part in content.parts or []]
    assert texts == ["Summarize it", "Attached file: report.pdf", None]
    file = (content.parts or [])[-1].inline_data
    assert file is not None
    assert (file.mime_type, file.data) == ("application/pdf", PDF)
    # The conversation's message is as it was sent.
    assert message.parts == [types.Part(text="Summarize it"), sent]
    assert sent.inline_data is not None
    assert sent.inline_data.mime_type == 'application/pdf; name="report.pdf"'


async def test_parts_without_a_name_are_left_alone() -> None:
    plain = types.Content(role="user", parts=[blob("image/png"), types.Part(text="hi")])
    other = types.Content(role="user", parts=[blob("text/plain; charset=utf-8")])
    request = await before_model([plain, other])

    assert request.contents[0] is plain
    [kept] = request.contents[1].parts or []
    assert kept.inline_data is not None
    assert kept.inline_data.mime_type == "text/plain"
