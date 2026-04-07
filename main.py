import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
import subprocess
import sys
from time import monotonic

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Static, TextArea


DEFAULT_DELAY_SECONDS = 1.0
CHECK_INTERVAL_MS = 100


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tdt",
        description="Write continuously or lose the current block after some time of inactivity.",
    )
    parser.add_argument(
        "-d",
        "--delay",
        type=positive_float,
        default=DEFAULT_DELAY_SECONDS,
        help="Seconds of inactivity before the current block is deleted (default: 1.0).",
    )
    parser.add_argument(
        "-n",
        "--no-review",
        action="store_true",
        help="Exit immediately instead of entering review mode.",
    )
    parser.add_argument(
        "-s",
        "--sprint",
        type=positive_int,
        metavar="MINUTES",
        help="End the session after the given number of minutes.",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        help="Show a writing prompt above the editor.",
    )
    parser.add_argument(
        "-st",
        "--show-time",
        action="store_true",
        help="Show timer information in the title bar.",
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
    delay = float(value)
    if delay <= 0:
        raise argparse.ArgumentTypeError("delay must be greater than 0")
    return delay


def positive_int(value: str) -> int:
    minutes = int(value)
    if minutes <= 0:
        raise argparse.ArgumentTypeError("minutes must be greater than 0")
    return minutes


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


class ReviewTextArea(TextArea):
    BINDINGS = [
        ("r", "restart_session", "Restart"),
        ("a", "append_session", "Append"),
        ("c", "copy_session", "Copy"),
        ("j", "scroll_down", "Down"),
        ("k", "scroll_up", "Up"),
    ]

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
        self.app.copy_session_to_clipboard()

    def action_append_session(self) -> None:
        self.app.append_session()

    def action_scroll_down(self) -> None:
        self.scroll_relative(y=1)

    def action_scroll_up(self) -> None:
        self.scroll_relative(y=-1)


class InputTextArea(TextArea):
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
        border: round $accent;
    }

    #review {
        overflow: auto;
        border: round green;
    }

    .hidden {
        display: none;
    }
    """

    BINDINGS = [
        ("escape", "handle_escape", "Review / Quit"),
    ]

    def __init__(
        self,
        *,
        no_review: bool = False,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        sprint_minutes: int | None = None,
        prompt: str | None = None,
        show_time: bool = False,
    ) -> None:
        super().__init__()
        self.no_review = no_review
        self.delay_seconds = delay_seconds
        self.prompt = prompt.strip() if prompt else ""
        self.show_time = show_time
        self.session = SessionState(
            delay_ms=int(delay_seconds * 1000),
            sprint_duration_ms=sprint_minutes * 60 * 1000 if sprint_minutes is not None else None,
        )
        if self.no_review:
            self._bindings = self._bindings.copy()
            self._bindings.key_to_bindings["escape"] = []
            self._bindings.bind("escape", "handle_escape", "Quit")
        self.final_output = ""
        self._last_status_text = ""
        self.title_widget: Static | None = None
        self.editor: InputTextArea | None = None
        self.review: ReviewTextArea | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield Static("", id="title")
            yield Static(self._get_prompt_text(), id="prompt", classes="hidden" if not self.prompt else "")
            yield InputTextArea(id="editor")
            yield ReviewTextArea("", id="review", read_only=True, classes="hidden")
        yield Footer()

    def on_mount(self) -> None:
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
        self._refresh_status()

    def action_handle_escape(self) -> None:
        if self.session.in_review_mode:
            self._exit_review_mode()
            return

        if not self.session.has_any_text():
            self.exit()
            return

        if self.no_review:
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
        self._expire_input_if_needed()
        if self.show_time:
            self._refresh_status()

    def _end_sprint_if_needed(self) -> None:
        sprint_remaining_ms = self.session.remaining_sprint_ms()
        if sprint_remaining_ms is None:
            return

        if sprint_remaining_ms > 0:
            return

        self.action_handle_escape()

    def _expire_input_if_needed(self) -> None:
        if self.session.last_activity_at is None or not self.session.current_text.strip():
            return

        if self.session.remaining_delay_ms() > 0:
            return

        self.commit_current_block()

    def commit_current_block(self) -> None:
        if not self.session.commit_current_block():
            return

        assert self.editor is not None
        self.editor.load_text("")
        self._refresh_status()

    def _refresh_status(self) -> None:
        status_text: str
        if self.session.in_review_mode:
            status_parts = ["Type Don't Think", "review"]
            status_parts.append(self._format_elapsed_time(self.session.review_elapsed_ms))
            status_parts.append(f"{self.session.word_count()} words")
            status_text = " | ".join(status_parts)
        else:
            status_parts = ["Type Don't Think", "input"]
            if self.show_time:
                status_parts.append(f"{self.session.remaining_delay_ms() / 1000:.1f}s")
            sprint_progress = self._get_sprint_progress_text()
            if sprint_progress is not None:
                status_parts.append(sprint_progress)
            status_text = " | ".join(status_parts)

        if status_text == self._last_status_text:
            return

        assert self.title_widget is not None
        self.title_widget.update(status_text)
        self._last_status_text = status_text

    def _get_sprint_progress_text(self) -> str | None:
        if not self.show_time or self.session.in_review_mode or self.session.sprint_duration_ms is None:
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
        bar = "#" * filled + "-" * (width - filled)
        return f"[{bar}] {self._format_elapsed_time(remaining_ms)}"

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
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _enter_review_mode(self) -> None:
        self.session.enter_review_mode()
        self._refresh_review()
        assert self.editor is not None
        assert self.review is not None
        self.editor.add_class("hidden")
        self.review.remove_class("hidden")
        self.review.focus()
        self._refresh_status()

    def _exit_review_mode(self) -> None:
        self.final_output = self.session.get_review_text()
        self.exit()

    def _exit_without_review(self) -> None:
        self.final_output = self.session.get_review_text()
        self.exit()

    def copy_session_to_clipboard(self) -> None:
        text = self.session.get_review_text()
        if not text:
            return

        if sys.platform == "darwin":
            try:
                subprocess.run(["pbcopy"], input=text, text=True, check=True)
                return
            except (OSError, subprocess.SubprocessError):
                pass

        self.copy_to_clipboard(text)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    should_emit_final_output = not sys.stdout.isatty()
    app = TypeDontThinkTUI(
        no_review=args.no_review,
        delay_seconds=args.delay,
        sprint_minutes=args.sprint,
        prompt=args.prompt,
        show_time=args.show_time,
    )

    with redirected_stdout_to_tty(should_emit_final_output):
        app.run()

    if should_emit_final_output and app.final_output:
        try:
            sys.stdout.write(app.final_output)
            if not app.final_output.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
        except BrokenPipeError:
            _exit_on_broken_pipe()


if __name__ == "__main__":
    main()
