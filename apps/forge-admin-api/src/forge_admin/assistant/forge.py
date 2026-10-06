"""The Forge assistant: the agents behind the web console's assistant.

A supervisor talks with the person. It has no tools of its own: it hands
each request to a specialist, an agent with one toolset (the access tools in
``access_tools.py``, the administration tools in ``admin_tools.py``), and
answers from what the specialist reports. With a few tools
each, the specialists pick the right one more reliably than one agent
offered all of them. Each specialist runs as an ADK
task: it may ask the person a question, and its changes ask them to
confirm, before it reports back.

Which specialists the supervisor has, and which of its tools each has,
depend on the screen the person is on (``screens.py``, configured in
``screens.yaml``). Every tool acts as the person. The agents know who the
person is (``person.py``) and, while they share it, the page they're on as
they send each message (``page_context.py``).
"""

from collections.abc import Callable, Mapping
from pathlib import Path

from fastapi import FastAPI
from forge_common.adk.models import ProviderModels
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps import App
from google.adk.models import BaseLlm
from google.adk.models.llm_request import LlmRequest
from pydantic import Field

from forge_admin.assistant import (
    access_tools,
    admin_tools,
    knowledge_tools,
    language_models,
    screens,
)
from forge_admin.assistant.page_context import describe_page
from forge_admin.assistant.person import PERSON, describe_person
from forge_admin.assistant.screens import AssistantToolset, Screens
from forge_admin.config import Settings

# The ADK app name, which the web console connects to (VITE_ADK_APP), and the
# supervisor's name, which it shows as the one answering.
APP_NAME = "forge"

# What Forge is; every agent's instruction starts with it.
FORGE = """\
Forge is a workspace where organizations build Google ADK workflows and run \
them, and design Google ADK agents to chat with. An ADK workflow's steps call \
models and other systems, and a person approves the steps that need one.

What Forge is made of:
- A hierarchy: the site, then organizations. The work happens in \
organizations: their ADK workflows and their runs, their agents, and their \
knowledge bases, named sets of documents their agents search.
- Role-based access: a role is assigned in a scope, the site or an \
organization; a site role applies in every organization too. Permissions are \
keyed resource:action.

How you write to the person:
- Plainly and briefly, in the second person and short sentences. Use Forge's \
words: organization, scope, role, permission, member, ADK workflow, agent, \
step, run, approval, knowledge base, document.
- In Markdown: short paragraphs, lists, and fenced code blocks that name their \
language.
- Never invent records, people, numbers or links.
- Say what a document says in its own terms: its parties, names and roles \
("the husband must...", "the vendor shall..."), never as if it were about \
the person you're talking to, even when you answer them in the second person.
"""

# The supervisor's part: `supervisor_instruction` adds the screen and context.
INSTRUCTION = f"""\
You are the Forge assistant, built into Forge. {FORGE}
How you work:
- You have no tools of your own; your specialists do. Each is an agent with \
the tools of one part of Forge, acting as the person: it sees and changes \
exactly what they can. Which specialists you have depends on the page \
they're on (below).
- Hand every request that needs Forge's records to the specialist whose part \
it is. It sees neither this \
conversation nor these instructions, only who the person is and their page, \
so your request must stand on its own: what the person wants, in their words \
where the wording matters, with every ID, name and earlier result it needs.
- One specialist at a time. When a request spans parts, hand it on in turn \
and pass along what the earlier one found: for example, find a person's user \
record with one, then give them a role in an organization with another.
- A specialist asks the person itself when it has to: a question, or their \
approval before a change. Don't ask them to approve it again.
- Answer from what the specialist reports; it's what happened. Say something \
changed only when it says so; when it couldn't do something, say why and what \
the person can do. Keep its links and IDs exactly as it wrote them, next to \
the facts they support, and its citations too: the refs in square brackets \
right after each fact ("... within 180 days [KQM4821]."), which become \
numbered sources.
- Answer on your own only about Forge in general, and about what you and \
your specialists can do. For what none of them reaches, say so and tell the \
person where in Forge to look.
"""

