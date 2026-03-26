# tdt - Type Don't Think

This tool is great for quick brainstorming, dumping ideas or screaming into the void.

It is a writing tool with a single input area and a visible countdown. Your text only stays visible if you continue typing. When you're done, press `ESC` to go to the review part. If you want to save the text press `c` in review to save it to the clipboard or pipe it into a file with `tdt > test.txt` when starting. Press `ESC` again to exit.

After installing use `tdt --help` for some help.

## Install
Install the `tdt` command into your terminal with [uv](https://docs.astral.sh/uv/).

```sh
uv tool install .
# uv tool uninstall tdt
```

**For Powershell:**
If `tdt` is not recognized after installation, the uv tool bin directory is not available in your current `PATH` yet.

```ps
uv tool update-shell
# $env:Path += ";$(uv tool dir --bin)" # or add to current session
```

## Run
After installing, you can launch it from anywhere with:

```sh
tdt
# tdt --no-review
# tdt -d 2.5
# tdt --sprint 10
# tdt --prompt "Write the worst possible startup pitch"
# tdt --sprint 10 --prompt "Describe the city at dawn"
# tdt --show-time
```

## Sprint And Prompt

Use `--sprint MINUTES` to end the writing session automatically after a fixed writing sprint. The sprint countdown starts with your first input, not when the app opens. By default, countdown and elapsed time displays are hidden from the title bar while the underlying timers stay active. Use `--show-time` to make them visible.

Use `--prompt TEXT` to show a writing prompt above the editor. The prompt is visible during the session but is not included in the exported review text.

## Piping

Pipe or redirect the final review text:

```bash
tdt > test.txt
tdt | grep "idea"
tdt | wc -w
```

By default tdt does not echo to terminal. Use `tdt | cat`.
