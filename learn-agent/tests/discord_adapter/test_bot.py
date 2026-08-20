import asyncio

from discord_adapter.bot import (
    BUSY_MESSAGE,
    RATE_LIMITED_MESSAGE,
    RESET_CONFIRMATION,
    TOO_LONG_MESSAGE_TEMPLATE,
    chunk_message,
    convert_markdown_tables,
    format_status,
    handle_incoming,
)
from discord_adapter.config import DiscordConfig


class FakeSession:
    def __init__(self, reply: str = "the answer"):
        self.reply = reply
        self.asked: list[str] = []
        self.reset_called = False
        self._status = {
            "backend": "fake",
            "model": "fake-model",
            "state": "running",
            "message_count": 1,
            "total_tokens": 0,
        }

    async def ask(self, text: str) -> str:
        self.asked.append(text)
        return self.reply

    def reset(self) -> None:
        self.reset_called = True

    def status(self) -> dict:
        return self._status


class AlwaysAllowRateLimiter:
    def allow(self) -> bool:
        return True


class AlwaysDenyRateLimiter:
    def allow(self) -> bool:
        return False


def make_config(allowed_user_id: int = 42) -> DiscordConfig:
    return DiscordConfig(
        bot_token="fake",
        allowed_user_id=allowed_user_id,
        max_messages_per_minute=5,
        max_messages_per_hour=50,
    )


def test_ignores_non_dm_messages():
    session = FakeSession()
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=False,
            text="what's due this week?",
        )
    )
    assert replies == []
    assert session.asked == []


def test_ignores_messages_from_unauthorized_user():
    session = FakeSession()
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(allowed_user_id=42),
            author_id=999,
            is_dm=True,
            text="what's due this week?",
        )
    )
    assert replies == []
    assert session.asked == []


def test_rejects_message_over_length_limit():
    session = FakeSession()
    long_text = "x" * 5000
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text=long_text,
        )
    )
    assert replies == [TOO_LONG_MESSAGE_TEMPLATE.format(length=5000)]
    assert session.asked == []


def test_rate_limited_message_never_reaches_session():
    session = FakeSession()
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysDenyRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="what's due this week?",
        )
    )
    assert replies == [RATE_LIMITED_MESSAGE]
    assert session.asked == []


def test_reset_command_handled_without_calling_session_ask():
    session = FakeSession()
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="!reset",
        )
    )
    assert replies == [RESET_CONFIRMATION]
    assert session.reset_called is True
    assert session.asked == []


def test_status_command_handled_without_calling_session_ask():
    session = FakeSession()
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="!status",
        )
    )
    assert replies == [format_status(session.status())]
    assert session.asked == []


def test_busy_lock_rejects_concurrent_message():
    session = FakeSession()
    lock = asyncio.Lock()

    async def scenario():
        await lock.acquire()
        try:
            return await handle_incoming(
                session=session,
                rate_limiter=AlwaysAllowRateLimiter(),
                lock=lock,
                config=make_config(),
                author_id=42,
                is_dm=True,
                text="what's due this week?",
            )
        finally:
            lock.release()

    replies = asyncio.run(scenario())
    assert replies == [BUSY_MESSAGE]
    assert session.asked == []


def test_ordinary_question_is_asked_and_chunked():
    session = FakeSession(reply="short answer")
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="what's due this week?",
        )
    )
    assert session.asked == ["what's due this week?"]
    assert replies == ["short answer"]


def test_ordinary_question_with_table_reply_is_converted_before_chunking():
    session = FakeSession(
        reply=(
            "Deadlines:\n\n"
            "| Assignment | Due Date |\n"
            "| :--- | :--- |\n"
            "| Lab1 | May 16, 2026 |\n"
            "| Project | August 14, 2026 |\n"
        )
    )
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="what's due?",
        )
    )
    assert len(replies) == 1
    assert "|" not in replies[0]
    assert "```" in replies[0]


def test_convert_markdown_tables_leaves_non_table_text_untouched():
    text = "Just a normal reply with no pipes or tables."
    assert convert_markdown_tables(text) == text


def test_convert_markdown_tables_wraps_table_in_code_block_and_aligns_columns():
    text = (
        "Deadlines:\n\n"
        "| Assignment | Due Date |\n"
        "| :--- | :--- |\n"
        "| Lab1_Take-Home Activity | May 16, 2026 |\n"
        "| Project | August 14, 2026 |\n"
        "\nLet me know if you need more."
    )
    result = convert_markdown_tables(text)

    assert "|" not in result
    assert result.count("```") == 2
    assert "Assignment" in result
    assert "Lab1_Take-Home Activity" in result
    assert "Let me know if you need more." in result
    # code block content should be padded/aligned, not raw pipe cells
    code_block = result.split("```")[1]
    lines = [line for line in code_block.strip("\n").splitlines()]
    header_col_start = lines[0].index("Due Date")
    row_col_start = lines[2].index("May 16, 2026")
    assert header_col_start == row_col_start


def test_convert_markdown_tables_handles_multiple_tables():
    text = (
        "| A | B |\n"
        "|---|---|\n"
        "| 1 | 2 |\n"
        "\ntext between\n\n"
        "| C | D |\n"
        "|---|---|\n"
        "| 3 | 4 |\n"
    )
    result = convert_markdown_tables(text)
    assert result.count("```") == 4
    assert "text between" in result
    assert "|" not in result


def test_empty_reply_from_session_ask_becomes_no_response_placeholder():
    session = FakeSession(reply="")
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="what's due this week?",
        )
    )
    assert replies == ["(no response)"]


def test_whitespace_only_reply_from_session_ask_becomes_no_response_placeholder():
    session = FakeSession(reply="   \n  ")
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="what's due this week?",
        )
    )
    assert replies == ["(no response)"]


def test_empty_message_returns_no_replies_and_never_calls_session_ask():
    session = FakeSession()
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="",
        )
    )
    assert replies == []
    assert session.asked == []


def test_whitespace_only_message_returns_no_replies_and_never_calls_session_ask():
    session = FakeSession()
    replies = asyncio.run(
        handle_incoming(
            session=session,
            rate_limiter=AlwaysAllowRateLimiter(),
            lock=asyncio.Lock(),
            config=make_config(),
            author_id=42,
            is_dm=True,
            text="   \n\t  ",
        )
    )
    assert replies == []
    assert session.asked == []


def test_chunk_message_splits_long_text_without_exceeding_limit():
    text = "word " * 1000  # 5000 chars
    chunks = chunk_message(text, limit=100)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_chunk_message_returns_single_chunk_when_short():
    assert chunk_message("hello") == ["hello"]


def test_format_status_never_includes_course_data():
    status = {
        "backend": "gemini",
        "model": "gemini-3.1-flash-lite",
        "state": "running",
        "message_count": 4,
        "total_tokens": 1234,
    }
    text = format_status(status)
    assert "gemini" in text
    assert "1,234" in text
    assert "running" in text
