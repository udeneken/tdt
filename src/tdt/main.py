import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import re
import shlex
import subprocess
import sys
from time import monotonic

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.document._document import Selection
from textual.timer import Timer
from textual.widgets import Footer, Static, TextArea
from rich.text import Text


DEFAULT_DELAY_SECONDS = 1.0
CHECK_INTERVAL_MS = 100
COPY_SELECTION_DURATION_SECONDS = 0.18
TIMEOUT_SELECTION_DURATION_SECONDS = 0.18
DEFAULT_STRESS_LEVEL = "mid"
STRESS_LEVELS = ("none", "mid", "high")
FOCUSED_WORD_PATTERN = re.compile(r"\w+")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tdt",
        description="Write continuously or lose the current block after some time of inactivity.",
    )
    parser.add_argument(
        "-n",
        "--no-review",
        action="store_true",
        help="Exit immediately instead of entering review mode.",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        help="Show a writing prompt above the editor.",
    )
    parser.add_argument(
        "-s",
        "--sprint",
        type=positive_float,
        metavar="MINUTES",
        help="End the session after the given number of minutes.",
    )
    parser.add_argument(
        "-d",
        "--delay",
        type=positive_float,
        default=DEFAULT_DELAY_SECONDS,
        metavar="SECONDS",
        help="Seconds of inactivity before the current block is deleted (default: 1 second).",
    )
    parser.add_argument(
        "-st",
        "--show-time",
        action="store_true",
        help="Show timer information in the title bar.",
    )
    parser.add_argument(
        "-f",
        "--focus",
        action="store_true",
        help="Use focused reading by bolding completed word starts.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=output_path,
        help="Save the final text to the given file.",
    )
    parser.add_argument(
        "--dark",
        action="store_true",
        help="Use a dark background.",
    )
    parser.add_argument(
        "--stress",
        choices=STRESS_LEVELS,
        default=DEFAULT_STRESS_LEVEL,
        help=(
            "Set timeout feedback: none hides text immediately, mid keeps the current "
            "selection flash, high also tints text red during the last 20%% of the delay "
            f"(default: {DEFAULT_STRESS_LEVEL})."
        ),
    )
    return parser.parse_args(argv)


@contextmanager
def redirected_stdout_to_tty(enabled: bool):
    redirected_stdout_fd: int | None = None
    tty_stream = None
    redirection_active = False
    tty_path = "CONOUT$" if os.name == "nt" else "/dev/tty"

    try:
        if enabled:
            try:
                redirected_stdout_fd = os.dup(sys.stdout.fileno())
                tty_stream = open(tty_path, "w", encoding=sys.stdout.encoding or "utf-8")
                try:
                    sys.stdout.flush()
                except BrokenPipeError:
                    _exit_on_broken_pipe()
                os.dup2(tty_stream.fileno(), sys.stdout.fileno())
                redirection_active = True
            except OSError:
                if redirected_stdout_fd is not None:
                    os.close(redirected_stdout_fd)
                    redirected_stdout_fd = None

        yield redirection_active
    finally:
        if redirected_stdout_fd is not None:
            try:
                sys.stdout.flush()
            except BrokenPipeError:
                os.close(redirected_stdout_fd)
                if tty_stream is not None:
                    tty_stream.close()
                _exit_on_broken_pipe()
            os.dup2(redirected_stdout_fd, sys.stdout.fileno())
            os.close(redirected_stdout_fd)
            sys.stdout = os.fdopen(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)

        if tty_stream is not None:
            tty_stream.close()


def _exit_on_broken_pipe() -> None:
    try:
        sys.stdout.close()
    except OSError:
        pass
    raise SystemExit(141)


def positive_float(value: str) -> float:
    val = float(value)
    if val <= 0:
        raise argparse.ArgumentTypeError("input must be greater than 0")
    return val


def output_path(value: str) -> str:
    parent_dir = os.path.dirname(os.path.abspath(value)) or "."
    if not os.path.isdir(parent_dir):
        raise argparse.ArgumentTypeError(
            f"output directory does not exist: {parent_dir}"
        )
    return value


