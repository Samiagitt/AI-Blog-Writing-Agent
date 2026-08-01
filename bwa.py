from __future__ import annotations

import operator
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, List, Literal, Optional, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field


load_dotenv()


# ============================================================
# Blog Writer
#
# Router
#   -> Research when needed
#   -> Orchestrator
#   -> Parallel Workers
#   -> Reducer
#   -> Clean Markdown
#   -> Save final blog
# ============================================================


# ============================================================
# 1. Schemas
# ============================================================
class Task(BaseModel):
    id: int
    title: str

    goal: str = Field(
        ...,
        description=(
            "One sentence describing what the reader "
            "should understand or be able to do."
        ),
    )

    bullets: List[str] = Field(
        ...,
        min_length=3,
        max_length=6,
    )

    target_words: int = Field(
        ...,
        description="Target word count from 120 to 550 words.",
    )

    tags: List[str] = Field(default_factory=list)
    requires_research: bool = False
    requires_citations: bool = False
    requires_code: bool = False


class Plan(BaseModel):
    blog_title: str
    audience: str
    tone: str

    blog_kind: Literal[
        "explainer",
        "tutorial",
        "news_roundup",
        "comparison",
        "system_design",
    ] = "explainer"

    constraints: List[str] = Field(default_factory=list)
    tasks: List[Task]


class EvidenceItem(BaseModel):
    title: str
    url: str
    published_at: Optional[str] = None
    snippet: Optional[str] = None
    source: Optional[str] = None


class RouterDecision(BaseModel):
    needs_research: bool

    mode: Literal[
        "closed_book",
        "hybrid",
        "open_book",
    ]

    reason: str
    queries: List[str] = Field(default_factory=list)
    max_results_per_query: int = Field(default=5)


class EvidencePack(BaseModel):
    evidence: List[EvidenceItem] = Field(
        default_factory=list
    )


class State(TypedDict):
    topic: str

    # Routing and research
    mode: str
    needs_research: bool
    queries: List[str]
    evidence: List[EvidenceItem]
    plan: Optional[Plan]

    # Recency
    as_of: str
    recency_days: int

    # Parallel worker results
    sections: Annotated[
        List[tuple[int, str]],
        operator.add,
    ]

    # Final blog
    merged_md: str
    final: str


# ============================================================
# 2. Language model
# ============================================================
llm = ChatOpenAI(
    model="gpt-4.1-mini",
    temperature=0.3,
)


# ============================================================
# 3. Router
# ============================================================
ROUTER_SYSTEM = """
You are a routing module for a technical blog-writing system.

Decide whether web research is needed before creating the blog plan.

Modes:

1. closed_book
   - Use for stable and evergreen concepts.
   - Set needs_research=false.

2. hybrid
   - Use for mostly evergreen concepts that benefit from current
     examples, tools, products, models, or industry practices.
   - Set needs_research=true.

3. open_book
   - Use for current news, weekly roundups, recent releases,
     current pricing, policies, or other volatile information.
   - Set needs_research=true.

When research is needed:

- Return between 3 and 10 focused search queries.
- Keep each query closely related to the requested topic.
- For a weekly roundup, make the queries focus on the last 7 days.
"""


def router_node(state: State) -> dict:
    decider = llm.with_structured_output(
        RouterDecision
    )

    decision = decider.invoke(
        [
            SystemMessage(content=ROUTER_SYSTEM),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n"
                    f"As-of date: {state['as_of']}"
                )
            ),
        ]
    )

    if decision.mode == "open_book":
        recency_days = 7

    elif decision.mode == "hybrid":
        recency_days = 45

    else:
        recency_days = 3650

    return {
        "needs_research": decision.needs_research,
        "mode": decision.mode,
        "queries": decision.queries,
        "recency_days": recency_days,
    }


def route_next(state: State) -> str:
    if state["needs_research"]:
        return "research"

    return "orchestrator"


# ============================================================
# 4. Research using Tavily
# ============================================================
def _tavily_search(
    query: str,
    max_results: int = 5,
) -> List[dict]:
    """
    Search Tavily and return normalized search results.
    """
    if not os.getenv("TAVILY_API_KEY"):
        return []

    try:
        from langchain_community.tools.tavily_search import (
            TavilySearchResults,
        )

        tool = TavilySearchResults(
            max_results=max_results
        )

        results = tool.invoke(
            {"query": query}
        )

        normalized_results: List[dict] = []

        for result in results or []:
            normalized_results.append(
                {
                    "title": (
                        result.get("title")
                        or ""
                    ),
                    "url": (
                        result.get("url")
                        or ""
                    ),
                    "snippet": (
                        result.get("content")
                        or result.get("snippet")
                        or ""
                    ),
                    "published_at": (
                        result.get("published_date")
                        or result.get("published_at")
                    ),
                    "source": result.get("source"),
                }
            )

        return normalized_results

    except Exception as error:
        print(
            f"Tavily search failed for query "
            f"'{query}': {error}"
        )

        return []


