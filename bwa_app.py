from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pandas as pd
import streamlit as st

from bwa import app


def clean_blog_markdown(markdown: str) -> str:
    if not markdown:
        return ""

    text = markdown.replace("\r\n", "\n").replace("\r", "\n")

    text = re.sub(
        r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]",
        "",
        text,
    )

    cleaned_lines = []
    skipping_image_block = False
    found_error_line = False

    for line in text.splitlines():
        stripped = line.strip()
        normalized = re.sub(r"^\s*>\s?", "", stripped)
        normalized = normalized.replace("**", "").strip()

        if "[IMAGE GENERATION FAILED]" in normalized:
            skipping_image_block = True
            found_error_line = False
            continue

        if skipping_image_block:
            if re.match(r"^#{1,6}\s+\S", stripped):
                skipping_image_block = False
                found_error_line = False
                cleaned_lines.append(line)
                continue

            if normalized.startswith("Error:"):
                found_error_line = True
                continue

            if found_error_line and not normalized:
                skipping_image_block = False
                found_error_line = False

            continue

        if re.fullmatch(r"\[\[IMAGE_\d+\]\]", normalized):
            continue

        if any(
            phrase in line
            for phrase in (
                "RESOURCE_EXHAUSTED",
                "generativelanguage.googleapis.com",
                "gemini-2.5-flash-preview-image",
                "gemini-2.5-flash-image",
                "Quota exceeded for metric",
            )
        ):
            continue

        if normalized.lower() in {
            "not found in provided sources.",
            "not found in provided sources",
            "image generation failed",
        }:
            continue

        cleaned_lines.append(line)

    cleaned_text = "\n".join(cleaned_lines)
    cleaned_text = re.sub(r"\[\[IMAGE_\d+\]\]", "", cleaned_text)
    cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text)

    return cleaned_text.strip() + "\n"
# ============================================================
# Page configuration
# ============================================================
st.set_page_config(
    page_title="Blog Writing Agent",
    page_icon="✍️",
    layout="wide",
)


# ============================================================
# Constants
# ============================================================
BLOG_DIRECTORY = Path(
    "generated_blogs"
)