def write_output_file(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as output_file:
        output_file.write(text)
        if text and not text.endswith("\n"):
            output_file.write("\n")


def apply_focused_reading(line: Text, *, completed_words_only: bool = False) -> Text:
    for match in FOCUSED_WORD_PATTERN.finditer(line.plain):
        if completed_words_only and not _is_completed_word(line.plain, match.end()):
            continue

        prefix_length = get_focused_prefix_length(match.group())
        line.stylize("bold", match.start(), match.start() + prefix_length)
    return line


def _is_completed_word(text: str, word_end: int) -> bool:
    if word_end >= len(text):
        return False

    next_word = FOCUSED_WORD_PATTERN.search(text, word_end)
    boundary_text = text[word_end : next_word.start() if next_word else len(text)]
    return any(char.isspace() for char in boundary_text)


def get_focused_prefix_length(word: str) -> int:
    word_length = len(word)
    if word_length <= 3:
        return 1
    if word_length <= 5:
        return 2
    return 3


@dataclass
class SessionState:
    delay_ms: int
    sprint_duration_ms: int | None
    saved_blocks: list[str] = field(default_factory=list)
    current_text: str = ""
    first_activity_at: float | None = None
    last_activity_at: float | None = None
    review_elapsed_ms: int = 0
    in_review_mode: bool = False

    def update_text(self, text: str) -> None:
        self.current_text = text
        if not text:
            self.last_activity_at = None
            return

        now = monotonic()
        if self.first_activity_at is None:
            self.first_activity_at = now
        self.last_activity_at = now

    def commit_current_block(self) -> bool:
        if not self.current_text.strip():
            self.last_activity_at = None
            return False

        self.saved_blocks.append(self.current_text)
        self.current_text = ""
        self.last_activity_at = None
        return True

    def get_review_text(self) -> str:
        parts = [*self.saved_blocks]
        if self.current_text:
            parts.append(self.current_text)
        while parts and not parts[-1]:
            parts.pop()
        text = ""
        for part in parts:
            if not text:
                text = part
            elif text.endswith("\n"):
                text += part
            else:
                text += f"\n{part}"
        return text

    def has_any_text(self) -> bool:
        return bool(self.saved_blocks or self.current_text)

    def word_count(self) -> int:
        return len(self.get_review_text().split())

    def elapsed_ms_since(self, timestamp: float | None) -> int | None:
        if timestamp is None:
            return None
        return int((monotonic() - timestamp) * 1000)

    def remaining_delay_ms(self) -> int:
        if self.last_activity_at is None or not self.current_text.strip():
            return self.delay_ms

        elapsed_ms = self.elapsed_ms_since(self.last_activity_at)
        assert elapsed_ms is not None
        return max(0, self.delay_ms - elapsed_ms)

    def remaining_sprint_ms(self) -> int | None:
        if self.sprint_duration_ms is None:
            return None
        if self.first_activity_at is None:
            return self.sprint_duration_ms

        elapsed_ms = self.elapsed_ms_since(self.first_activity_at)
        assert elapsed_ms is not None
        return max(0, self.sprint_duration_ms - elapsed_ms)

    def enter_review_mode(self) -> None:
        elapsed_ms = self.elapsed_ms_since(self.first_activity_at)
        self.review_elapsed_ms = elapsed_ms if elapsed_ms is not None else 0
        self.in_review_mode = True

    def reset(self, *, clear_saved_blocks: bool) -> None:
        self.first_activity_at = None
        self.last_activity_at = None
        self.review_elapsed_ms = 0
        self.current_text = ""
        self.in_review_mode = False
        if clear_saved_blocks:
            self.saved_blocks.clear()

    def start_append_session(self) -> None:
        preserved_text = self.get_review_text()
        self.reset(clear_saved_blocks=True)
        if preserved_text:
            self.saved_blocks.append(preserved_text)


@dataclass(frozen=True)
class AppConfig:
    no_review: bool = False
    delay_seconds: float = DEFAULT_DELAY_SECONDS
    sprint_minutes: float | None = None
    prompt: str | None = None
    show_time: bool = False
    dark: bool = False
    stress: str = DEFAULT_STRESS_LEVEL
    focus: bool = False


@dataclass
class AppResult:
    final_output: str = ""
    editor_command: list[str] | None = None
    editor_input_text: str = ""


class ReviewTextArea(TextArea):
    BINDINGS = [
        ("r", "restart_session", "Restart"),
        ("a", "append_session", "Append"),
        ("c", "copy_session", "Copy"),
        ("e", "edit_session", "Edit"),
        ("j", "scroll_down", "Down"),
        ("k", "scroll_up", "Up"),
    ]

    def __init__(
        self, *args: object, focused_reading: bool = False, **kwargs: object
    ) -> None:
        super().__init__(*args, **kwargs)
        self.focused_reading = focused_reading
        self._copy_selection_timer: Timer | None = None
        self._copy_selection_cursor: tuple[int, int] | None = None

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        if not self.focused_reading:
            return line

        return apply_focused_reading(line)

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"r", "a"}:
            event.stop()
            event.prevent_default()
            if event.key == "r":
                self.app.restart_session()
            else:
                self.app.append_session()
            return
        await super()._on_key(event)

    def action_copy_session(self) -> None:
        if self.app.copy_session_to_clipboard():
            self.animate_copy_selection()

    def animate_copy_selection(self) -> None:
        if not self.text:
            return

        if self._copy_selection_timer is None:
            self._copy_selection_cursor = self.selection.end
        else:
            self._copy_selection_timer.stop()

        self.select_all()
        self._copy_selection_timer = self.set_timer(
            COPY_SELECTION_DURATION_SECONDS,
            self._clear_copy_selection,
            name="copy-selection",
        )

    def _clear_copy_selection(self) -> None:
        cursor = self._copy_selection_cursor
        self._copy_selection_timer = None
        self._copy_selection_cursor = None
        if cursor is not None:
            self.selection = Selection.cursor(cursor)

    def action_append_session(self) -> None:
        self.app.append_session()

    def action_edit_session(self) -> None:
        self.app.edit_session_in_editor()

    def action_scroll_down(self) -> None:
        self.scroll_relative(y=1)

    def action_scroll_up(self) -> None:
        self.scroll_relative(y=-1)


