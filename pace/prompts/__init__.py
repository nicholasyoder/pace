"""Prompt templates: `<name>.md` files with `{{variable}}` placeholders."""

import re
from pathlib import Path

_DIR = Path(__file__).parent


def render(name: str, **values: str) -> str:
    text = (_DIR / f"{name}.md").read_text()

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in values:
            raise KeyError(f"prompt {name}: missing {{{{{key}}}}}")
        return values[key]

    return re.sub(r"\{\{(\w+)\}\}", sub, text).strip() + "\n"
