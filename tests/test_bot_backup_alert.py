from __future__ import annotations

from types import SimpleNamespace

from dnd_bot.bot import DnDBot


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send_dm(self, user_id, content):
        self.sent.append((user_id, content))
        return True


def make(now):
    return SimpleNamespace(
        config=SimpleNamespace(admin_user_id=999),
        notifier=FakeNotifier(),
        _last_backup_alert=None,
        _monotonic=lambda: now["t"],
    )


async def test_first_failure_sends_dm_without_paths():
    now = {"t": 1000.0}
    stub = make(now)
    await DnDBot._report_backup_failure(stub, OSError("disk full at /data/backups/x.db"))
    assert len(stub.notifier.sent) == 1
    user_id, content = stub.notifier.sent[0]
    assert user_id == 999
    assert "/data" not in content
    assert "OSError" in content


async def test_second_failure_within_20h_is_suppressed():
    now = {"t": 1000.0}
    stub = make(now)
    await DnDBot._report_backup_failure(stub, OSError("x"))
    now["t"] += 3600
    await DnDBot._report_backup_failure(stub, OSError("x"))
    assert len(stub.notifier.sent) == 1


async def test_failure_after_20h_alerts_again():
    now = {"t": 1000.0}
    stub = make(now)
    await DnDBot._report_backup_failure(stub, OSError("x"))
    now["t"] += 20 * 3600 + 1
    await DnDBot._report_backup_failure(stub, OSError("x"))
    assert len(stub.notifier.sent) == 2
