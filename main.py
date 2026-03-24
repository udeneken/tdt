import argparse
import os
import subprocess
import sys
from time import monotonic

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Static, TextArea


DEFAULT_DELAY_SECONDS = 1.0
CHECK_INTERVAL_MS = 250


def positive_float(value: str) -> float:
    delay = float(value)
    if delay <= 0:
        raise argparse.ArgumentTypeError("delay must be greater than 0")
    return delay


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
    return parser.parse_args(argv)


class ReviewTextArea(TextArea):
    BINDINGS = [
        ("enter", "restart_session", "Restart"),
        ("c", "copy_session_c", "Copy"),
        ("j", "scroll_down", "Down"),
        ("k", "scroll_up", "Up"),
    ]

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            self.app.restart_session()
            return
        await super()._on_key(event)

    def action_copy_session_y(self) -> None:
        self.app.copy_session_to_clipboard()

    def action_copy_session_c(self) -> None:
        self.app.copy_session_to_clipboard()

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

    #editor,
    #review {
        width: 100%;
        height: 1fr;
        border: round $accent;
    }

    #review {
        overflow: auto;
    }

    .hidden {
        display: none;
    }
    """

    BINDINGS = [
        ("escape", "handle_escape", "Review / Quit"),
    ]

    def __init__(self, *, no_review: bool = False, delay_seconds: float = DEFAULT_DELAY_SECONDS) -> None:
        super().__init__()
        self.no_review = no_review
        self.delay_seconds = delay_seconds
        self.delay_ms = int(delay_seconds * 1000)
        if self.no_review:
            self._bindings = self._bindings.copy()
            self._bindings.key_to_bindings["escape"] = []
            self._bindings.bind("escape", "handle_escape", "Quit")
        self.first_input_at: float | None = None
        self.last_keypress_at: float | None = None
        self.final_output = ""
        self.review_elapsed_ms = 0
        self.saved_blocks: list[str] = []
        self.current_text = ""
        self.in_review_mode = False

    def compose(self) -> ComposeResult:
        with Vertical(id="root"):
            yield Static("", id="title")
            yield InputTextArea(id="editor")
            yield ReviewTextArea("", id="review", read_only=True, classes="hidden")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#editor", InputTextArea).focus()
        self.set_interval(CHECK_INTERVAL_MS / 1000, self._tick)
        self._refresh_status()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "editor" or self.in_review_mode:
            return

        self.current_text = event.text_area.text
        if self.current_text:
            if self.first_input_at is None:
                self.first_input_at = monotonic()
            self.last_keypress_at = monotonic()
        else:
            self.last_keypress_at = None
        self._refresh_status()

    def action_handle_escape(self) -> None:
        if self.in_review_mode:
            self.final_output = self._get_review_text()
            self.exit()
            return

        if not self.saved_blocks and not self.current_text:
            self.exit()
            return

        if self.no_review:
            self.final_output = self._get_review_text()
            self.exit()
            return

        if self.first_input_at is not None:
            self.review_elapsed_ms = int((monotonic() - self.first_input_at) * 1000)

        self.in_review_mode = True
        self._refresh_review()
        self._refresh_status()
        self.query_one("#editor", TextArea).add_class("hidden")
        review = self.query_one("#review", ReviewTextArea)
        review.remove_class("hidden")
        review.focus()

    def restart_session(self) -> None:
        if not self.in_review_mode:
            return

        self.first_input_at = None
        self.last_keypress_at = None
        self.review_elapsed_ms = 0
        self.saved_blocks.clear()
        self.current_text = ""
        self.in_review_mode = False
        review = self.query_one("#review", ReviewTextArea)
        review.load_text("")
        review.add_class("hidden")
        editor = self.query_one("#editor", InputTextArea)
        editor.load_text("")
        editor.remove_class("hidden")
        editor.focus()
        self._refresh_status()

    def _tick(self) -> None:
        if self.in_review_mode:
            self._refresh_status()
            return

        self._expire_input_if_needed()
        self._refresh_status()

    def _expire_input_if_needed(self) -> None:
        if self.last_keypress_at is None:
            return

        elapsed_ms = int((monotonic() - self.last_keypress_at) * 1000)
        if elapsed_ms < self.delay_ms:
            return

        self.commit_current_block()

    def commit_current_block(self) -> None:
        if not self.current_text.strip():
            self.last_keypress_at = None
            return

        self.saved_blocks.append(self.current_text)
        self.current_text = ""
        self.last_keypress_at = None
        editor = self.query_one("#editor", TextArea)
        editor.load_text("")

    def _refresh_status(self) -> None:
        title = self.query_one("#title", Static)
        if self.in_review_mode:
            word_count = self._get_word_count(self._get_review_text())
            title.update(
                "Type Don't Think"
                " | "
                "review"
                " | "
                f"{self._format_elapsed_time(self.review_elapsed_ms)}"
                " | "
                f"{word_count} words"
            )
            return

        if self.last_keypress_at is None or not self.current_text.strip():
            remaining_ms = self.delay_ms
        else:
            elapsed_ms = int((monotonic() - self.last_keypress_at) * 1000)
            remaining_ms = max(0, self.delay_ms - elapsed_ms)

        title.update(
            "Type Don't Think"
            " | "
            "input "
            " | "
            f"{remaining_ms / 1000:.1f}s"
        )

    def _refresh_review(self) -> None:
        review = self.query_one("#review", ReviewTextArea)
        review.load_text(self._get_review_text())

    def _get_review_text(self) -> str:
        parts = [*self.saved_blocks]
        if self.current_text:
            parts.append(self.current_text)
        return "\n".join(parts)

    def _format_elapsed_time(self, elapsed_ms: int) -> str:
        total_seconds = max(0, elapsed_ms // 1000)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _get_word_count(self, text: str) -> int:
        return len(text.split())

    def copy_session_to_clipboard(self) -> None:
        text = self._get_review_text()
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
    redirected_stdout_fd: int | None = None
    tty_stream = None

    if should_emit_final_output:
        try:
            redirected_stdout_fd = os.dup(sys.stdout.fileno())
            tty_stream = open("/dev/tty", "w", encoding=sys.stdout.encoding or "utf-8")
            sys.stdout.flush()
            os.dup2(tty_stream.fileno(), sys.stdout.fileno())
        except OSError:
            if redirected_stdout_fd is not None:
                os.close(redirected_stdout_fd)
                redirected_stdout_fd = None

    app = TypeDontThinkTUI(no_review=args.no_review, delay_seconds=args.delay)
    app.run()

    if redirected_stdout_fd is not None:
        sys.stdout.flush()
        os.dup2(redirected_stdout_fd, sys.stdout.fileno())
        os.close(redirected_stdout_fd)
        sys.stdout = os.fdopen(sys.stdout.fileno(), "w", encoding="utf-8", closefd=False)

    if tty_stream is not None:
        tty_stream.close()

    if should_emit_final_output and app.final_output:
        sys.stdout.write(app.final_output)
        if not app.final_output.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
