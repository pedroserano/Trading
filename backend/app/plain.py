"""Plain-language settings: explanations whose numbers and switches are the settings.

The scanner's and automanage's step lists are written once, as text with tokens where a
setting's value goes:

    ⟦key⟧          a setting on this form: a number, time or choice, edited in place
    ⟦?key⟧         a switch on this form: a checkbox that starts the sentence it turns on
    ⟦rules:key⟧    a value set on the Rules tab: shown, and linked there
    ⟦link:text|to⟧ a link to another part of Settings (to: a tab, or an element id)

as_text() fills the tokens with values (for the API and tests); as_html() turns them into
inputs for the Settings page, so the explanation is the form.
"""

import re
from dataclasses import dataclass, field

from markupsafe import Markup, escape

TOKEN = re.compile(r"⟦([^⟧]+)⟧")


@dataclass
class Field:
    kind: str  # "int", "float", "percent", "delta", "time", "select", "check"
    value: object
    default: object = None
    lo: float | None = None
    hi: float | None = None
    options: list[tuple[str, str]] = field(default_factory=list)
    hint: str = ""
    id: str = ""  # the element id, when not prefix-key


def fmt(f: Field, v) -> str:
    if f.kind == "check":
        return "on" if v else "off"
    if f.kind == "select":
        return next((label for k, label in f.options if k == v), str(v))
    if f.kind == "percent":
        return f"{round(float(v) * 100, 2):g}"
    if f.kind == "int":
        return f"{int(v):,}" if abs(int(v)) >= 10_000 else str(int(v))
    if f.kind in ("float", "delta"):
        return f"{float(v):g}" if f.kind == "float" else f"{float(v):.2f}"
    return str(v)


def as_text(text: str, fields: dict[str, Field], rules: dict[str, str] | None = None) -> str:
    """The explanation with every token replaced by its current value."""

    def sub(m):
        tok = m.group(1)
        if tok.startswith("link:"):
            return tok[5:].split("|")[0]
        if tok.startswith("rules:"):
            return (rules or {}).get(tok[6:], "?")
        if tok.startswith("?"):
            return ""  # a switch reads as part of its sentence
        f = fields.get(tok)
        return fmt(f, f.value) if f else "?"

    return re.sub(r"\s+", " ", TOKEN.sub(sub, text)).strip()


def _attr(v) -> str:
    return str(escape(v))


def _input(name: str, f: Field, prefix: str) -> str:
    title = f' title="{_attr(f.hint)}"' if f.hint else ""
    ident = f'id="{_attr(f.id or f"{prefix}-{name}")}" '
    if f.kind == "check":
        on = " checked" if f.value else ""
        d = "1" if f.default else "0"
        return (
            f'<input {ident}class="inl inl-check" type="checkbox" name="{name}"{on} '
            f'data-default="{d}" aria-label="{_attr(name)}"{title}>'
        )
    if f.kind == "select":
        opts = "".join(
            f'<option value="{_attr(k)}"{" selected" if k == f.value else ""}>{escape(label)}'
            "</option>"
            for k, label in f.options
        )
        return (
            f'<select {ident}class="inl" name="{name}" data-default="{_attr(f.default)}" '
            f'aria-label="{_attr(name)}"{title}>{opts}</select>'
        )
    if f.kind == "time":
        return (
            f'<input {ident}class="inl inl-time" type="time" name="{name}" '
            f'value="{_attr(f.value)}" '
            f'data-default="{_attr(f.default)}" required aria-label="{_attr(name)}"{title}>'
        )
    value = fmt(f, f.value).replace(",", "")
    default = fmt(f, f.default).replace(",", "") if f.default is not None else ""
    lo = "" if f.lo is None else f' min="{f.lo:g}"'
    hi = "" if f.hi is None else f' max="{f.hi:g}"'
    step = "1" if f.kind == "int" else "any"
    width = max(len(value), len(default), 2) + 2.5
    return (
        f'<input {ident}class="inl" type="number" name="{name}" value="{value}" '
        f'data-default="{default}" step="{step}"{lo}{hi} required '
        f'style="width:{width}ch" aria-label="{_attr(name)}"{title}>'
    )


def as_html(
    text: str,
    fields: dict[str, Field],
    rules: dict[str, tuple[str, str]] | None = None,
    prefix: str = "x",
) -> Markup:
    """The explanation with every token turned into its input or link, the words escaped.
    rules: key -> (shown value, link target id)."""
    out, pos = [], 0
    for m in TOKEN.finditer(text):
        out.append(str(escape(text[pos : m.start()])))
        tok = m.group(1)
        if tok.startswith("link:"):
            label, _, to = tok[5:].partition("|")
            out.append(f'<a href="#/settings/{_attr(to or label)}">{escape(label)}</a>')
        elif tok.startswith("rules:"):
            shown, to = (rules or {}).get(tok[6:], ("?", "rules"))
            out.append(
                f'<a class="inl-rule" href="#/settings/{_attr(to)}" '
                f'title="Set on the Rules tab">{escape(shown)}</a>'
            )
        else:
            key = tok.lstrip("?")
            f = fields.get(key)
            out.append(_input(key, f, prefix) if f else "?")
        pos = m.end()
    out.append(str(escape(text[pos:])))
    # Every word is escaped above and every value goes through escape(): safe to mark.
    return Markup("".join(out))  # noqa: S704
