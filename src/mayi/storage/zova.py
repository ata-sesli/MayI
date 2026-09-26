"""Audit records on Zova's public SQL API, confined to the event-loop thread."""

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path


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
        finally:
            os.umask(old_mask)

    def record(self, request_id, request, result, prediction):
        record = {
            "id": request_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "agent": request.agent if request else None,
            "tool": request.tool if request else None,
            "operation": request.operation if request else None,
            "cwd": request.cwd if request else None,
            **result.to_dict(),
            "julia_choice": prediction.choice if prediction else None,
            "julia_approve_probability": prediction.approve_probability
            if prediction
            else None,
            "julia_hold_probability": prediction.hold_probability
            if prediction
            else None,
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

    def close(self):
        self.db.close()