# A specialist's part, around its toolset's instruction.
SPECIALIST_INSTRUCTION = """\
You are one of the Forge assistant's specialists, built into Forge. {forge}
Your part: {part}

How you work:
- The Forge assistant hands you one request from the person. Do it with your \
tools, which act as the person: they read and change only what the person \
can. Changes ask the person to confirm first.
- When it's done, or can't be, call finish_task with your report for the \
assistant: what you found or did, with the IDs, names, links and numbers it \
needs to answer; or what stopped you and what the person can \
do. Report only what your tools returned.
- When the request is unclear, or needs a choice only the person can make, \
ask them one short question and wait for their answer.
- When the person asks for something your tools don't reach, call \
finish_task saying what they asked, so the assistant can hand it on.
"""

# How to use who the person is and their page, which follow it.
CONTEXT_INSTRUCTION = """
The person and their page:
- Below is who you're talking to, from their sign-in, and, while they share \
it, the page they're on as they send each message.
- When they say this, here, it or the current one, they mean what's in \
focus, or else the innermost record on screen, by its ID: an \
organization's for its members, ADK workflows, agents and knowledge bases; \
a knowledge base's on its page, and the knowledge_document open in its \
viewer; a user's, role's or permission's on its administration page.
- The page can change between messages: earlier messages may be about an \
earlier page.
- The page says where they are, not what they may do: every tool still acts \
only as them.
"""


def model_selection(
    models: ProviderModels, screens: Screens | None = None
) -> Callable[[CallbackContext, LlmRequest], None]:
    """
    A ``before_model_callback`` that runs the turn on the conversation's model
    and thinking level, which the web console's model section sends as the
    session state ``model`` and ``thinking_level``, or else the screen's
    defaults (``ProviderModels.select``). A model it may not run on runs on
    the default instead; a level the model doesn't offer, on the nearest it
    does.

    :param models: The models the agents run on (``language_models``).
    :param screens: The screens, whose defaults apply when the conversation
        hasn't chosen.
    :return: The callback.
    """

    def select(context: CallbackContext, request: LlmRequest) -> None:
        state = context.state.to_dict()
        screen = screens.resolve_state(state) if screens else None
        model = state.get("model") or (screen.model if screen else None)
        level = state.get("thinking_level") or (
            screen.thinking_level if screen else None
        )
        models.select(request, model, level)

    return select


def offer_specialists(
    screens: Screens,
) -> Callable[[CallbackContext, LlmRequest], None]:
    """
    The supervisor's ``before_model_callback`` that offers it only the
    specialists the person's screen gives, or the conversation had before;
    ADK offers every sub-agent. It also has the conversation keep its
    screen's toolsets (``Screens.remember``): state written here goes with
    the model's reply, rather than in an event of its own.

    A specialist that isn't offered still runs if the model names it, but
    with no tools (``ScreenToolset``).

    :param screens: The screens.
    :return: The callback.
    """

    def offer(context: CallbackContext, request: LlmRequest) -> None:
        screens.remember(context)
        active = screens.active(context.state.to_dict())
        withheld = {name for name in screens.toolsets if name not in active}
        for tool in list(request.config.tools or []):
            declarations = getattr(tool, "function_declarations", None)
            if not declarations:
                continue
            kept = [d for d in declarations if d.name not in withheld]
            if kept:
                tool.function_declarations = kept
            else:
                request.config.tools.remove(tool)  # type: ignore[union-attr]
        for name in withheld:
            request.tools_dict.pop(name, None)

    return offer