def _iso_to_date(
    value: Optional[str],
) -> Optional[date]:
    """
    Convert an ISO date string into a date object.
    """
    if not value:
        return None

    try:
        return date.fromisoformat(
            value[:10]
        )

    except Exception:
        return None


RESEARCH_SYSTEM = """
You are a research synthesizer for a technical blog.

Convert the supplied raw search results into EvidenceItem objects.

Rules:

- Include only items with a non-empty URL.
- Prefer relevant and authoritative sources.
- Keep snippets short and factual.
- Deduplicate results by URL.
- Use YYYY-MM-DD for published_at only when the date is clear.
- If a publication date cannot be verified, use null.
- Never invent a title, URL, source, date, or claim.
"""


def research_node(state: State) -> dict:
    queries = (
        state.get("queries")
        or []
    )[:10]

    raw_results: List[dict] = []

    for query in queries:
        raw_results.extend(
            _tavily_search(
                query,
                max_results=6,
            )
        )

    if not raw_results:
        return {
            "evidence": [],
        }

    extractor = llm.with_structured_output(
        EvidencePack
    )

    evidence_pack = extractor.invoke(
        [
            SystemMessage(content=RESEARCH_SYSTEM),
            HumanMessage(
                content=(
                    f"As-of date: {state['as_of']}\n"
                    f"Recency window: "
                    f"{state['recency_days']} days\n\n"
                    f"Raw search results:\n"
                    f"{raw_results}"
                )
            ),
        ]
    )

    evidence_by_url: dict[
        str,
        EvidenceItem,
    ] = {}

    for evidence_item in evidence_pack.evidence:
        if evidence_item.url:
            evidence_by_url[
                evidence_item.url
            ] = evidence_item

    evidence = list(
        evidence_by_url.values()
    )

    if state.get("mode") == "open_book":
        as_of_date = date.fromisoformat(
            state["as_of"]
        )

        cutoff_date = (
            as_of_date
            - timedelta(
                days=int(
                    state["recency_days"]
                )
            )
        )

        evidence = [
            item
            for item in evidence
            if (
                publication_date
                := _iso_to_date(
                    item.published_at
                )
            )
            and publication_date >= cutoff_date
        ]

    return {
        "evidence": evidence,
    }


# ============================================================
# 5. Orchestrator
# ============================================================
ORCHESTRATOR_SYSTEM = """
You are a senior technical writer and developer advocate.

Create a highly actionable plan for a polished technical blog post.

Requirements:

- Create between 5 and 9 sections.
- Each section must have:
  - A clear title.
  - One goal.
  - Between 3 and 6 bullets.
  - A target word count between 120 and 550 words.
- Use tags only when useful.
- Mark tasks that require research, citations, or code.
- Organize the sections in a natural reading order.
- Avoid repeated sections or repeated ideas.
- The final post should feel cohesive, practical, and ready to publish.

Grounding rules:

- closed_book:
  Write an evergreen plan without depending on external evidence.

- hybrid:
  Use evidence for recent examples or external factual claims.
  Mark those sections as requires_research=true and
  requires_citations=true.

- open_book:
  Use blog_kind="news_roundup".
  Focus on current events, releases, changes, and implications.
  Do not invent events when evidence is weak.

Return only data matching the Plan schema.
"""


def orchestrator_node(
    state: State,
) -> dict:
    planner = llm.with_structured_output(
        Plan
    )

    mode = state.get(
        "mode",
        "closed_book",
    )

    evidence = (
        state.get("evidence")
        or []
    )

    force_news_roundup = (
        mode == "open_book"
    )

    plan = planner.invoke(
        [
            SystemMessage(
                content=ORCHESTRATOR_SYSTEM
            ),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n"
                    f"Mode: {mode}\n"
                    f"As-of date: {state['as_of']}\n"
                    f"Recency window: "
                    f"{state['recency_days']} days\n"
                    f"Force news_roundup: "
                    f"{force_news_roundup}\n\n"
                    f"Available evidence:\n"
                    f"{[
                        item.model_dump()
                        for item in evidence[:16]
                    ]}"
                )
            ),
        ]
    )

    if force_news_roundup:
        plan.blog_kind = "news_roundup"

    return {
        "plan": plan,
    }


