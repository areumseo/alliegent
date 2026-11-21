"""Following up on an /ask answer by replying to it: the conversation is kept,
survives a restart, and a reply never reaches the agenda's chat agent."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from alliegent import ask
from alliegent.agenda import AgendaService
from alliegent.config import Config, Secrets
from alliegent.integrations.discord_bot import AlliegentBot

from .conftest import FakeNotionClient

# -- remembering answers -----------------------------------------------------------


def test_an_answer_is_remembered_with_its_conversation(tmp_path):
    threads = ask.Threads(tmp_path / "t.json")
    threads.remember([11, 12], [("q1", "a1")])
    assert threads.get(11) == [("q1", "a1")]
    assert threads.get(12) == [("q1", "a1")]   # an answer sent in two parts
    assert threads.get(99) is None


def test_it_survives_a_restart(tmp_path):
    path = tmp_path / "data" / "t.json"
    ask.Threads(path).remember([11], [("q1", "a1")])
    assert ask.Threads(path).get(11) == [("q1", "a1")]


def test_only_the_newest_answers_are_kept(tmp_path):
    threads = ask.Threads(tmp_path / "t.json", limit=2)
    for i in range(3):
        threads.remember([i], [(f"q{i}", f"a{i}")])
    assert threads.get(0) is None and threads.get(2) is not None


def test_a_damaged_file_starts_afresh(tmp_path):
    path = tmp_path / "t.json"
    path.write_text("{not json")
    assert ask.Threads(path).get(1) is None


def test_long_conversations_keep_their_latest_turns(tmp_path):
    threads = ask.Threads(tmp_path / "t.json")
    turns = [(f"q{i}", f"a{i}") for i in range(10)]
    threads.remember([1], turns)
    kept = threads.get(1)
    assert len(kept) == ask.MAX_TURNS and kept[-1] == ("q9", "a9")
    assert json.loads((tmp_path / "t.json").read_text())


# -- what the model is sent ---------------------------------------------------------


def test_the_earlier_turns_go_before_the_new_question():
    messages = ask.conversation([("q1", "a1")], "q2")
    assert messages == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]


async def test_answer_sends_the_conversation():
    class Client:
        def __init__(self):
            self.messages = self

        async def create(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(stop_reason="end_turn",
                                   content=[SimpleNamespace(type="text", text="ok")])

    from datetime import date

    client = Client()
    await ask.answer("그럼 언제?", "(reports)", api_key="k", today=date(2026, 11, 21),
                     kind=None, turns=[("뭐라고 했지?", "9900원으로 정했어요.")], client=client)
    assert [m["role"] for m in client.kwargs["messages"]] == ["user", "assistant", "user"]
    assert client.kwargs["messages"][-1]["content"] == "그럼 언제?"


# -- the bot ---------------------------------------------------------------------------


def make_bot(tmp_path, monkeypatch) -> AlliegentBot:
    client = FakeNotionClient()
    config = Config()
    bot = AlliegentBot(
        config=config,
        agenda=AgendaService(client, config, "agenda-db"),
        projects=None,
        secrets=Secrets(discord_channel_id=123, anthropic_api_key="sk-test"),
    )
    bot.ask_threads = ask.Threads(tmp_path / "t.json")
    monkeypatch.setattr(AlliegentBot, "user", property(lambda self: SimpleNamespace(
        id=999, mentioned_in=lambda m: True)))
    return bot


class Message:
    _next = 1000

    def __init__(self, content, reply_to=None):
        self.content = content
        self.author = SimpleNamespace(bot=False)
        self.mention_everyone = False
        self.reference = SimpleNamespace(message_id=reply_to) if reply_to else None
        self.replies: list[str] = []

        @asynccontextmanager
        async def typing():
            yield

        self.channel = SimpleNamespace(id=123, parent_id=None, typing=typing)

    async def reply(self, text, **_):
        self.replies.append(text)
        Message._next += 1
        return SimpleNamespace(id=Message._next)


async def test_a_reply_to_an_answer_carries_the_conversation_on(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch)
    bot.ask_threads.remember([500], [("뭐라고 했지?", "9900원이요.")])
    seen = {}

    async def fake_ask(channel, question, turns=None):
        seen.update(question=question, turns=turns)
        return "다음 주부터요."

    async def no_chat(*_):
        raise AssertionError("a follow-up must not reach the agenda's chat agent")

    bot.ask = fake_ask
    bot.chat.respond = no_chat
    message = Message("<@999> 그럼 언제부터?", reply_to=500)
    await bot.on_message(message)

    assert seen == {"question": "그럼 언제부터?", "turns": [("뭐라고 했지?", "9900원이요.")]}
    assert message.replies == ["다음 주부터요."]
    # And the new answer can be replied to in turn.
    assert bot.ask_threads.get(Message._next) == [
        ("뭐라고 했지?", "9900원이요."), ("그럼 언제부터?", "다음 주부터요."),
    ]


async def test_a_reply_to_any_other_message_is_the_chat_agents(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch)
    got = []

    async def fake_ask(*_a, **_k):
        raise AssertionError("not an /ask answer")

    async def chat(channel_id, text):
        got.append(text)
        return "ok"

    bot.ask = fake_ask
    bot.chat.respond = chat
    await bot.on_message(Message("<@999> add groceries", reply_to=777))
    assert got == ["add groceries"]


async def test_a_failing_follow_up_says_so(tmp_path, monkeypatch):
    bot = make_bot(tmp_path, monkeypatch)
    bot.ask_threads.remember([500], [("q", "a")])

    async def broken(*_a, **_k):
        raise RuntimeError("boom")

    bot.ask = broken
    message = Message("and then?", reply_to=500)
    await bot.on_message(message)
    assert message.replies == ["⚠️ I couldn't answer that just now."]