def supervisor_instruction(screens: Screens) -> Callable[[ReadonlyContext], str]:
    """
    The supervisor's instruction, per model call: how to answer and hand
    requests on; which specialists other screens have; the screen's own
    sections; and who the person is and the page they're on, from the
    session state (``temp:person`` from their sign-in, ``page_context`` from
    their browser). The specialists it has here are its tools, each
    described by its declaration.

    ADK doesn't fill ``{name}`` placeholders in what an instruction provider
    returns, so names and labels may hold braces.

    :param screens: The screens and their toolsets.
    :return: The instruction provider.
    """

    def provide(context: ReadonlyContext) -> str:
        state = context.state
        parts = [INSTRUCTION]
        elsewhere = _elsewhere(screens, screens.active(state))
        if elsewhere:
            parts.append(elsewhere)
        return _with_context(parts, screens, state)

    return provide


def specialist_instruction(
    screens: Screens, entry: AssistantToolset
) -> Callable[[ReadonlyContext], str]:
    """
    A specialist's instruction, per model call: how to do its part and report
    back; how to use its toolset; where the rest of its tools are, when the
    screen gives only some; the screen's own sections; and the person and
    their page.

    :param screens: The screens and their toolsets.
    :param entry: Its toolset.
    :return: The instruction provider.
    """
    head = SPECIALIST_INSTRUCTION.format(
        forge=FORGE, part=entry.delegate or entry.description
    )

    def provide(context: ReadonlyContext) -> str:
        state = context.state
        parts = [head]
        if entry.instruction:
            parts.append(entry.instruction)
        active = screens.active(state)
        if active.get(entry.name, frozenset()) is not None:
            pages = _pages_with(screens, entry.name)
            parts.append(
                "\nOn this page you have only some of your tools. For what they "
                "don't reach, call finish_task saying which page has the rest"
                + (f": the {', '.join(pages)} pages.\n" if pages else ".\n")
            )
        return _with_context(parts, screens, state)

    return provide


def _with_context(
    parts: list[str], screens: Screens, state: Mapping[str, object]
) -> str:
    """An instruction's parts, then the screen's sections, the person and the page."""
    parts = [
        *parts,
        *(f"\n{text}\n" for text in screens.resolve_state(state).instructions),
    ]
    guidance = "".join(parts)
    sections = [describe_person(state.get(PERSON)), describe_page(state)]
    known = [section for section in sections if section]
    if not known:
        return guidance
    return "\n".join([guidance + CONTEXT_INSTRUCTION, *known])


def _pages_with(screens: Screens, name: str) -> list[str]:
    """The screens that give toolset ``name``, by title."""
    return [
        screen.title
        for screen in screens.config.screens
        if any(ref.name == name for ref in screen.toolsets)
    ]


def _elsewhere(screens: Screens, active: Mapping[str, object]) -> str | None:
    """The specialists other screens have, so the supervisor can say where to go."""
    lines = []
    for entry in screens.available():
        if entry.name in active:
            continue
        titles = _pages_with(screens, entry.name)
        if titles:
            lines.append(f"- {entry.title}: on the {', '.join(titles)} pages.")
    if not lines:
        return None
    return (
        "\nSpecialists you don't have on this page (when a request needs one, "
        "tell the person which page has it):\n" + "\n".join(lines) + "\n"
    )


class ForgeAgent(LlmAgent):
    """The supervisor, with the screens it and its specialists follow."""

    screens: Screens | None = Field(default=None, exclude=True)


def supervisor(screens: Screens, models: ProviderModels) -> ForgeAgent:
    """
    The agent the person talks with, and a specialist for every toolset of
    ``screens`` that's set up.

    :param screens: The screens, which say which specialists it's offered and
        which tools each has on each page.
    :param models: The models they run on.
    """
    return ForgeAgent(
        name=APP_NAME,
        description="Answers questions about Forge and the work in it.",
        model=models,
        instruction=supervisor_instruction(screens),
        sub_agents=[
            specialist(entry, screens, models) for entry in screens.available()
        ],
        before_model_callback=[
            model_selection(models, screens),
            offer_specialists(screens),
        ],
        screens=screens,
    )


