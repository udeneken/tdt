# tdt - Type Don't Think

Writing tool with a single input area and a visible countdown. If you let the timer run out your text disappears. When you're done, press `ESC` to go to the review. Press `ESC` again to exit. If you want to save the text, pipe it into a file with `tdt > test.txt` or press `c` in review to save it to the clipboard.

After installing use `tdt --help` for some help.

## Run
Run it once without installing:

```sh
uv run python main.py
uv run tdt # or like this
```

## Install
Install the `tdt` command into your terminal with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install .
# uv tool uninstall tdt
```

After that, you can launch it from anywhere with:

```sh
tdt
tdt --no-review
tdt -d 2.5
```

## Piping

Pipe or redirect the final review text:

Examples:

```bash
tdt > test.txt
tdt | grep "idea"
tdt | wc -w
```

By default tdt does not echo to terminal. Use `tdt | cat`.
