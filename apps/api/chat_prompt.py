"""Shared assistant instructions for Web and Desktop Chat."""
from datetime import datetime
from pathlib import Path

CHAT_PROMPT = (Path(__file__).resolve().parents[1] / "ui" / "system_prompt.md").read_text(encoding="utf-8-sig")


def assistant_system_prompt() -> str:
    now = datetime.now().astimezone()
    weekday_vi = ("Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm", "Thứ Sáu", "Thứ Bảy", "Chủ nhật")[now.weekday()]
    return (CHAT_PROMPT + f"\n\nRuntime server date and time: {now.isoformat(timespec='minutes')}. "
              f"Runtime weekday: {now:%A} ({weekday_vi}). "
              "For questions about today's date or time, use these supplied values directly. "
              "Use 24-hour time and only state the weekday or time when asked. "
              "This is the server clock, not the publication or verification time of a source. "
              "The examples above are formatting examples, not sources. "
              "Image retrieval is unavailable in this chat. Only cite URLs verified in this turn. "
              "When no retrieval tool is provided, explicitly say it is unavailable if verification is needed.")
