"""Audit records on Zova's public SQL API, confined to the event-loop thread."""

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

from ..model.engine import Prediction


class AuditStore:
    def __init__(self, path, *, retain_input=False):
        import zova

        self.zova = zova
        self.retain_input = retain_input
        path = Path(path).expanduser()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise OSError("Audit path cannot be a symlink")
        old_mask = os.umask(0o077)
        try:
            if path.exists():
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise OSError("Audit file must be owned by this user")
                path.chmod(0o600)
                self.db = zova.Database.open(str(path))
            else:
                self.db = zova.Database.create(str(path))
            self.db.set_busy_timeout(1000)
            self.db.exec(
                "CREATE TABLE IF NOT EXISTS mayi_audit ("
                "id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, "
                "decision TEXT NOT NULL, record TEXT NOT NULL)"
            )
            self.db.exec(
                "CREATE TABLE IF NOT EXISTS mayi_events (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, kind TEXT NOT NULL, record TEXT NOT NULL)"
            )
            self.db.exec(
                "CREATE TABLE IF NOT EXISTS mayi_prompts ("
                "id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                "ordinal INTEGER NOT NULL, record TEXT NOT NULL)"
            )
        finally:
            os.umask(old_mask)

    def record(self, request_id, request, result, prediction):
        binary = prediction if isinstance(prediction, Prediction) else None
        record = {
            "id": request_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "agent": request.agent if request else None,
            "tool": request.tool if request else None,
            "operation": request.operation if request else None,
            "cwd": request.cwd if request else None,
            **result.to_dict(),
            "model_choice": binary.choice if binary else None,
            "model_approve_probability": binary.approve_probability if binary else None,
            "model_hold_probability": binary.hold_probability if binary else None,
            "human_decision": None,
            "metadata": request.metadata if request else {},
        }
        if self.retain_input and request:
            record["input"] = request.input
            record["reason"] = request.reason
        with self.db.prepare("INSERT INTO mayi_audit VALUES (?1, ?2, ?3, ?4)") as stmt:
            for index, value in enumerate(
                (
                    request_id,
                    record["timestamp"],
                    result.decision,
                    json.dumps(record, allow_nan=False),
                ),
                1,
            ):
                stmt.bind_text(index, value)
            stmt.step()

    def logs(self, *, decision=None, limit=100):
        if not 1 <= limit <= 10000:
            raise ValueError("Log limit must be between 1 and 10000")
        sql = "SELECT record FROM mayi_audit"
        if decision is not None:
            sql += " WHERE decision = ?1"
        sql += " ORDER BY timestamp DESC, id DESC LIMIT " + str(int(limit))
        with self.db.prepare(sql) as stmt:
            if decision is not None:
                stmt.bind_text(1, decision)
            rows = []
            while stmt.step() == self.zova.Step.ROW:
                rows.append(json.loads(stmt.column_text(0)))
            return rows

    def has_request(self, request_id):
        with self.db.prepare("SELECT id FROM mayi_audit WHERE id = ?1") as stmt:
            stmt.bind_text(1, request_id)
            return stmt.step() == self.zova.Step.ROW

    def record_event(self, event):
        event = {
            **event,
            "timestamp": event.get("timestamp", datetime.now(UTC).isoformat()),
        }
        with self.db.prepare("INSERT INTO mayi_events VALUES (?1, ?2, ?3, ?4)") as stmt:
            for index, value in enumerate(
                (
                    event["id"],
                    event["timestamp"],
                    event["kind"],
                    json.dumps(event, allow_nan=False),
                ),
                1,
            ):
                stmt.bind_text(index, value)
            stmt.step()

    def events(self, *, limit=100):
        if not 1 <= limit <= 10000:
            raise ValueError("Log limit must be between 1 and 10000")
        with self.db.prepare(
            "SELECT record FROM mayi_events ORDER BY timestamp DESC, id DESC LIMIT "
            + str(int(limit))
        ) as stmt:
            rows = []
            while stmt.step() == self.zova.Step.ROW:
                rows.append(json.loads(stmt.column_text(0)))
            return rows

    def record_prompt(self, record):
        with self.db.prepare("SELECT record FROM mayi_prompts WHERE id = ?1") as stmt:
            stmt.bind_text(1, record["id"])
            if stmt.step() == self.zova.Step.ROW:
                saved = json.loads(stmt.column_text(0))
                identity = ("id", "session_id", "turn_id", "cwd", "prompt")
                if all(saved[key] == record[key] for key in identity):
                    return saved
                raise ValueError("Conflicting prompt delivery")
        with self.db.prepare("SELECT MAX(ordinal) FROM mayi_prompts") as stmt:
            stmt.step()
            ordinal = (stmt.column_int(0) or 0) + 1
        record = {**record, "ordinal": ordinal}
        with self.db.prepare(
            "INSERT INTO mayi_prompts VALUES (?1, ?2, ?3, ?4)"
        ) as stmt:
            stmt.bind_text(1, record["id"])
            stmt.bind_text(2, record["session_id"])
            stmt.bind_int(3, ordinal)
            stmt.bind_text(4, json.dumps(record, allow_nan=False))
            stmt.step()
        return record

    def prompts(self, session_id, *, limit=65):
        if not 1 <= limit <= 65:
            raise ValueError("Invalid prompt limit")
        with self.db.prepare(
            "SELECT record FROM mayi_prompts WHERE session_id = ?1 "
            "ORDER BY ordinal ASC LIMIT " + str(int(limit))
        ) as stmt:
            stmt.bind_text(1, session_id)
            rows = []
            while stmt.step() == self.zova.Step.ROW:
                rows.append(json.loads(stmt.column_text(0)))
            return rows

    def close(self):
        self.db.close()
