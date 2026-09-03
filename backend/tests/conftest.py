"""Shared fixtures and fakes for the backend suite."""

import pytest_asyncio

from app.db.session import Database


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database()
    await database.connect(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    yield database
    await database.close()


class FakeRunner:
    """Journals notes, records intents, and serves a mutable paper book.
    `reporters` (date -> [payload dicts]) backs reporters_for for the
    strategies that ask."""

    def __init__(self, reporters=None):
        self.reporters = reporters or {}
        self.notes: list[dict] = []
        self.intents: list = []
        self.book = {"equity": 10_000.0, "available": 10_000.0}

    async def journal_note(self, _id, detail, signal_ids=()):
        self.notes.append(detail)

    async def execute_intent(self, _id, intent):
        self.intents.append(intent)
        return {"id": "plan-1"}

    async def account(self, _id):
        return self.book

    async def reporters_for(self, date):
        return self.reporters.get(date, [])
