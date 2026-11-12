"""Cross-profile coordination state in the shared global database.

Only two rows are shared between profiles (``app-notifications.md`` §6.2):

* ``reminder_lease`` elects exactly one delivery owner for global-calendar
  reminders. The lease uses the same ``BEGIN IMMEDIATE`` conditional-update
  pattern as the sync lease: a 180 s TTL and a 60 s takeover grace so a suspended
  host does not lose its lease immediately.
* ``reminder_delivery_ledger`` stores content-free dedup keys that have already
  produced a user-visible message. It lets a takeover holder skip a reminder the
  previous holder sent but did not get to mark delivered.

Neither table is notification content. ``core.md`` §3.7/§5.11 say the global
store has no notification state; this is coordination state, and
``docs/DECISIONS.md`` records that reconciliation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

GLOBAL_LEASE_SCOPE = "global"


@dataclass(frozen=True)
class LeaseState:
    """One row of the global reminder delivery lease."""

    owner_profile_id: str
    lease_expires_utc: int
    heartbeat_utc: int


def read_lease(connection: sqlite3.Connection) -> LeaseState | None:
    """Return the current global reminder lease, or ``None`` when unheld."""
    row = connection.execute(
        """
        SELECT owner_profile_id, lease_expires_utc, heartbeat_utc
          FROM reminder_lease
         WHERE scope = ?
        """,
        (GLOBAL_LEASE_SCOPE,),
    ).fetchone()
    if row is None:
        return None
    return LeaseState(
        owner_profile_id=str(row["owner_profile_id"]),
        lease_expires_utc=int(row["lease_expires_utc"]),
        heartbeat_utc=int(row["heartbeat_utc"]),
    )


def acquire_or_renew_lease(
    connection: sqlite3.Connection,
    *,
    profile_id: str,
    now: int,
    ttl_seconds: int = 180,
    takeover_grace_seconds: int = 60,
) -> bool:
    """Acquire or renew the global delivery lease for ``profile_id``.

    Returns ``True`` when the caller is the delivery owner for this tick. A
    different owner is taken over only after its lease has been expired for the
    grace period; until then this profile must not materialize or deliver global
    reminders. The conditional update runs inside ``BEGIN IMMEDIATE`` so two
    profile processes cannot both become owner.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute(
            """
            SELECT owner_profile_id, lease_expires_utc
              FROM reminder_lease
             WHERE scope = ?
            """,
            (GLOBAL_LEASE_SCOPE,),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO reminder_lease
                  (scope, owner_profile_id, lease_expires_utc, heartbeat_utc)
                VALUES (?, ?, ?, ?)
                """,
                (GLOBAL_LEASE_SCOPE, profile_id, now + ttl_seconds, now),
            )
            connection.execute("COMMIT")
            return True

        owner = str(row["owner_profile_id"])
        expires = int(row["lease_expires_utc"])
        if owner == profile_id:
            connection.execute(
                """
                UPDATE reminder_lease
                   SET lease_expires_utc = ?, heartbeat_utc = ?
                 WHERE scope = ? AND owner_profile_id = ?
                """,
                (now + ttl_seconds, now, GLOBAL_LEASE_SCOPE, profile_id),
            )
            connection.execute("COMMIT")
            return True

        if now > expires + takeover_grace_seconds:
            cursor = connection.execute(
                """
                UPDATE reminder_lease
                   SET owner_profile_id = ?, lease_expires_utc = ?,
                       heartbeat_utc = ?
                 WHERE scope = ? AND owner_profile_id = ?
                   AND lease_expires_utc = ?
                """,
                (
                    profile_id,
                    now + ttl_seconds,
                    now,
                    GLOBAL_LEASE_SCOPE,
                    owner,
                    expires,
                ),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == 1

        connection.execute("COMMIT")
        return False
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def ledger_contains(connection: sqlite3.Connection, dedup_key: str) -> bool:
    """Return whether a global reminder with this key was already delivered."""
    row = connection.execute(
        "SELECT 1 FROM reminder_delivery_ledger WHERE dedup_key = ?",
        (dedup_key,),
    ).fetchone()
    return row is not None


def record_ledger(
    connection: sqlite3.Connection,
    *,
    dedup_key: str,
    now: int,
    profile_id: str,
) -> None:
    """Record a successfully delivered global reminder without any content."""
    connection.execute(
        """
        INSERT INTO reminder_delivery_ledger (dedup_key, delivered_utc, profile_id)
        VALUES (?, ?, ?)
        ON CONFLICT (dedup_key) DO NOTHING
        """,
        (dedup_key, now, profile_id),
    )
