from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude" / "skills" / "autoconfig" / "scripts"))

import experiment_db


def test_get_consecutive_discards_is_phase_aware(monkeypatch) -> None:
    class FakeDb:
        def execute(self, query, params=()):
            if "WHERE phase = ?" in query:
                rows = [
                    {"kept": 0, "status": "completed"},
                    {"kept": 0, "status": "completed"},
                    {"kept": 1, "status": "completed"},
                ]
            else:
                rows = [
                    {"kept": 0, "status": "completed"},
                    {"kept": 0, "status": "completed"},
                    {"kept": 0, "status": "completed"},
                    {"kept": 1, "status": "completed"},
                ]

            class Result:
                def fetchall(self_nonlocal):
                    return rows

            return Result()

    monkeypatch.setattr(experiment_db, "get_db", lambda: FakeDb())

    assert experiment_db.get_consecutive_discards(phase=1) == 2
    assert experiment_db.get_consecutive_discards() == 3
