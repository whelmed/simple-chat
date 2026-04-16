#!/usr/bin/env python3
import json
import os
import sqlite3
import subprocess
from io import StringIO
from pathlib import Path

import pandas as pd
from nicegui import events, ui
from openai import AsyncAzureOpenAI
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

# ── Paths ─────────────────────────────────────────────────────────────────────

_HOME          = Path(os.environ.get("HOME", "/home/primary"))
DB_PATH        = _HOME / "chat.db"
UPLOADS_DIR    = _HOME / "uploads"
SHARED_DIR     = Path("/home/project")
RESPONSE_FILE  = SHARED_DIR / "response.json"
AI_CONFIG_FILE = SHARED_DIR / "ai_config.json"
UPLOADS_DIR.mkdir(exist_ok=True)


# ── Constants ─────────────────────────────────────────────────────────────────
HUMAN = "human"
ROBOT = "robot"
# ── Database ──────────────────────────────────────────────────────────────────

def db_init() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                whom    TEXT    NOT NULL,
                text    TEXT    NOT NULL,
                file    TEXT,
                flagged INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)


def db_load() -> list[dict]:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute(
            "SELECT id, whom, text, file, flagged FROM messages ORDER BY id"
        ).fetchall()
    return [
        {"id": r[0], "whom": r[1], "text": r[2], "file": r[3], "flagged": bool(r[4])}
        for r in rows
    ]


def db_append(whom: str, text: str, file: str | None = None) -> int:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "INSERT INTO messages (whom, text, file) VALUES (?, ?, ?)",
            (whom, text, file),
        )
        return cur.lastrowid


def db_set_flag(message_id: int, flagged: bool) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE messages SET flagged = 0")
        if flagged:
            con.execute("UPDATE messages SET flagged = 1 WHERE id = ?", (message_id,))


def db_prune_from(message_id: int) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM messages WHERE id >= ?", (message_id,))


def db_clear() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("DELETE FROM messages")


def db_load_config() -> dict:
    with sqlite3.connect(DB_PATH) as con:
        rows = con.execute("SELECT key, value FROM config").fetchall()
    return {r[0]: r[1] for r in rows}