class InputTextArea(TextArea):
    def __init__(
        self, *args: object, focused_reading: bool = False, **kwargs: object
    ) -> None:
        super().__init__(*args, **kwargs)
        self.focused_reading = focused_reading

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        if not self.focused_reading:
            return line

        return apply_focused_reading(line, completed_words_only=True)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.app.commit_current_block()
            return
        await super()._on_key(event)


class TypeDontThinkTUI(App[None]):
    CSS = """
    Screen {
        layout: vertical;
        width: 100%;
        height: 100%;
        background: #f5f5f5;
        color: black;
    }

    #root {
        width: 100%;
        height: 1fr;
        padding: 1 2;
    }

    #title {
        text-style: bold;
        margin: 0 0 1 0;
    }

    #prompt {
        margin: 0 0 1 0;
        padding: 0 1;
        color: $text-muted;
        background: $surface;
        border-left: wide $accent;
    }

    #editor,
    #review {
        width: 100%;
        height: 1fr;
        background: #f5f5f5;
        color: black;
        border: round $accent;
    }

    #review {
        overflow: auto;
        border: round green;
    }

    .hidden {
        display: none;
    }

    .dark Screen {
        background: #2f2f2f;
        color: #f5f5f5;
    }

    .dark #prompt {
        color: #d0d0d0;
        background: #3a3a3a;
    }

    .dark #editor,
    .dark #review {
        background: #2f2f2f;
        color: #f5f5f5;
    }

    #editor.timeout-selection .text-area--selection {
        background: #f3c5c5;
    }

    #editor.stress-warning {
        color: #8f3f3f;
    }

    .dark #editor.stress-warning {
        color: #ffb0b0;
    }
    """

    BINDINGS = [
        ("escape", "handle_escape", "Review / Quit"),
    ]

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self.prompt = config.prompt.strip() if config.prompt else ""
        self.session = SessionState(
            delay_ms=int(config.delay_seconds * 1000),
            sprint_duration_ms=(
                int(config.sprint_minutes * 60 * 1000)
                if config.sprint_minutes is not None
                else None
            ),
        )
        if self.config.no_review:
            self._bindings = self._bindings.copy()
            self._bindings.key_to_bindings["escape"] = []
            self._bindings.bind("escape", "handle_escape", "Quit")
        self.result = AppResult()
        self._last_status_text: object = ""
        self._status_notice = ""
        self._status_notice_timer: Timer | None = None
        self.title_widget: Static | None = None
        self.editor: InputTextArea | None = None
        self.review: ReviewTextArea | None = None
        self._timeout_selection_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield Static("", id="title")
            yield Static(self._get_prompt_text(), id="prompt", classes="hidden" if not self.prompt else "")
            yield InputTextArea(id="editor", focused_reading=self.config.focus)
            yield ReviewTextArea(
                "",
                id="review",
                read_only=True,
                show_cursor=False,
                classes="hidden",
                focused_reading=self.config.focus,
            )
        yield Footer()

    def on_mount(self) -> None:
        if self.config.dark:
            self.add_class("dark")
        self.title_widget = self.query_one("#title", Static)
        self.editor = self.query_one("#editor", InputTextArea)
        self.review = self.query_one("#review", ReviewTextArea)
        self.editor.focus()
        self.set_interval(CHECK_INTERVAL_MS / 1000, self._tick)
        self._refresh_status()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "editor" or self.session.in_review_mode:
            return

        self.session.update_text(event.text_area.text)
        self._refresh_editor_stress_state()
        self._refresh_status()

    def action_handle_escape(self) -> None:
        if self.session.in_review_mode:
            self._exit_review_mode()
            return

        if not self.session.has_any_text():
            self.exit()
            return

        if self.config.no_review:
            self._exit_without_review()
            return

        self._enter_review_mode()

    def restart_session(self) -> None:
        if not self.session.in_review_mode:
            return

        self.session.reset(clear_saved_blocks=True)
        assert self.review is not None
        assert self.editor is not None
        self.review.load_text("")
        self.review.add_class("hidden")
        self.editor.load_text("")
        self.editor.remove_class("hidden")
        self.editor.focus()
        self._refresh_status()

    def append_session(self) -> None:
        if not self.session.in_review_mode:
            return

        self.session.start_append_session()
        assert self.review is not None
        assert self.editor is not None
        self.review.load_text("")
        self.review.add_class("hidden")
        self.editor.load_text("")
        self.editor.remove_class("hidden")
        self.editor.focus()
        self._refresh_status()

    def _tick(self) -> None:
        if self.session.in_review_mode:
            return

        self._end_sprint_if_needed()
        if self.session.in_review_mode:
            return
        self._refresh_editor_stress_state()
        self._expire_input_if_needed()
        if self.config.show_time:
            self._refresh_status()

    def _end_sprint_if_needed(self) -> None:
        sprint_remaining_ms = self.session.remaining_sprint_ms()
        if sprint_remaining_ms is None:
            return

        if sprint_remaining_ms > 0:
            return

        self.action_handle_escape()

    def _expire_input_if_needed(self) -> None:
        if self._timeout_selection_timer is not None:
            return

        if self.session.last_activity_at is None or not self.session.current_text.strip():
            return

        if self.session.remaining_delay_ms() > 0:
            return

        if self.config.stress == "none":
            self.commit_current_block()
            return

        self._flash_timeout_selection()

    def commit_current_block(self) -> bool:
        if not self.session.commit_current_block():
            return False

        assert self.editor is not None
        self.editor.load_text("")
        self._refresh_editor_stress_state()
        self._refresh_status()
        return True

    def _flash_timeout_selection(self) -> None:
        assert self.editor is not None

        self.editor.remove_class("stress-warning")
        self.editor.read_only = True
        self.editor.add_class("timeout-selection")
        self.editor.select_all()
        self._timeout_selection_timer = self.set_timer(
            TIMEOUT_SELECTION_DURATION_SECONDS,
            self._commit_expired_block,
            name="timeout-selection",
        )

    def _commit_expired_block(self) -> None:
        assert self.editor is not None

        self._timeout_selection_timer = None
        self.editor.remove_class("timeout-selection")
        self.editor.read_only = False
        self.commit_current_block()

    def _refresh_editor_stress_state(self) -> None:
        assert self.editor is not None

        should_warn = False
        if (
            self.config.stress == "high"
            and not self.session.in_review_mode
            and self._timeout_selection_timer is None
            and self.session.last_activity_at is not None
            and self.session.current_text.strip()
        ):
            should_warn = self.session.remaining_delay_ms() <= self.session.delay_ms * 0.5

        if should_warn:
            self.editor.add_class("stress-warning")
        else:
            self.editor.remove_class("stress-warning")

    def _refresh_status(self) -> None:
        status_text: str | Text
        if self.session.in_review_mode:
            status_parts = ["Type Don't Think", "review"]
            status_parts.append(self._format_elapsed_time(self.session.review_elapsed_ms))
            status_parts.append(f"{self.session.word_count()} words")
            if self._status_notice:
                status_parts.append(self._status_notice)
            status_text = " | ".join(status_parts)
        else:
            status_text = Text("Type Don't Think | input")
            sprint_progress = self._get_sprint_progress_text()
            if sprint_progress is not None:
                status_text.append(" | ")
                status_text.append_text(sprint_progress)
            if self._status_notice:
                status_text.append(f" | {self._status_notice}")

        status_signature = self._get_status_signature(status_text)
        if status_signature == self._last_status_text:
            return

        assert self.title_widget is not None
        self.title_widget.update(status_text)
        self._last_status_text = status_signature

    def _get_status_signature(self, status_text: str | Text) -> object:
        if isinstance(status_text, Text):
            return (
                status_text.plain,
                tuple((span.start, span.end, str(span.style)) for span in status_text.spans),
            )
        return status_text

    def _get_sprint_progress_text(self) -> Text | None:
        if (
            not self.config.show_time
            or self.session.in_review_mode
            or self.session.sprint_duration_ms is None
        ):
            return None

        width = 24
        if self.session.first_activity_at is None:
            remaining_ms = self.session.sprint_duration_ms
            completed = 0.0
        else:
            remaining_ms = self.session.remaining_sprint_ms()
            assert remaining_ms is not None
            completed = 1 - (remaining_ms / self.session.sprint_duration_ms)
        completed = max(0.0, min(1.0, completed))
        filled = round(width * completed)
        warning_start = width - max(1, round(width * 0.2))
        should_tint = self.config.stress == "high" and completed >= 0.8

        progress = Text("[")
        for index in range(width):
            char = "#" if index < filled else "-"
            style = "red" if should_tint and index >= warning_start else None
            progress.append(char, style=style)
        progress.append(f"] {self._format_elapsed_time(remaining_ms)}")
        return progress

    def _refresh_review(self) -> None:
        assert self.review is not None
        self.review.load_text(self.session.get_review_text())

    def _get_prompt_text(self) -> str:
        return f"Prompt: {self.prompt}" if self.prompt else ""

    def _format_elapsed_time(self, elapsed_ms: int) -> str:
        total_seconds = max(0, elapsed_ms // 1000)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02.0f}:{seconds:02.0f}"
        return f"{minutes:02.0f}:{seconds:02.0f}"

    def _enter_review_mode(self) -> None:
        self.session.enter_review_mode()
        self._refresh_review()
        assert self.editor is not None
        assert self.review is not None
        self.editor.remove_class("stress-warning")
        self.editor.remove_class("timeout-selection")
        self.editor.add_class("hidden")
        self.review.remove_class("hidden")
        self.review.focus()
        self._refresh_status()

    def _exit_review_mode(self) -> None:
        self.result.final_output = self.session.get_review_text()
        self.exit()

    def _exit_without_review(self) -> None:
        self.result.final_output = self.session.get_review_text()
        self.exit()

    def _show_status_notice(self, message: str, duration_seconds: float = 2.5) -> None:
        self._status_notice = message
        self._refresh_status()
        if self._status_notice_timer is not None:
            self._status_notice_timer.stop()
        self._status_notice_timer = self.set_timer(
            duration_seconds,
            self._clear_status_notice,
            name="status-notice",
        )

    def _clear_status_notice(self) -> None:
        self._status_notice = ""
        self._status_notice_timer = None
        self._refresh_status()

    def edit_session_in_editor(self) -> bool:
        editor = os.environ.get("EDITOR")
        if not editor:
            self.bell()
            self._show_status_notice("Set $EDITOR to use edit mode.")
            return False

        text = self.session.get_review_text()
        if not text:
            return False

        editor_command = shlex.split(editor)
        if not editor_command:
            self.bell()
            self._show_status_notice("Set $EDITOR to use edit mode.")
            return False

        self.result.editor_command = editor_command
        self.result.editor_input_text = text
        self.result.final_output = ""
        self.exit()
        return True

    def copy_session_to_clipboard(self) -> bool:
        text = self.session.get_review_text()
        if not text:
            return False

        if sys.platform == "darwin":
            try:
                subprocess.run(["pbcopy"], input=text, text=True, check=True)
                return True
            except (OSError, subprocess.SubprocessError):
                pass

        self.copy_to_clipboard(text)
        return True


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    should_emit_final_output = not sys.stdout.isatty()
    config = AppConfig(
        no_review=args.no_review,
        delay_seconds=args.delay,
        sprint_minutes=args.sprint,
        prompt=args.prompt,
        show_time=args.show_time,
        dark=args.dark,
        stress=args.stress,
        focus=args.focus,
    )
    app = TypeDontThinkTUI(config)

    with redirected_stdout_to_tty(should_emit_final_output):
        app.run()

    if app.result.editor_command:
        editor_command = [*app.result.editor_command]
        if "-" not in editor_command[1:]:
            editor_command.append("-")
        try:
            completed = subprocess.run(
                editor_command,
                input=app.result.editor_input_text,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                sys.stderr.write(
                    "tdt: failed to open $EDITOR with stdin. "
                    "Set $EDITOR to a command that can read from stdin, for example "
                    "'nvim -'.\n"
                )
                sys.stderr.flush()
        except OSError:
            sys.stderr.write("tdt: failed to launch $EDITOR.\n")
            sys.stderr.flush()
        return

    if args.output and app.result.final_output:
        write_output_file(args.output, app.result.final_output)

    if should_emit_final_output and app.result.final_output:
        try:
            sys.stdout.write(app.result.final_output)
            if not app.result.final_output.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
        except BrokenPipeError:
            _exit_on_broken_pipe()


if __name__ == "__main__":
    main()
