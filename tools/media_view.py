"""Helpers for reading the text results of the image tool."""


def labeled_url(text: str, label: str = "URL") -> str:
    """Pull `<label>: https://...` out of a text tool output, or ''."""
    prefix = f"{label}: "
    for line in text.splitlines():
        if line.startswith(prefix) and line[len(prefix):].strip().startswith(("http://", "https://")):
            return line[len(prefix):].strip()
    return ""
