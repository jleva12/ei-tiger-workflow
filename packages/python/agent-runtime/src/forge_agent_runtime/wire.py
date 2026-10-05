"""ADK's models as the JSON the web console's assistant reads.

The same camelCase JSON as ADK's own API server (``adk api_server``), which
``@assistant-ui/react-google-adk`` is written against, with two differences:

- Binary data (``inlineData``: images, files) is standard base64, which the
  browser's ``atob`` and data URLs read. genai writes the URL-safe alphabet,
  which they refuse, so pictures and downloads would break.
- The empty text parts Gemini 3 carries its thought signatures on are left
  out. ADK keeps them in the session, because the model needs them back on
  the next turn; the browser would show each as an empty paragraph after the
  reply.
"""

import json
from typing import Any

from pydantic import BaseModel

_URL_SAFE_TO_STANDARD = str.maketrans("-_", "+/")


def _standard_base64(node: Any) -> Any:
    """
    Transforms all URL-safe Base64 encoded data found within a nested data structure
    (e.g., dict, list) into standard Base64 encoded data by applying a translation
    from URL-safe to standard Base64 encoding.

    :param node: The nested data structure to be processed.
        It can be a dictionary, a list, or any object.
    :return: The transformed data structure with all URL-safe Base64 encoded data
        converted to standard Base64 encoded data.
    """
    if isinstance(node, dict):
        blob = node.get("inlineData")
        if isinstance(blob, dict) and isinstance(blob.get("data"), str):
            blob["data"] = blob["data"].translate(_URL_SAFE_TO_STANDARD)
        for value in node.values():
            _standard_base64(value)
    elif isinstance(node, list):
        for item in node:
            _standard_base64(item)
    return node


def _is_bare_signature(part: Any) -> bool:
    """
    Whether a part is only a thought signature: Gemini 3 sends one on an empty
    text part at the end of a reply.

    :param part: A part's JSON value.
    :return: True for an empty text part with nothing else but a signature.
    """
    return isinstance(part, dict) and part.get("text") == "" and set(part) <= {"text", "thoughtSignature"}


def _drop_bare_signatures(node: Any) -> Any:
    """
    Removes the parts that are only a thought signature from every ``parts``
    list in a nested data structure, unless they are all it holds, so an event
    never loses its content.

    :param node: The nested data structure to be processed.
    :return: The same structure without those parts.
    """
    if isinstance(node, dict):
        parts = node.get("parts")
        if isinstance(parts, list):
            kept = [part for part in parts if not _is_bare_signature(part)]
            if kept:
                node["parts"] = kept
        for value in node.values():
            _drop_bare_signatures(value)
    elif isinstance(node, list):
        for item in node:
            _drop_bare_signatures(item)
    return node


def to_wire(model: BaseModel) -> Any:
    """
    Converts a given Pydantic model into its serialized and encoded wire format.
    The function takes a `BaseModel` object, serializes it to a JSON-compatible
    dictionary with special handling for aliasing and excluding `None` values,
    encodes it using a standardized Base64 encoding, and leaves out the parts
    that are only a thought signature.

    :param model: A Pydantic BaseModel object to be serialized and encoded.
    :type model: BaseModel
    :return: The serialized and Base64-encoded representation of the model.
    :rtype: Any
    """
    return _drop_bare_signatures(
        _standard_base64(model.model_dump(mode="json", by_alias=True, exclude_none=True))
    )


def sse(model: BaseModel) -> str:
    """
    Convert a model instance to a Server-Sent Events (SSE) formatted string.

    This function takes a model instance, serializes it into a JSON representation
    following a wire format, and formats it into a string compliant with the Server-
    Sent Events protocol. The returned string is designed to be sent over an SSE
    connection, where the `data` field represents the JSON-encoded content of the
    model.

    :param model: An instance of the `BaseModel` class to be converted into an SSE
        formatted string.
    :type model: BaseModel
    :return: A string formatted according to the Server-Sent Events protocol,
        containing the serialized representation of `model` in the `data` field.
    :rtype: str
    """
    data = json.dumps(to_wire(model), ensure_ascii=False, separators=(",", ":"))
    return f"data: {data}\n\n"