# ============================================================
# 6. Fan-out
# ============================================================
def fanout(
    state: State,
) -> List[Send]:
    """
    Send each planned section to a parallel worker.
    """
    plan = state["plan"]

    if plan is None:
        raise ValueError(
            "fanout called without a plan."
        )

    return [
        Send(
            "worker",
            {
                "task": task.model_dump(),
                "topic": state["topic"],
                "mode": state["mode"],
                "as_of": state["as_of"],
                "recency_days": (
                    state["recency_days"]
                ),
                "plan": plan.model_dump(),
                "evidence": [
                    item.model_dump()
                    for item in state.get(
                        "evidence",
                        [],
                    )
                ],
            },
        )
        for task in plan.tasks
    ]


# ============================================================
# 7. Worker
# ============================================================
WORKER_SYSTEM = """
You are a senior technical writer and developer advocate.

Write exactly one section of a polished technical blog post
in Markdown.

Formatting rules:

- Start with: ## <Section Title>
- Cover all supplied bullets in their given order.
- Stay within approximately 15 percent of the target word count.
- Use clear paragraphs, lists, examples, and code when appropriate.
- Output only the finished section.
- Do not include planning notes, internal messages, image prompts,
  placeholders, system instructions, API errors, or explanations
  about how the section was generated.
- Never write phrases such as:
  "Not found in provided sources."
  "IMAGE GENERATION FAILED."
  "As an AI model."
  "Here is the section."

Scope rules:

- For a news roundup, focus on events and implications.
- Do not turn a news roundup into a tutorial unless requested.

Research rules:

- In open_book mode, every specific current claim about a company,
  product, model, release, funding event, regulation, or policy
  must be supported by one of the supplied evidence URLs.
- When citations are required, use natural Markdown links.
- If a claim cannot be supported, omit it instead of mentioning
  missing evidence.
- Never invent a URL, source, date, event, or statistic.

Code rules:

- If requires_code=true, include at least one small,
  useful code example.
- Use fenced Markdown code blocks.
"""


def worker_node(
    payload: dict,
) -> dict:
    task = Task(
        **payload["task"]
    )

    plan = Plan(
        **payload["plan"]
    )

    evidence = [
        EvidenceItem(**item)
        for item in payload.get(
            "evidence",
            [],
        )
    ]

    bullets_text = (
        "\n- "
        + "\n- ".join(task.bullets)
    )

    evidence_text = "\n".join(
        (
            f"- {item.title} | "
            f"{item.url} | "
            f"{item.published_at or 'date unknown'}"
        )
        for item in evidence[:20]
    )

    response = llm.invoke(
        [
            SystemMessage(
                content=WORKER_SYSTEM
            ),
            HumanMessage(
                content=(
                    f"Blog title: {plan.blog_title}\n"
                    f"Audience: {plan.audience}\n"
                    f"Tone: {plan.tone}\n"
                    f"Blog kind: {plan.blog_kind}\n"
                    f"Blog constraints: "
                    f"{plan.constraints}\n"
                    f"Topic: {payload['topic']}\n"
                    f"Mode: {payload.get('mode')}\n"
                    f"As-of date: "
                    f"{payload.get('as_of')}\n"
                    f"Recency window: "
                    f"{payload.get('recency_days')} days\n\n"
                    f"Section title: {task.title}\n"
                    f"Section goal: {task.goal}\n"
                    f"Target words: "
                    f"{task.target_words}\n"
                    f"Tags: {task.tags}\n"
                    f"Requires research: "
                    f"{task.requires_research}\n"
                    f"Requires citations: "
                    f"{task.requires_citations}\n"
                    f"Requires code: "
                    f"{task.requires_code}\n"
                    f"Required points:"
                    f"{bullets_text}\n\n"
                    f"Approved evidence URLs:\n"
                    f"{evidence_text or 'No evidence supplied.'}"
                )
            ),
        ]
    )

    section_markdown = str(
        response.content
    ).strip()

    return {
        "sections": [
            (
                task.id,
                section_markdown,
            )
        ],
    }


