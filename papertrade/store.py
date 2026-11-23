import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from .models import Account, Position, Side

SCHEMA_VERSION = 1


def save_account(path: str | Path, account: Account) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, "account": asdict(account)}
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True, indent=2, allow_nan=False)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load_account(path: str | Path, starting_cash: float = 100.0) -> Account:
    path = Path(path)
    if not path.exists():
        return Account(starting_cash, starting_cash, starting_cash, "")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported state schema")
        d = data["account"]
        positions = {}
        for ticker, p in d.get("positions", {}).items():
            p["side"] = Side(p["side"])
            positions[ticker] = Position(**p)
        return Account(cash=d["cash"], peak_equity=d["peak_equity"], day_start_equity=d["day_start_equity"],
                       day=d["day"], daily_halt=d.get("daily_halt", False), risk_halt=d.get("risk_halt", False),
                       positions=positions, fills=d.get("fills", []), last_error=d.get("last_error"))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"state recovery refused; file may be corrupt: {exc}") from exc