# ============================================================
# General helpers
# ============================================================
def safe_slug(
    title: str,
) -> str:
    """
    Convert a blog title into a safe filename.
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


def object_to_dict(
    value: Any,
) -> Dict[str, Any]:
    """
    Convert a Pydantic model or similar object into a dictionary.
    """
    if value is None:
        return {}

    if hasattr(value, "model_dump"):
        return value.model_dump()

    if isinstance(value, dict):
        return value

    try:
        return json.loads(
            json.dumps(
                value,
                default=str,
            )
        )

    except Exception:
        return {}


def list_to_dicts(
    values: List[Any],
) -> List[Dict[str, Any]]:
    """
    Convert a list of models or dictionaries into dictionaries.
    """
    converted_values: List[
        Dict[str, Any]
    ] = []

    for value in values:
        converted = object_to_dict(
            value
        )

        if converted:
            converted_values.append(
                converted
            )

    return converted_values


# ============================================================
# LangGraph execution helpers
# ============================================================
def stream_graph(
    graph_app: Any,
    inputs: Dict[str, Any],
) -> Iterator[Tuple[str, Any]]:
    """
    Stream the graph once and use the final streamed state.

    This avoids running the graph a second time with invoke().
    """
    latest_state: Optional[
        Dict[str, Any]
    ] = None

    for state_value in graph_app.stream(
        inputs,
        stream_mode="values",
    ):
        if isinstance(
            state_value,
            dict,
        ):
            latest_state = state_value

        yield (
            "values",
            state_value,
        )

    if latest_state is None:
        raise RuntimeError(
            "The graph finished without returning "
            "a final state."
        )

    yield (
        "final",
        latest_state,
    )


def update_current_state(
    current_state: Dict[str, Any],
    payload: Any,
) -> Dict[str, Any]:
    """
    Merge streamed graph data into the current state.
    """
    if not isinstance(
        payload,
        dict,
    ):
        return current_state

    if (
        len(payload) == 1
        and isinstance(
            next(iter(payload.values())),
            dict,
        )
    ):
        inner_state = next(
            iter(payload.values())
        )

        current_state.update(
            inner_state
        )

    else:
        current_state.update(
            payload
        )

    return current_state


def get_task_count(
    plan: Any,
) -> Optional[int]:
    """
    Return the number of planned blog sections.
    """
    plan_dict = object_to_dict(
        plan
    )

    tasks = plan_dict.get(
        "tasks"
    )

    if isinstance(tasks, list):
        return len(tasks)

    return None


def infer_graph_stage(
    state: Dict[str, Any],
) -> str:
    """
    Create a readable workflow progress message.
    """
    if state.get("final"):
        return "Final blog completed"

    if state.get("merged_md"):
        return "Preparing final Markdown"

    sections = (
        state.get("sections")
        or []
    )

    if sections:
        return (
            f"Writing sections: "
            f"{len(sections)} completed"
        )

    if state.get("plan") is not None:
        return "Blog plan created"

    if (
        state.get("needs_research")
        and state.get("evidence")
    ):
        return "Research completed"

    if state.get("needs_research"):
        return "Researching the topic"

    if state.get("mode"):
        return (
            "Routing completed: "
            f"{state.get('mode')}"
        )

    return "Starting workflow"


# ============================================================
# Saved-blog helpers
# ============================================================
def list_past_blogs() -> List[Path]:
    """
    Return generated Markdown blogs, newest first.
    """
    if not BLOG_DIRECTORY.exists():
        return []

    blog_files = [
        file_path
        for file_path
        in BLOG_DIRECTORY.glob("*.md")
        if file_path.is_file()
    ]

    blog_files.sort(
        key=lambda file_path: (
            file_path.stat().st_mtime
        ),
        reverse=True,
    )

    return blog_files


def read_markdown_file(
    file_path: Path,
) -> str:
    """
    Read and clean a saved Markdown blog.
    """
    raw_markdown = file_path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    return clean_blog_markdown(
        raw_markdown
    )


def extract_title_from_markdown(
    markdown_text: str,
    fallback: str,
) -> str:
    """
    Extract the first level-one Markdown heading.
    """
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()

            if title:
                return title

    return fallback


# ============================================================
# Session state
# ============================================================
if "last_out" not in st.session_state:
    st.session_state["last_out"] = None

if "logs" not in st.session_state:
    st.session_state["logs"] = []


# ============================================================
# Page heading
# ============================================================
st.title(
    "Blog Writing Agent"
)

st.caption(
    "Generate clear, structured, and researched "
    "technical blogs using LangGraph."
)


# ============================================================
# Sidebar
# ============================================================
with st.sidebar:
    st.header(
        "Generate New Blog"
    )

    topic = st.text_area(
        "Blog topic",
        height=130,
        placeholder=(
            "Example: Best practices for "
            "deploying AI models in production"
        ),
    )

    as_of = st.date_input(
        "As-of date",
        value=date.today(),
    )

    run_button = st.button(
        "🚀 Generate Blog",
        type="primary",
        use_container_width=True,
    )

    st.divider()

    st.subheader(
        "Past Blogs"
    )

    past_blog_files = (
        list_past_blogs()
    )

    selected_file: Optional[
        Path
    ] = None

    if not past_blog_files:
        st.caption(
            "No saved blogs found yet."
        )

    else:
        labels: List[str] = []
        files_by_label: Dict[
            str,
            Path,
        ] = {}

        for file_path in past_blog_files[:50]:
            try:
                markdown_text = (
                    read_markdown_file(
                        file_path
                    )
                )

                saved_title = (
                    extract_title_from_markdown(
                        markdown_text,
                        file_path.stem,
                    )
                )

            except Exception:
                saved_title = (
                    file_path.stem
                )

            label = (
                f"{saved_title} · "
                f"{file_path.name}"
            )

            labels.append(label)
            files_by_label[label] = (
                file_path
            )

        selected_label = st.radio(
            "Select a saved blog",
            options=labels,
            index=0,
            label_visibility="collapsed",
        )

        selected_file = (
            files_by_label.get(
                selected_label
            )
        )

        if st.button(
            "📂 Load Selected Blog",
            use_container_width=True,
        ):
            if selected_file is not None:
                loaded_markdown = (
                    read_markdown_file(
                        selected_file
                    )
                )

                st.session_state[
                    "last_out"
                ] = {
                    "plan": None,
                    "evidence": [],
                    "final": loaded_markdown,
                }

                st.success(
                    f"Loaded "
                    f"{selected_file.name}"
                )


# ============================================================
# Tabs
# ============================================================
(
    tab_plan,
    tab_evidence,
    tab_preview,
    tab_logs,
) = st.tabs(
    [
        "🧩 Plan",
        "🔎 Evidence",
        "📝 Final Blog",
        "🧾 Logs",
    ]
)


# ============================================================
# Generate blog
# ============================================================
if run_button:
    if not topic.strip():
        st.warning(
            "Please enter a blog topic."
        )

        st.stop()

    # Remove the previous result before a new run.
    st.session_state[
        "last_out"
    ] = None

    graph_inputs: Dict[
        str,
        Any,
    ] = {
        "topic": topic.strip(),
        "mode": "",
        "needs_research": False,
        "queries": [],
        "evidence": [],
        "plan": None,
        "as_of": as_of.isoformat(),
        "recency_days": 7,
        "sections": [],
        "merged_md": "",
        "final": "",
    }

    status = st.status(
        "Running blog workflow...",
        expanded=True,
    )

    progress_area = st.empty()

    current_state: Dict[
        str,
        Any,
    ] = {}

    previous_stage: Optional[
        str
    ] = None

    current_run_logs: List[
        str
    ] = []

    try:
        for event_type, payload in stream_graph(
            app,
            graph_inputs,
        ):
            if event_type == "values":
                current_state = (
                    update_current_state(
                        current_state,
                        payload,
                    )
                )

                current_stage = (
                    infer_graph_stage(
                        current_state
                    )
                )

                if (
                    current_stage
                    != previous_stage
                ):
                    status.write(
                        f"➡️ {current_stage}"
                    )

                    current_run_logs.append(
                        current_stage
                    )

                    previous_stage = (
                        current_stage
                    )

                queries = (
                    current_state.get(
                        "queries"
                    )
                    or []
                )

                evidence = (
                    current_state.get(
                        "evidence"
                    )
                    or []
                )

                sections = (
                    current_state.get(
                        "sections"
                    )
                    or []
                )

                progress_summary = {
                    "stage": current_stage,
                    "mode": (
                        current_state.get(
                            "mode"
                        )
                    ),
                    "needs_research": (
                        current_state.get(
                            "needs_research"
                        )
                    ),
                    "queries": (
                        queries[:5]
                        if isinstance(
                            queries,
                            list,
                        )
                        else []
                    ),
                    "evidence_count": (
                        len(evidence)
                    ),
                    "planned_sections": (
                        get_task_count(
                            current_state.get(
                                "plan"
                            )
                        )
                    ),
                    "sections_completed": (
                        len(sections)
                    ),
                }

                progress_area.json(
                    progress_summary
                )

            elif event_type == "final":
                if not isinstance(
                    payload,
                    dict,
                ):
                    raise TypeError(
                        "The graph returned an "
                        "invalid final output."
                    )

                clean_output = dict(
                    payload
                )

                clean_output["final"] = (
                    clean_blog_markdown(
                        clean_output.get(
                            "final",
                            "",
                        )
                    )
                )

                st.session_state[
                    "last_out"
                ] = clean_output

                status.update(
                    label="✅ Blog completed",
                    state="complete",
                    expanded=False,
                )

                current_run_logs.append(
                    "Final blog completed"
                )

    except Exception as error:
        status.update(
            label=(
                "❌ Blog generation failed"
            ),
            state="error",
            expanded=True,
        )

        current_run_logs.append(
            f"Error: {error}"
        )

        st.error(
            "The blog could not be generated."
        )

        st.exception(error)

    if current_run_logs:
        st.session_state[
            "logs"
        ].extend(
            current_run_logs
        )


# ============================================================
# Display latest output
# ============================================================
output = st.session_state.get(
    "last_out"
)

if output:
    # --------------------------------------------------------
    # Plan tab
    # --------------------------------------------------------
    with tab_plan:
        st.subheader(
            "Blog Plan"
        )

        plan_object = output.get(
            "plan"
        )

        if not plan_object:
            st.info(
                "The plan is unavailable because "
                "this is a previously saved blog."
            )

        else:
            plan_dict = object_to_dict(
                plan_object
            )

            st.write(
                "**Title:**",
                plan_dict.get(
                    "blog_title",
                    "Untitled Blog",
                ),
            )

            (
                audience_column,
                tone_column,
                kind_column,
            ) = st.columns(3)

            audience_column.write(
                "**Audience:** "
                + str(
                    plan_dict.get(
                        "audience",
                        "",
                    )
                )
            )

            tone_column.write(
                "**Tone:** "
                + str(
                    plan_dict.get(
                        "tone",
                        "",
                    )
                )
            )

            kind_column.write(
                "**Blog kind:** "
                + str(
                    plan_dict.get(
                        "blog_kind",
                        "",
                    )
                )
            )

            constraints = (
                plan_dict.get(
                    "constraints"
                )
                or []
            )

            if constraints:
                st.write(
                    "**Constraints:**"
                )

                for constraint in constraints:
                    st.write(
                        f"- {constraint}"
                    )

            raw_tasks = (
                plan_dict.get("tasks")
                or []
            )

            tasks = list_to_dicts(
                raw_tasks
            )

            if tasks:
                task_rows: List[
                    Dict[str, Any]
                ] = []

                for task in tasks:
                    task_rows.append(
                        {
                            "id": task.get(
                                "id"
                            ),
                            "title": task.get(
                                "title"
                            ),
                            "target_words": (
                                task.get(
                                    "target_words"
                                )
                            ),
                            "requires_research": (
                                task.get(
                                    "requires_research"
                                )
                            ),
                            "requires_citations": (
                                task.get(
                                    "requires_citations"
                                )
                            ),
                            "requires_code": (
                                task.get(
                                    "requires_code"
                                )
                            ),
                            "tags": ", ".join(
                                task.get(
                                    "tags"
                                )
                                or []
                            ),
                        }
                    )

                task_dataframe = (
                    pd.DataFrame(
                        task_rows
                    )
                )

                if (
                    not task_dataframe.empty
                    and "id"
                    in task_dataframe.columns
                ):
                    task_dataframe = (
                        task_dataframe.sort_values(
                            "id"
                        )
                    )

                st.dataframe(
                    task_dataframe,
                    use_container_width=True,
                    hide_index=True,
                )

                with st.expander(
                    "View task details"
                ):
                    st.json(tasks)

    # --------------------------------------------------------
    # Evidence tab
    # --------------------------------------------------------
    with tab_evidence:
        st.subheader(
            "Research Evidence"
        )

        raw_evidence = (
            output.get("evidence")
            or []
        )

        evidence_items = (
            list_to_dicts(
                raw_evidence
            )
        )

        if not evidence_items:
            st.info(
                "No evidence was returned. "
                "The topic may have used "
                "closed-book mode."
            )

        else:
            evidence_rows: List[
                Dict[str, Any]
            ] = []

            for item in evidence_items:
                evidence_rows.append(
                    {
                        "title": item.get(
                            "title"
                        ),
                        "published_at": (
                            item.get(
                                "published_at"
                            )
                        ),
                        "source": item.get(
                            "source"
                        ),
                        "url": item.get(
                            "url"
                        ),
                    }
                )

            st.dataframe(
                pd.DataFrame(
                    evidence_rows
                ),
                use_container_width=True,
                hide_index=True,
            )

    # --------------------------------------------------------
    # Final blog tab
    # --------------------------------------------------------
    with tab_preview:
        st.subheader(
            "Ready-to-Post Blog"
        )

        final_markdown = (
            clean_blog_markdown(
                output.get(
                    "final",
                    "",
                )
            )
        )

        if not final_markdown:
            st.warning(
                "No final blog was found."
            )

        else:
            # Render only the clean finished blog.
            st.markdown(
                final_markdown,
                unsafe_allow_html=False,
            )

            plan_object = output.get(
                "plan"
            )

            if hasattr(
                plan_object,
                "blog_title",
            ):
                blog_title = (
                    plan_object.blog_title
                )

            elif isinstance(
                plan_object,
                dict,
            ):
                blog_title = (
                    plan_object.get(
                        "blog_title",
                        "blog",
                    )
                )

            else:
                blog_title = (
                    extract_title_from_markdown(
                        final_markdown,
                        "blog",
                    )
                )

            markdown_filename = (
                f"{safe_slug(blog_title)}.md"
            )

            st.divider()

            st.download_button(
                "⬇️ Download Clean Markdown",
                data=final_markdown.encode(
                    "utf-8"
                ),
                file_name=markdown_filename,
                mime="text/markdown",
                use_container_width=True,
            )

            with st.expander(
                "Copy Markdown Source"
            ):
                st.code(
                    final_markdown,
                    language="markdown",
                )

    # --------------------------------------------------------
    # Logs tab
    # --------------------------------------------------------
    with tab_logs:
        st.subheader(
            "Workflow Logs"
        )

        saved_logs = (
            st.session_state.get(
                "logs",
                [],
            )
        )

        if not saved_logs:
            st.info(
                "No workflow logs are available."
            )

        else:
            st.text_area(
                "Event log",
                value="\n".join(
                    saved_logs[-80:]
                ),
                height=420,
            )

            if st.button(
                "Clear Logs"
            ):
                st.session_state[
                    "logs"
                ] = []

                st.rerun()

else:
    with tab_plan:
        st.info(
            "The generated blog plan "
            "will appear here."
        )

    with tab_evidence:
        st.info(
            "Research evidence will "
            "appear here when needed."
        )

    with tab_preview:
        st.info(
            "Enter a topic in the sidebar "
            "and click **Generate Blog**."
        )

    with tab_logs:
        st.info(
            "Workflow logs will appear here."
        )