def specialist(
    entry: AssistantToolset, screens: Screens, models: ProviderModels
) -> LlmAgent:
    """
    The agent that holds toolset ``entry``: an ADK task, which the supervisor
    hands a request and which reports back with ``finish_task``. It never
    hands the conversation on itself.

    :param entry: Its toolset, which must be set up.
    :param screens: The screens, which give it its tools on each page.
    :param models: The models it runs on, as the supervisor does.
    """
    return LlmAgent(
        name=entry.name,
        description=entry.delegate or entry.description,
        model=models,
        mode="task",
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
        instruction=specialist_instruction(screens, entry),
        tools=[screens.toolset(entry.name)],
        before_model_callback=model_selection(models, screens),
    )


def toolsets(settings: Settings, app: FastAPI | None = None) -> list[AssistantToolset]:
    """
    Every toolset the screens can name, each None where it isn't set up.

    :param settings: The application settings.
    :param app: The admin API, whose routes the tools call as the person;
        without it they aren't set up.
    """
    return [
        AssistantToolset(
            name="access",
            title="Access and people",
            description=(
                "Explains what you can do and why something was refused, shows who "
                "holds which role on the site or in an organization, and gives "
                "or removes roles where you may. Changes ask you first."
            ),
            toolset=access_tools.toolset(app) if app is not None else None,
            instruction=access_tools.INSTRUCTION,
            tools=access_tools.TOOL_NAMES,
            delegate=(
                "Access and people: the person's profile, organizations and memberships, "
                "what they may do and why something was refused; who holds which "
                "role on the site or in an organization, the roles "
                "available, finding people, and giving or removing roles."
            ),
        ),
        AssistantToolset(
            name="administration",
            title="Administration",
            description=(
                "Reads and edits organizations, the roles and "
                "permissions Forge grants, and its users, as you. It never deletes; "
                "changes ask you first."
            ),
            toolset=admin_tools.toolset(app) if app is not None else None,
            instruction=admin_tools.INSTRUCTION,
            tools=admin_tools.TOOL_NAMES,
            delegate=(
                "Administration: organizations, the roles "
                "and permissions Forge grants, and its users; reads, creates and "
                "edits them, never deletes."
            ),
        ),
        AssistantToolset(
            name="knowledge",
            title="Knowledge bases",
            description=(
                "Lists an organization's knowledge bases and their documents, says "
                "how each document's ingestion went, answers questions from the "
                "documents with cited passages, and retries or re-indexes "
                "ingestion where you may. Changes ask you first."
            ),
            toolset=knowledge_tools.toolset(app) if app is not None else None,
            instruction=knowledge_tools.INSTRUCTION,
            tools=knowledge_tools.TOOL_NAMES,
            delegate=(
                "Knowledge bases: an organization's knowledge bases and their "
                "documents; answering questions from what the documents say, with "
                "the passages cited by ref; which documents are searchable, failed "
                "or stale, and retrying or re-indexing them."
            ),
        ),
    ]


def create_app(
    settings: Settings,
    model: BaseLlm | None = None,
    *,
    app: FastAPI | None = None,
    screens_file: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> App:
    """
    The Forge assistant as an ADK app.

    :param settings: The application settings.
    :param model: The model every turn runs on, whatever the conversation
        chooses; tests pass a scripted one. By default each turn runs on the
        model it chose (``language_models``).
    :param app: The admin API, for the tools that call it as the person.
    :param screens_file: The screen configuration; ``agent_screens`` from the
        settings, or the bundled ``screens.yaml``.
    :param environment: What the model provider configuration's ``${NAME}``
        references resolve from; the process environment over .env by default.
    :return: The app, named ``APP_NAME``: the supervisor, with a specialist
        for every toolset that's set up.
    :raises ValueError: The screen configuration or the model provider
        configuration is invalid (``ModelProviderConfigError`` is a
        ``ValueError``).
    """
    models = language_models.provider_models(
        settings,
        environment=environment,
        build=(lambda call: model) if model is not None else None,
    )
    on_screens = screens.load(
        toolsets(settings, app),
        screens_file or settings.agent_screens,
        models=language_models.accepted_names(models),
    )
    return App(name=APP_NAME, root_agent=supervisor(on_screens, models))
