"""Notion rich_text items → HTML 转换器

直接将 Notion API 的 rich_text 数组转为 HTML，无需 Markdown 中间态。
用于 create_draft 路径：webhook 传入 raw blocks → 本地直接生成 HTML → 剪贴板。
"""

_COLOR_MAP = {
    "gray": "#787774", "brown": "#9F6B53", "orange": "#D9730D",
    "yellow": "#CB912F", "green": "#448361", "blue": "#337EA9",
    "purple": "#9065B0", "pink": "#C14C8A", "red": "#D44C47",
}


def rich_text_to_html(items: list, font_size: int = 14) -> str:
    if not items:
        return ""
    parts = []
    for item in items:
        text = item.get("text", {}).get("content", "")
        if not text:
            continue
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        text = text.replace("\n", "<br>")

        ann = item.get("annotations", {})
        link = item.get("text", {}).get("link")

        if ann.get("code"):
            text = f"<code style='background:#f0f0f0;padding:1px 4px;border-radius:3px'>{text}</code>"
        if ann.get("bold"):
            text = f"<b>{text}</b>"
        if ann.get("italic"):
            text = f"<i>{text}</i>"
        if ann.get("strikethrough"):
            text = f"<s>{text}</s>"
        if ann.get("underline"):
            text = f"<u>{text}</u>"

        color = ann.get("color", "default")
        if color != "default":
            if color.endswith("_background"):
                base = color.replace("_background", "")
                css = _COLOR_MAP.get(base, "")
                if css:
                    text = f'<span style="background-color:{css}20">{text}</span>'
            else:
                css = _COLOR_MAP.get(color, "")
                if css:
                    text = f'<span style="color:{css}">{text}</span>'

        if link and link.get("url"):
            text = f'<a href="{link["url"]}" style="color:#1a73e8">{text}</a>'

        parts.append(text)

    body = "".join(parts)
    return f"<div style='font-family:system-ui,-apple-system;font-size:{font_size}px;line-height:1.6'>{body}</div>"