def db_save_config(key: str, value: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ── Config ────────────────────────────────────────────────────────────────────

CONFIG: dict[str, str] = {
    "endpoint":    "",
    "api_key":     "",
    "api_version": "",
    "deployment":  "",
}


def config_save(key: str, value: str) -> None:
    CONFIG[key] = value
    db_save_config(key, value)
    try:
        SHARED_DIR.mkdir(parents=True, exist_ok=True)
        AI_CONFIG_FILE.write_text(
            json.dumps({
                "endpoint":    CONFIG.get("endpoint", ""),
                "api_key":     CONFIG.get("api_key", ""),
                "api_version": CONFIG.get("api_version", ""),
                "deployment":  CONFIG.get("deployment", ""),
            }, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


# ── Theme ─────────────────────────────────────────────────────────────────────

PALETTE = {
    "primary":   "#4F46E5",
    "secondary": "#64748B",
    "accent":    "#818CF8",
    "positive":  "#22C55E",
    "negative":  "#EF4444",
    "info":      "#38BDF8",
    "warning":   "#F59E0B",
}

THEME_CSS = """
body, .q-page, p, div { font-size: 15px; }
body {
    background-color: #F1F5F9;
    font-family: 'Inter', sans-serif;
}
.msg-user {
    background-color: var(--q-primary);
    color: #ffffff;
    max-width: 32rem;
    padding: 0.6rem 1rem;
    border-radius: 1rem 1rem 0.25rem 1rem;
    line-height: 1.5;
}
.msg-bot {
    background-color: #ffffff;
    color: #1E293B;
    border: 1px solid #E2E8F0;
    max-width: 32rem;
    padding: 0.6rem 1rem;
    border-radius: 1rem 1rem 1rem 0.25rem;
    line-height: 1.5;
}
.msg-bot-flagged {
    background-color: #ffffff;
    color: #1E293B;
    border: 2px solid #F59E0B;
    max-width: 32rem;
    padding: 0.6rem 1rem;
    border-radius: 1rem 1rem 1rem 0.25rem;
    line-height: 1.5;
}
.msg-user p, .msg-bot p, .msg-bot-flagged p { font-size: 15px; margin: 0; }
.input-bar {
    background-color: #ffffff;
    border: 1px solid #E2E8F0;
    border-radius: 0.75rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
    padding: 0.25rem 0.5rem;
}
.chip-human {
    width: 2rem; height: 2rem; border-radius: 50%;
    background-color: #EEF2FF;
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.chip-bot {
    width: 2rem; height: 2rem; border-radius: 50%;
    background-color: #E0E7FF;
    display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.label-muted   { color: #94A3B8; font-size: 13px; }
.label-section { color: #64748B; font-size: 13px; }
.label-heading { color: #1E293B; }
.app-header    { background-color: #ffffff; border-bottom: 1px solid #E2E8F0; }
.table-breakout {
    width: calc(100vw - 3rem);
    position: relative;
    left: 50%;
    transform: translateX(-50%);
    max-width: calc(100vw - 3rem);
}
"""

# ── AI ────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """
You are an AI assistant.

When the user gives you a prompt follow it precisely.
If the prompt is vague or incomplete, do your best with what you have — but note briefly

""".strip()

_agent: Agent | None = None
_agent_config_snapshot: dict = {}


def get_agent() -> Agent:
    """Return a cached agent, rebuilding only when CONFIG has changed."""
    global _agent, _agent_config_snapshot
    if _agent is not None and _agent_config_snapshot == dict(CONFIG):
        return _agent

    client = AsyncAzureOpenAI(
        azure_endpoint=CONFIG["endpoint"],
        api_key=CONFIG["api_key"],
        api_version=CONFIG["api_version"] or "2023-05-15",
    )
    model = OpenAIChatModel(
        CONFIG["deployment"] or "gpt-4.1-mini",
        provider=OpenAIProvider(openai_client=client),
    )
    _agent = Agent(model, instructions=SYSTEM_PROMPT)

    @_agent.tool_plain
    def run_python(code: str) -> str:
        """Execute Python code in a subprocess and return stdout + stderr.

        Use this whenever computation, data analysis, or code execution would
        help answer the user's question. The code runs in the application's
        Python environment and has access to the uploads/ directory.
        """
        result = subprocess.run(
            ["python", "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        return output or "(no output)"

    _agent_config_snapshot = dict(CONFIG)
    return _agent


def history_from_db(exclude_last: int = 0) -> list[ModelMessage]:
    """Rebuild PydanticAI message history from the database.

    exclude_last: omit the final N rows (already passed as user_text).
    """
    rows = db_load()
    if exclude_last:
        rows = rows[:-exclude_last]
    history: list[ModelMessage] = []
    for row in rows:
        if row["whom"] == HUMAN:
            content = (
                f"[File: {row['file']}]\n\n{row['text']}" if row["file"] else row["text"]
            )
            history.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        else:
            history.append(ModelResponse(parts=[TextPart(content=row["text"])]))
    return history


async def get_ai_response(user_text: str) -> str:
    result = await get_agent().run(user_text, message_history=history_from_db(exclude_last=1))
    return result.output


def thinking_notification(message: str = "Thinking…") -> ui.notification:
    return ui.notification(message, type="ongoing", spinner=True, timeout=None)


# ── Avatar helpers ────────────────────────────────────────────────────────────

def avatar_human() -> None:
    with ui.element("div").classes("chip-human"):
        ui.icon("person", color="primary").style("font-size: 1.1rem")


def avatar_bot() -> None:
    with ui.element("div").classes("chip-bot"):
        ui.icon("smart_toy", color="primary").style("font-size: 1.1rem")


# ── File rendering ────────────────────────────────────────────────────────────

def render_file(filename: str, text: str) -> None:
    ext = Path(filename).suffix.lower()
    if ext == ".csv":
        try:
            df = pd.read_csv(StringIO(text))
            ui.table.from_pandas(df, pagination={"rowsPerPage": 10, "page": 1}).classes("w-full")
            return
        except Exception:
            pass
    if ext in (".md", ".markdown"):
        ui.markdown(text).classes("w-full bg-white border border-slate-200 rounded-lg p-4")
    else:
        lang = {".json": "json", ".yaml": "yaml", ".yml": "yaml"}.get(ext, "")
        ui.markdown(f"```{lang}\n{text}\n```").classes(
            "w-full max-h-96 overflow-auto bg-white border border-slate-200 rounded-lg p-2"
        )


# ── Chat UI ───────────────────────────────────────────────────────────────────

@ui.refreshable
async def chat_messages() -> None:
    for row in db_load():
        if row["whom"] == HUMAN:
            with ui.row().classes("w-full justify-end items-start gap-2 py-1"):
                with ui.column().classes("items-end gap-1"):
                    ui.markdown(
                        f"📎 `{row['file']}`" if row["file"] else row["text"]
                    ).classes("msg-user text-sm" if row["file"] else "msg-user")
                    if not row["file"]:
                        ui.button(
                            icon="edit",
                            on_click=lambda r=row: open_edit_dialog(r),
                        ).props("flat round dense color=grey-5").tooltip("Edit message")
                with ui.column().classes("items-center shrink-0"):
                    avatar_human()
            if row["file"]:
                with ui.row().classes("table-breakout py-1"):
                    render_file(row["file"], row["text"])
        else:
            msg_class = "msg-bot-flagged" if row["flagged"] else "msg-bot"
            with ui.row().classes("w-full justify-start items-start gap-2 py-1"):
                with ui.column().classes("items-center gap-0 shrink-0"):
                    avatar_bot()
                    flag_btn = ui.button(
                        icon="flag",
                        on_click=lambda r=row: toggle_flag(r["id"], r["flagged"]),
                    ).props("flat round dense")
                    if row["flagged"]:
                        flag_btn.props("color=warning").tooltip("Unflag this response")
                    else:
                        flag_btn.props("color=grey-5").tooltip("Flag for evaluation")
                ui.markdown(row["text"]).classes(msg_class)

    ui.run_javascript("window.scrollTo(0, document.body.scrollHeight)")


# ── Actions ───────────────────────────────────────────────────────────────────

async def toggle_flag(message_id: int, currently_flagged: bool) -> None:
    now_flagged = not currently_flagged
    db_set_flag(message_id, now_flagged)
    # Write the flagged response to the shared mount whenever a flag is set.
    # Cleared flags remove the file so the workspace container sees no result.
    if now_flagged:
        rows = db_load()
        row = next((r for r in rows if r["id"] == message_id), None)
        if row:
            try:
                SHARED_DIR.mkdir(parents=True, exist_ok=True)
                RESPONSE_FILE.write_text(f"""
                    Flagged Response:

                    {row["text"]}
                    """,
                    encoding="utf-8",
                )
            except OSError:
                pass
    else:
        try:
            RESPONSE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    chat_messages.refresh()


async def submit_prompt(inp: ui.textarea) -> None:
    text = inp.value.strip()
    if not text:
        return
    if not CONFIG["endpoint"] or not CONFIG["api_key"]:
        ui.notify("Configure Azure OpenAI credentials in Settings first.", type="warning")
        return
    db_append(HUMAN, text)
    inp.set_value("")
    inp.update()
    ui.run_javascript("document.querySelectorAll('textarea').forEach(t => t.style.height = 'auto')")
    chat_messages.refresh()
    note = thinking_notification()
    try:
        reply = await get_ai_response(text)
    except Exception as exc:
        reply = f"⚠️ Error: {exc}"
    finally:
        note.dismiss()
    db_append(ROBOT, reply)
    chat_messages.refresh()


async def handle_upload(e: events.UploadEventArguments) -> None:
    text = await e.file.text()
    file_path = UPLOADS_DIR / e.file.name
    file_path.write_text(text, encoding="utf-8")
    db_append(HUMAN, text, e.file.name)
    chat_messages.refresh()
    if not CONFIG["endpoint"] or not CONFIG["api_key"]:
        return
    prompt = (
        f"[File uploaded: {e.file.name}]\n"
        f"[Local path: {file_path.resolve()}]\n\n"
        f"{text}\n\n"
        "Briefly acknowledge the file, summarise its contents in one sentence, "
        "and note that you can analyse it using the path above."
    )
    note = thinking_notification("Reading file…")
    try:
        reply = await get_ai_response(prompt)
    except Exception as exc:
        reply = f"⚠️ Error: {exc}"
    finally:
        note.dismiss()
    db_append(ROBOT, reply)
    chat_messages.refresh()


async def open_edit_dialog(row: dict) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl gap-3"):
        ui.label("Edit message").classes("text-base font-semibold label-heading")
        ui.separator()
        edit_input = (
            ui.textarea(value=row["text"])
            .classes("w-full")
            .props("outlined autogrow")
            .style("font-size: 15px")
        )
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat color=secondary")
            ui.button(
                "Save & resend", icon="send",
                on_click=lambda: edit_saved(dialog, row["id"], edit_input),
            ).props("color=primary")
    dialog.open()


async def edit_saved(dialog: ui.dialog, message_id: int, inp: ui.textarea) -> None:
    text = inp.value.strip()
    if not text:
        return
    dialog.close()
    db_prune_from(message_id)
    db_append(HUMAN, text)
    chat_messages.refresh()
    if not CONFIG["endpoint"] or not CONFIG["api_key"]:
        return
    note = thinking_notification()
    try:
        reply = await get_ai_response(text)
    except Exception as exc:
        reply = f"⚠️ Error: {exc}"
    finally:
        note.dismiss()
    db_append(ROBOT, reply)
    chat_messages.refresh()


async def confirm_clear(parent_dialog: ui.dialog) -> None:
    with ui.dialog() as confirm_dialog, ui.card().classes("gap-3 px-6 py-4"):
        ui.label("Clear chat history?").classes("text-base font-semibold label-heading")
        ui.label("This will permanently delete all messages and cannot be undone.").classes("label-muted")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=confirm_dialog.close).props("flat color=secondary")
            async def cleared():
                confirm_dialog.close()
                db_clear()
                try:
                    RESPONSE_FILE.unlink(missing_ok=True)
                except OSError:
                    pass
                parent_dialog.close()
                chat_messages.refresh()
            ui.button("Clear", icon="delete_outline", on_click=cleared, color="negative")
    confirm_dialog.open()


# ── Page ──────────────────────────────────────────────────────────────────────

@ui.page("/")
async def page_layout() -> None:
    ui.colors(**PALETTE)
    ui.add_css(THEME_CSS)

    # Header
    with ui.header().classes("app-header items-center justify-between px-6 py-3"):
        with ui.row().classes("items-center gap-3"):
            avatar_bot()
            ui.label("Assistant").classes("text-base font-semibold label-heading")

        with ui.dialog() as settings_dialog, ui.card().classes("w-96"):
            ui.label("Settings").classes("text-base font-semibold label-heading")
            ui.separator()
            with ui.column().classes("w-full gap-3 pt-2"):
                ui.label("Azure OpenAI").classes(
                    "text-xs font-medium label-section uppercase tracking-wide"
                )
                for label, key, placeholder, kwargs in [
                    ("Endpoint",        "endpoint",    "https://<resource>.openai.azure.com/", {}),
                    ("API Key",         "api_key",     "••••••••", {"password": True, "password_toggle_button": True}),
                    ("API Version",     "api_version", "2024-07-01-preview", {}),
                    ("Deployment Name", "deployment",  "gpt-4o", {}),
                ]:
                    ui.input(
                        label=label,
                        placeholder=placeholder,
                        value=CONFIG.get(key, ""),
                        on_change=lambda e, k=key: config_save(k, e.value),
                        **kwargs,
                    ).classes("w-full").props("outlined dense")
                ui.separator()
                ui.button(
                    "Clear chat history", icon="delete_outline",
                    on_click=lambda: confirm_clear(settings_dialog),
                    color="negative",
                ).classes("w-full").props("flat dense")
                ui.separator()
                ui.button(
                    "Save & close", icon="check",
                    on_click=settings_dialog.close,
                    color="primary",
                ).classes("w-full").props("dense")

        ui.button(icon="settings", on_click=settings_dialog.open).props("flat round color=secondary")

    # Chat body
    with ui.column().classes("w-full max-w-2xl mx-auto px-4 pt-4 pb-36 gap-0"):
        await chat_messages()

    # Input footer
    with ui.footer().classes("pb-4").style("background-color: #F1F5F9; border-top: 1px solid #E2E8F0;"):
        with ui.column().classes("w-full max-w-2xl mx-auto px-4 gap-2"):
            with ui.row().classes("w-full items-end gap-2 input-bar"):
                message_input = (
                    ui.textarea(placeholder="Type a message…")
                    .classes("flex-1")
                    .props("borderless dense autogrow")
                    .style("font-size: 15px; min-height: 2.5rem;")
                )
                send_btn = ui.button(
                    icon="send",
                    on_click=lambda: submit_prompt(message_input),
                ).props("round flat dense color=primary")

            ui.label("Attach a file to include in the conversation").classes("label-muted text-xs px-1")
            ui.upload(
                on_upload=handle_upload,
                max_file_size=1024 * 1024,
                auto_upload=True,
            ).classes("w-full").props(
                'color=primary flat accept=".csv,.txt,.md,.markdown,.json,.yaml,.yml,.log,.tsv"'
            )

    await ui.context.client.connected()

    # Intercept Enter to submit; Shift+Enter inserts a newline as normal.
    await ui.run_javascript(f"""
        (function poll() {{
            const ta = Array.from(document.querySelectorAll('textarea'))
                           .find(t => t.placeholder === 'Type a message\u2026');
            if (!ta) {{ setTimeout(poll, 100); return; }}
            ta.addEventListener('keydown', function(e) {{
                if (e.key === 'Enter' && !e.shiftKey) {{
                    e.preventDefault();
                    e.stopPropagation();
                    const icons = document.querySelectorAll('.material-icons');
                    for (const ic of icons) {{
                        if (ic.textContent.trim() === 'send') {{
                            ic.closest('button')?.click();
                            break;
                        }}
                    }}
                }}
            }}, true);
        }})();
    """)


# ── CLI ───────────────────────────────────────────────────────────────────────

async def cmd_evaluate() -> None:
    """Read a prompt from stdin and send it to the configured AI (no history, no DB writes).

    Usage:
        python main.py --evaluate < prompt.txt

        # Evaluate a flagged response against a rubric:
        (cat evaluate_prompt.txt && python main.py --flagged --n 3) | python main.py --evaluate
    """
    import sys
    if not CONFIG.get("endpoint") or not CONFIG.get("api_key"):
        print(json.dumps({"error": "Azure OpenAI credentials not configured."}))
        return
    prompt = sys.stdin.read().strip()
    if not prompt:
        print(json.dumps({"error": "No input provided on stdin."}))
        return
    client = AsyncAzureOpenAI(
        azure_endpoint=CONFIG["endpoint"],
        api_key=CONFIG["api_key"],
        api_version=CONFIG["api_version"] or "2024-07-01-preview",
    )
    agent = Agent(
        OpenAIChatModel(CONFIG["deployment"] or "gpt-4o", provider=OpenAIProvider(openai_client=client)),
        instructions=SYSTEM_PROMPT,
    )
    result = await agent.run(prompt)
    print(result.output)


# ── Entry ─────────────────────────────────────────────────────────────────────

if __name__ in {"__main__", "__mp_main__"}:
    import argparse, asyncio

    parser = argparse.ArgumentParser(description="Prompt learning assistant")
    parser.add_argument("--evaluate", action="store_true", help="Read prompt from stdin, send to AI, print response, exit")
    parser.add_argument("--config",   type=str, default=None, help="Path to ai_config.json to load Azure credentials")
    args, _ = parser.parse_known_args()

    db_init()
    CONFIG.update(db_load_config())
    if args.config:
        try:
            CONFIG.update(json.loads(Path(args.config).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"error": f"Cannot load config file: {exc}"}))
            raise SystemExit(1)

    if args.evaluate:
        asyncio.run(cmd_evaluate())
    else:
        ui.run(title="Assistant", favicon="🤖", port=3000, host="0.0.0.0")