# ============================================================
# 8. Markdown cleanup
# ============================================================
def clean_blog_markdown(
    markdown: str,
) -> str:
    """
    Remove internal messages and old image-generation errors
    from the final blog.

    This also cleans previously generated Markdown files.
    """
    if not markdown:
        return ""

    text = (
        markdown
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )

    # Remove hidden control characters.
    text = re.sub(
        r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]",
        "",
        text,
    )

    cleaned_lines: List[str] = []

    skipping_image_error = False
    saw_error_line = False

    for line in text.splitlines():
        normalized = re.sub(
            r"^\s*>\s?",
            "",
            line,
        ).strip()

        normalized_without_bold = (
            normalized
            .replace("**", "")
            .strip()
        )

        # Start removing an old image failure block.
        if (
            "[IMAGE GENERATION FAILED]"
            in normalized_without_bold
        ):
            skipping_image_error = True
            saw_error_line = False
            continue

        if skipping_image_error:
            # If the next section begins before a blank line,
            # stop skipping and keep the heading.
            if re.match(
                r"^\s*#{1,6}\s+\S",
                line,
            ):
                skipping_image_error = False
                saw_error_line = False
                cleaned_lines.append(line)
                continue

            if normalized_without_bold.startswith(
                "Error:"
            ):
                saw_error_line = True
                continue

            if (
                saw_error_line
                and not normalized_without_bold
            ):
                skipping_image_error = False
                saw_error_line = False
                continue

            continue

        # Remove unused image placeholders.
        if re.fullmatch(
            r"\[\[IMAGE_\d+\]\]",
            normalized_without_bold,
        ):
            continue

        # Remove exact internal fallback messages.
        if normalized_without_bold.lower() in {
            "not found in provided sources.",
            "not found in provided sources",
            "image generation failed",
        }:
            continue

        # Remove any leftover Gemini quota-error line.
        if any(
            internal_text in line
            for internal_text in (
                "RESOURCE_EXHAUSTED",
                "generativelanguage.googleapis.com",
                "gemini-2.5-flash-preview-image",
                "gemini-2.5-flash-image",
            )
        ):
            continue

        cleaned_lines.append(line)

    cleaned_text = "\n".join(
        cleaned_lines
    )

    # Remove remaining inline placeholders.
    cleaned_text = re.sub(
        r"\[\[IMAGE_\d+\]\]",
        "",
        cleaned_text,
    )

    # Remove excessive blank lines.
    cleaned_text = re.sub(
        r"\n{3,}",
        "\n\n",
        cleaned_text,
    )

    return (
        cleaned_text.strip()
        + "\n"
    )


# ============================================================
# 9. Reducer
# ============================================================
def merge_content(
    state: State,
) -> dict:
    """
    Sort worker sections and merge them into one blog.
    """
    plan = state["plan"]

    if plan is None:
        raise ValueError(
            "merge_content called without a plan."
        )

    ordered_sections = [
        section_markdown
        for _, section_markdown in sorted(
            state["sections"],
            key=lambda item: item[0],
        )
    ]

    body = "\n\n".join(
        ordered_sections
    ).strip()

    merged_markdown = (
        f"# {plan.blog_title}\n\n"
        f"{body}\n"
    )

    return {
        "merged_md": merged_markdown,
    }


def _safe_slug(
    title: str,
) -> str:
    """
    Convert a title into a safe filename.
    """
    slug = title.strip().lower()

    slug = re.sub(
        r"[^a-z0-9 _-]+",
        "",
        slug,
    )

    slug = re.sub(
        r"\s+",
        "_",
        slug,
    ).strip("_")

    return slug or "blog"


def save_final(
    state: State,
) -> dict:
    """
    Clean and save the final publishable Markdown blog.
    """
    plan = state["plan"]

    if plan is None:
        raise ValueError(
            "save_final called without a plan."
        )

    final_markdown = clean_blog_markdown(
        state["merged_md"]
    )

    output_directory = Path(
        "generated_blogs"
    )

    output_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = (
        output_directory
        / f"{_safe_slug(plan.blog_title)}.md"
    )

    output_path.write_text(
        final_markdown,
        encoding="utf-8",
    )

    return {
        "final": final_markdown,
    }


# Build reducer subgraph.
reducer_graph = StateGraph(State)

reducer_graph.add_node(
    "merge_content",
    merge_content,
)

reducer_graph.add_node(
    "save_final",
    save_final,
)

reducer_graph.add_edge(
    START,
    "merge_content",
)

reducer_graph.add_edge(
    "merge_content",
    "save_final",
)

reducer_graph.add_edge(
    "save_final",
    END,
)

reducer_subgraph = (
    reducer_graph.compile()
)


# ============================================================
# 10. Main graph
# ============================================================
graph = StateGraph(State)

graph.add_node(
    "router",
    router_node,
)

graph.add_node(
    "research",
    research_node,
)

graph.add_node(
    "orchestrator",
    orchestrator_node,
)

graph.add_node(
    "worker",
    worker_node,
)

graph.add_node(
    "reducer",
    reducer_subgraph,
)

graph.add_edge(
    START,
    "router",
)

graph.add_conditional_edges(
    "router",
    route_next,
    {
        "research": "research",
        "orchestrator": "orchestrator",
    },
)

graph.add_edge(
    "research",
    "orchestrator",
)

graph.add_conditional_edges(
    "orchestrator",
    fanout,
    ["worker"],
)

graph.add_edge(
    "worker",
    "reducer",
)

graph.add_edge(
    "reducer",
    END,
)

app = graph.compile()