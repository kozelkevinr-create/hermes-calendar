"""SQLite DDL for the calendar stores.

This module is the single definition of the SQLite schema. V1 mirrors
``docs/design/core.md`` §5 with these omissions, which are explicitly out of
scope for that milestone: the disposable instance cache (``event_instances``,
``series_state`` and their invalidation triggers), sync-owned tables
(``sync_batches``, ``sync_cursors``, ``sync_conflicts``), full-text search,
notification delivery tables, and custom ``VTIMEZONE`` blobs.

V2 adds the notification-owned tables reconciled in ``core.md`` §5.11 and
``docs/design/app-notifications.md`` §6.5. Per ``core.md`` §3.7 and §5.11 those
tables live in the **per-profile** store only. The shared global store has no
notification content state; it receives only the two coordination tables the
app design needs (``reminder_lease`` and the content-free
``reminder_delivery_ledger``), which ``docs/DECISIONS.md`` records as
coordination state rather than notification state.

Keeping each statement separate (rather than one ``executescript`` call) lets the
migration runner execute the whole schema inside one ``BEGIN IMMEDIATE``
transaction; ``executescript`` implicitly commits first.
"""

from __future__ import annotations

V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE schema_meta (
      key    TEXT PRIMARY KEY,
      value  TEXT NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE sources (
      id                TEXT PRIMARY KEY,
      scope             TEXT NOT NULL DEFAULT 'profile'
                        CHECK (scope IN ('profile', 'global')),
      owner_profile_id  TEXT,
      kind              TEXT NOT NULL
                        CHECK (kind IN ('local', 'caldav', 'google', 'graph', 'ics')),
      display_name      TEXT NOT NULL,
      config_json       TEXT NOT NULL DEFAULT '{}'
                        CHECK (json_valid(config_json)),
      capabilities_json TEXT NOT NULL DEFAULT '{}'
                        CHECK (json_valid(capabilities_json)),
      created_utc       INTEGER NOT NULL,
      updated_utc       INTEGER NOT NULL,
      deleted_utc       INTEGER,
      UNIQUE (id, scope),
      CHECK (scope = 'global' OR owner_profile_id IS NOT NULL),
      CHECK (deleted_utc IS NULL OR deleted_utc >= created_utc)
    )
    """,
    """
    CREATE TABLE calendars (
      id                TEXT PRIMARY KEY,
      source_id         TEXT NOT NULL,
      scope             TEXT NOT NULL DEFAULT 'profile'
                        CHECK (scope IN ('profile', 'global')),
      owner_profile_id  TEXT,
      remote_id         TEXT,
      name              TEXT NOT NULL,
      description       TEXT,
      color             TEXT,
      default_tz        TEXT NOT NULL DEFAULT 'UTC',
      read_only         INTEGER NOT NULL DEFAULT 0 CHECK (read_only IN (0, 1)),
      sync_token        TEXT,
      ctag              TEXT,
      created_utc       INTEGER NOT NULL,
      updated_utc       INTEGER NOT NULL,
      deleted_utc       INTEGER,
      UNIQUE (id, source_id),
      UNIQUE (id, scope),
      UNIQUE (source_id, remote_id),
      CHECK (scope = 'global' OR owner_profile_id IS NOT NULL),
      FOREIGN KEY (source_id, scope)
        REFERENCES sources (id, scope)
        ON UPDATE CASCADE ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE events (
      id                TEXT PRIMARY KEY,
      calendar_id       TEXT NOT NULL,
      source_id         TEXT NOT NULL,
      scope             TEXT NOT NULL DEFAULT 'profile'
                        CHECK (scope IN ('profile', 'global')),
      owner_profile_id  TEXT,
      uid               TEXT NOT NULL,
      remote_href       TEXT,
      etag              TEXT,
      rev               INTEGER NOT NULL DEFAULT 1 CHECK (rev > 0),
      sequence          INTEGER NOT NULL DEFAULT 0 CHECK (sequence >= 0),

      summary           TEXT NOT NULL DEFAULT '',
      description       TEXT,
      location          TEXT,
      url               TEXT,

      start_local       TEXT,
      start_tz          TEXT,
      start_utc         INTEGER,
      end_local         TEXT,
      end_tz            TEXT,
      end_utc           INTEGER,
      duration_seconds  INTEGER,

      start_date        TEXT,
      end_date          TEXT,

      is_all_day        INTEGER NOT NULL DEFAULT 0 CHECK (is_all_day IN (0, 1)),
      is_floating       INTEGER NOT NULL DEFAULT 0 CHECK (is_floating IN (0, 1)),

      rrule             TEXT,
      rrule_unsupported_json TEXT CHECK (rrule_unsupported_json IS NULL
                                         OR json_valid(rrule_unsupported_json)),
      rdate_json        TEXT CHECK (rdate_json IS NULL OR json_valid(rdate_json)),
      exdate_json       TEXT CHECK (exdate_json IS NULL OR json_valid(exdate_json)),
      parent_id         TEXT REFERENCES events(id) ON DELETE CASCADE,
      recurrence_key    TEXT,
      recurrence_id_raw TEXT,
      recurrence_range  TEXT
                        CHECK (recurrence_range IN ('THIS', 'THISANDFUTURE')),

      status            TEXT NOT NULL DEFAULT 'CONFIRMED'
                        CHECK (status IN ('TENTATIVE', 'CONFIRMED', 'CANCELLED')),
      transparency      TEXT NOT NULL DEFAULT 'OPAQUE'
                        CHECK (transparency IN ('OPAQUE', 'TRANSPARENT')),
      classification    TEXT NOT NULL DEFAULT 'PUBLIC'
                        CHECK (classification IN ('PUBLIC', 'PRIVATE', 'CONFIDENTIAL')),

      organizer_email   TEXT,
      organizer_name    TEXT,
      categories_json   TEXT CHECK (categories_json IS NULL
                                    OR json_valid(categories_json)),
      xprops_json       TEXT NOT NULL DEFAULT '{}'
                        CHECK (json_valid(xprops_json)),

      created_utc       INTEGER,
      updated_utc       INTEGER NOT NULL,
      dtstamp_utc       INTEGER,
      last_modified_utc INTEGER,
      deleted_utc       INTEGER,

      FOREIGN KEY (calendar_id, source_id)
        REFERENCES calendars (id, source_id)
        ON UPDATE CASCADE ON DELETE CASCADE,
      FOREIGN KEY (calendar_id, scope)
        REFERENCES calendars (id, scope)
        ON UPDATE CASCADE ON DELETE CASCADE,
      CHECK (scope = 'global' OR owner_profile_id IS NOT NULL),

      CHECK ((parent_id IS NULL AND recurrence_key IS NULL)
          OR (parent_id IS NOT NULL AND recurrence_key IS NOT NULL)),
      CHECK (parent_id IS NULL OR rrule IS NULL),
      CHECK (length(trim(uid)) > 0),
      CHECK (
        (is_all_day = 1
          AND start_date IS NOT NULL
          AND end_date IS NOT NULL
          AND end_date > start_date
          AND start_local IS NULL AND start_tz IS NULL
          AND end_local IS NULL AND end_tz IS NULL
          AND duration_seconds IS NULL)
        OR
        (is_all_day = 0
          AND start_local IS NOT NULL
          AND start_utc IS NOT NULL
          AND end_utc IS NOT NULL
          AND duration_seconds IS NOT NULL
          AND duration_seconds >= 0
          AND end_utc - start_utc = duration_seconds
          AND start_date IS NULL AND end_date IS NULL)
      ),
      CHECK (is_all_day = 1
          OR (is_floating = 1 AND start_tz IS NULL)
          OR (is_floating = 0 AND start_tz IS NOT NULL)),
      CHECK (rrule IS NULL OR length(trim(rrule)) > 0),
      CHECK (recurrence_range IS NULL OR parent_id IS NOT NULL)
    )
    """,
    """
    CREATE TABLE event_attendees (
      event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
      ordinal         INTEGER NOT NULL CHECK (ordinal >= 0),
      address         TEXT NOT NULL,
      email           TEXT,
      name            TEXT,
      role            TEXT NOT NULL DEFAULT 'REQ-PARTICIPANT'
                      CHECK (role IN ('REQ-PARTICIPANT', 'OPT-PARTICIPANT',
                                      'NON-PARTICIPANT', 'CHAIR')),
      partstat        TEXT NOT NULL DEFAULT 'NEEDS-ACTION'
                      CHECK (partstat IN ('NEEDS-ACTION', 'ACCEPTED', 'DECLINED',
                                          'TENTATIVE', 'DELEGATED')),
      rsvp            INTEGER NOT NULL DEFAULT 0 CHECK (rsvp IN (0, 1)),
      cutype          TEXT CHECK (cutype IN ('INDIVIDUAL', 'GROUP', 'RESOURCE',
                                             'ROOM', 'UNKNOWN')),
      delegated_to    TEXT,
      delegated_from  TEXT,
      sent_by         TEXT,
      PRIMARY KEY (event_id, ordinal)
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE event_reminders (
      id                       TEXT PRIMARY KEY,
      event_id                 TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
      action                   TEXT NOT NULL DEFAULT 'DISPLAY'
                               CHECK (action IN ('DISPLAY', 'AUDIO', 'EMAIL')),
      related                  TEXT NOT NULL DEFAULT 'START'
                               CHECK (related IN ('START', 'END')),
      trigger_kind             TEXT NOT NULL
                               CHECK (trigger_kind IN ('RELATIVE', 'ABSOLUTE')),
      offset_seconds           INTEGER,
      absolute_utc             INTEGER,
      repeat_count             INTEGER NOT NULL DEFAULT 0 CHECK (repeat_count >= 0),
      repeat_interval_seconds  INTEGER CHECK (repeat_interval_seconds IS NULL
                                              OR repeat_interval_seconds > 0),
      description              TEXT,
      CHECK (
        (trigger_kind = 'RELATIVE'
          AND offset_seconds IS NOT NULL AND absolute_utc IS NULL)
        OR
        (trigger_kind = 'ABSOLUTE'
          AND absolute_utc IS NOT NULL AND offset_seconds IS NULL)
      ),
      CHECK (repeat_count = 0 OR repeat_interval_seconds IS NOT NULL)
    )
    """,
    """
    CREATE TABLE change_context (
      id       INTEGER PRIMARY KEY CHECK (id = 1),
      origin   TEXT NOT NULL DEFAULT 'local'
               CHECK (origin IN ('local', 'remote', 'system')),
      actor    TEXT,
      batch_id TEXT,
      suppress INTEGER NOT NULL DEFAULT 0 CHECK (suppress IN (0, 1))
    )
    """,
    """
    INSERT INTO change_context (id) VALUES (1)
    """,
    """
    CREATE TABLE change_log (
      seq            INTEGER PRIMARY KEY AUTOINCREMENT,
      at_utc         INTEGER NOT NULL,
      scope          TEXT NOT NULL DEFAULT 'profile'
                     CHECK (scope IN ('profile', 'global')),
      entity_type    TEXT NOT NULL
                     CHECK (entity_type IN ('source', 'calendar', 'event')),
      entity_id      TEXT NOT NULL,
      calendar_id    TEXT,
      op             TEXT NOT NULL CHECK (op IN ('insert', 'update', 'delete')),
      origin         TEXT NOT NULL CHECK (origin IN ('local', 'remote', 'system')),
      actor          TEXT,
      batch_id       TEXT,
      rev            INTEGER,
      before_json    TEXT CHECK (before_json IS NULL OR json_valid(before_json)),
      after_json     TEXT CHECK (after_json IS NULL OR json_valid(after_json)),
      pushed_utc     INTEGER,
      push_error     TEXT,
      CHECK (before_json IS NOT NULL OR after_json IS NOT NULL),
      CHECK (origin <> 'system' OR pushed_utc IS NOT NULL)
    )
    """,
    """
    CREATE INDEX idx_events_range ON events(start_utc, end_utc)
      WHERE deleted_utc IS NULL AND parent_id IS NULL AND rrule IS NULL
    """,
    """
    CREATE INDEX idx_events_dates ON events(start_date, end_date)
      WHERE deleted_utc IS NULL AND is_all_day = 1 AND parent_id IS NULL
    """,
    """
    CREATE INDEX idx_events_series ON events(calendar_id, start_utc)
      WHERE deleted_utc IS NULL AND parent_id IS NULL AND rrule IS NOT NULL
    """,
    """
    CREATE INDEX idx_events_calendar_start ON events(calendar_id, start_utc)
      WHERE deleted_utc IS NULL
    """,
    """
    CREATE INDEX idx_events_uid ON events(uid)
    """,
    """
    CREATE INDEX idx_events_href ON events(calendar_id, remote_href)
      WHERE remote_href IS NOT NULL
    """,
    """
    CREATE INDEX idx_events_parent ON events(parent_id)
      WHERE parent_id IS NOT NULL
    """,
    """
    CREATE INDEX idx_events_updated ON events(updated_utc)
    """,
    """
    CREATE INDEX idx_events_tombstones ON events(deleted_utc)
      WHERE deleted_utc IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX ux_events_uid_master
      ON events(calendar_id, uid)
      WHERE parent_id IS NULL
    """,
    """
    CREATE UNIQUE INDEX ux_calendars_single_global
      ON calendars(scope)
      WHERE scope = 'global'
    """,
    """
    CREATE UNIQUE INDEX ux_events_override
      ON events(parent_id, recurrence_key)
      WHERE parent_id IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX ux_reminders_dedupe ON event_reminders(
      event_id, action, related, trigger_kind,
      COALESCE(offset_seconds, 0), COALESCE(absolute_utc, 0)
    )
    """,
    """
    CREATE INDEX idx_attendees_email ON event_attendees(email)
      WHERE email IS NOT NULL
    """,
    """
    CREATE INDEX idx_reminders_event ON event_reminders(event_id)
    """,
    """
    CREATE INDEX idx_change_entity ON change_log(entity_type, entity_id, seq)
    """,
    """
    CREATE INDEX idx_change_calendar ON change_log(calendar_id, seq)
    """,
    """
    CREATE INDEX idx_change_scope ON change_log(scope, seq)
    """,
    """
    CREATE INDEX idx_change_unpushed ON change_log(seq)
      WHERE origin = 'local' AND pushed_utc IS NULL
    """,
    """
    CREATE VIEW event_view AS
    SELECT
      e.*,
      c.name          AS calendar_name,
      c.default_tz    AS calendar_tz,
      c.read_only     AS calendar_read_only,
      s.kind          AS source_kind,
      s.display_name  AS source_name
    FROM events e
    JOIN calendars c ON c.id = e.calendar_id
    JOIN sources s   ON s.id = e.source_id
    """,
    """
    CREATE VIEW pending_push AS
    SELECT l.scope, l.entity_type, l.entity_id, MAX(l.seq) AS seq
    FROM change_log l
    WHERE l.origin = 'local' AND l.pushed_utc IS NULL
    GROUP BY l.scope, l.entity_type, l.entity_id
    """,
    # Scope guards.
    """
    CREATE TRIGGER trg_sources_scope_guard
    BEFORE INSERT ON sources
    WHEN NEW.scope <> (SELECT value FROM schema_meta WHERE key = 'store_scope')
    BEGIN
      SELECT RAISE(ABORT, 'source scope does not match store scope');
    END
    """,
    """
    CREATE TRIGGER trg_calendars_scope_guard
    BEFORE INSERT ON calendars
    WHEN NEW.scope <> (SELECT value FROM schema_meta WHERE key = 'store_scope')
    BEGIN
      SELECT RAISE(ABORT, 'calendar scope does not match store scope');
    END
    """,
    """
    CREATE TRIGGER trg_sources_scope_immutable
    BEFORE UPDATE OF scope ON sources
    WHEN NEW.scope <> OLD.scope
    BEGIN
      SELECT RAISE(ABORT, 'source scope is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_calendars_scope_immutable
    BEFORE UPDATE OF scope ON calendars
    WHEN NEW.scope <> OLD.scope
    BEGIN
      SELECT RAISE(ABORT, 'calendar scope is immutable');
    END
    """,
    """
    CREATE TRIGGER trg_events_override_guard
    BEFORE INSERT ON events
    WHEN NEW.parent_id IS NOT NULL
    BEGIN
      SELECT CASE
        WHEN NOT EXISTS (
          SELECT 1 FROM events p
          WHERE p.id = NEW.parent_id
            AND p.parent_id IS NULL
            AND p.calendar_id = NEW.calendar_id
            AND p.uid = NEW.uid
        )
        THEN RAISE(ABORT, 'override parent must be a master in the same calendar with the same uid')
      END;
    END
    """,
    """
    CREATE TRIGGER trg_events_no_hard_delete_with_overrides
    BEFORE DELETE ON events
    WHEN EXISTS (SELECT 1 FROM events c WHERE c.parent_id = OLD.id)
      AND (SELECT suppress FROM change_context WHERE id = 1) = 0
    BEGIN
      SELECT RAISE(ABORT, 'delete overrides before purging a master');
    END
    """,
    # Event change capture.
    """
    CREATE TRIGGER trg_events_ai
    AFTER INSERT ON events
    WHEN (SELECT suppress FROM change_context WHERE id = 1) = 0
    BEGIN
      INSERT INTO change_log
        (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
         rev, after_json, pushed_utc)
      VALUES (
        unixepoch(), 'event', NEW.id, NEW.scope, NEW.calendar_id, 'insert',
        (SELECT origin FROM change_context WHERE id = 1),
        (SELECT actor FROM change_context WHERE id = 1),
        (SELECT batch_id FROM change_context WHERE id = 1),
        NEW.rev,
        json_object(
          'id', NEW.id, 'uid', NEW.uid, 'calendar_id', NEW.calendar_id,
          'source_id', NEW.source_id, 'remote_href', NEW.remote_href,
          'etag', NEW.etag, 'rev', NEW.rev, 'sequence', NEW.sequence,
          'summary', NEW.summary, 'rrule', NEW.rrule,
          'start_utc', NEW.start_utc, 'end_utc', NEW.end_utc,
          'start_date', NEW.start_date, 'end_date', NEW.end_date,
          'parent_id', NEW.parent_id, 'recurrence_key', NEW.recurrence_key,
          'status', NEW.status, 'deleted_utc', NEW.deleted_utc),
        CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
                  IN ('remote', 'system')
             THEN unixepoch() END
      );
    END
    """,
    """
    CREATE TRIGGER trg_events_au
    AFTER UPDATE ON events
    WHEN NEW.rev <> OLD.rev
      AND NOT (OLD.deleted_utc IS NULL AND NEW.deleted_utc IS NOT NULL)
      AND (SELECT suppress FROM change_context WHERE id = 1) = 0
    BEGIN
      INSERT INTO change_log
        (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
         rev, before_json, after_json, pushed_utc)
      VALUES (
        unixepoch(), 'event', NEW.id, NEW.scope, NEW.calendar_id, 'update',
        (SELECT origin FROM change_context WHERE id = 1),
        (SELECT actor FROM change_context WHERE id = 1),
        (SELECT batch_id FROM change_context WHERE id = 1),
        NEW.rev,
        json_object(
          'id', OLD.id, 'uid', OLD.uid, 'calendar_id', OLD.calendar_id,
          'etag', OLD.etag, 'rev', OLD.rev, 'sequence', OLD.sequence,
          'summary', OLD.summary, 'start_utc', OLD.start_utc,
          'end_utc', OLD.end_utc, 'start_date', OLD.start_date,
          'end_date', OLD.end_date, 'status', OLD.status,
          'deleted_utc', OLD.deleted_utc),
        json_object(
          'id', NEW.id, 'uid', NEW.uid, 'calendar_id', NEW.calendar_id,
          'etag', NEW.etag, 'rev', NEW.rev, 'sequence', NEW.sequence,
          'summary', NEW.summary, 'start_utc', NEW.start_utc,
          'end_utc', NEW.end_utc, 'start_date', NEW.start_date,
          'end_date', NEW.end_date, 'status', NEW.status,
          'deleted_utc', NEW.deleted_utc),
        CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
                  IN ('remote', 'system')
             THEN unixepoch() END
      );
    END
    """,
    """
    CREATE TRIGGER trg_events_soft_delete
    AFTER UPDATE OF deleted_utc ON events
    WHEN OLD.deleted_utc IS NULL AND NEW.deleted_utc IS NOT NULL
      AND (SELECT suppress FROM change_context WHERE id = 1) = 0
    BEGIN
      INSERT INTO change_log
        (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
         rev, before_json, pushed_utc)
      VALUES (
        unixepoch(), 'event', NEW.id, NEW.scope, NEW.calendar_id, 'delete',
        (SELECT origin FROM change_context WHERE id = 1),
        (SELECT actor FROM change_context WHERE id = 1),
        (SELECT batch_id FROM change_context WHERE id = 1),
        NEW.rev,
        json_object(
          'id', OLD.id, 'uid', OLD.uid, 'calendar_id', OLD.calendar_id,
          'source_id', OLD.source_id, 'remote_href', OLD.remote_href,
          'etag', OLD.etag, 'rev', OLD.rev, 'sequence', OLD.sequence,
          'start_utc', OLD.start_utc, 'end_utc', OLD.end_utc,
          'start_date', OLD.start_date, 'end_date', OLD.end_date,
          'parent_id', OLD.parent_id, 'recurrence_key', OLD.recurrence_key,
          'status', OLD.status),
        CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
                  IN ('remote', 'system')
             THEN unixepoch() END
      );
    END
    """,
    """
    CREATE TRIGGER trg_events_ad
    AFTER DELETE ON events
    WHEN (SELECT suppress FROM change_context WHERE id = 1) = 0
    BEGIN
      INSERT INTO change_log
        (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
         rev, before_json, pushed_utc)
      VALUES (
        unixepoch(), 'event', OLD.id, OLD.scope, OLD.calendar_id, 'delete',
        (SELECT origin FROM change_context WHERE id = 1),
        (SELECT actor FROM change_context WHERE id = 1),
        (SELECT batch_id FROM change_context WHERE id = 1),
        OLD.rev,
        json_object(
          'id', OLD.id, 'uid', OLD.uid, 'calendar_id', OLD.calendar_id,
          'source_id', OLD.source_id, 'remote_href', OLD.remote_href,
          'etag', OLD.etag, 'rev', OLD.rev, 'sequence', OLD.sequence,
          'start_utc', OLD.start_utc, 'end_utc', OLD.end_utc,
          'start_date', OLD.start_date, 'end_date', OLD.end_date,
          'parent_id', OLD.parent_id, 'recurrence_key', OLD.recurrence_key,
          'status', OLD.status),
        CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
                  IN ('remote', 'system')
             THEN unixepoch() END
      );
    END
    """,
    # Child-row revision bumps.
    """
    CREATE TRIGGER trg_attendees_ai AFTER INSERT ON event_attendees
    BEGIN
      UPDATE events SET rev = rev + 1, updated_utc = unixepoch()
      WHERE id = NEW.event_id;
    END
    """,
    """
    CREATE TRIGGER trg_attendees_au AFTER UPDATE ON event_attendees
    BEGIN
      UPDATE events SET rev = rev + 1, updated_utc = unixepoch()
      WHERE id = NEW.event_id;
    END
    """,
    """
    CREATE TRIGGER trg_attendees_ad AFTER DELETE ON event_attendees
    BEGIN
      UPDATE events SET rev = rev + 1, updated_utc = unixepoch()
      WHERE id = OLD.event_id;
    END
    """,
    """
    CREATE TRIGGER trg_reminders_ai AFTER INSERT ON event_reminders
    BEGIN
      UPDATE events SET rev = rev + 1, updated_utc = unixepoch()
      WHERE id = NEW.event_id;
    END
    """,
    """
    CREATE TRIGGER trg_reminders_au AFTER UPDATE ON event_reminders
    BEGIN
      UPDATE events SET rev = rev + 1, updated_utc = unixepoch()
      WHERE id = NEW.event_id;
    END
    """,
    """
    CREATE TRIGGER trg_reminders_ad AFTER DELETE ON event_reminders
    BEGIN
      UPDATE events SET rev = rev + 1, updated_utc = unixepoch()
      WHERE id = OLD.event_id;
    END
    """,
    # Calendar change capture.
    """
    CREATE TRIGGER trg_calendars_ai
    AFTER INSERT ON calendars
    WHEN (SELECT suppress FROM change_context WHERE id = 1) = 0
    BEGIN
      INSERT INTO change_log
        (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
         after_json, pushed_utc)
      VALUES (
        unixepoch(), 'calendar', NEW.id, NEW.scope, NEW.id, 'insert',
        (SELECT origin FROM change_context WHERE id = 1),
        (SELECT actor FROM change_context WHERE id = 1),
        (SELECT batch_id FROM change_context WHERE id = 1),
        json_object('id', NEW.id, 'source_id', NEW.source_id,
                    'remote_id', NEW.remote_id, 'name', NEW.name,
                    'default_tz', NEW.default_tz, 'deleted_utc', NEW.deleted_utc),
        CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
                  IN ('remote', 'system')
             THEN unixepoch() END
      );
    END
    """,
    """
    CREATE TRIGGER trg_calendars_au
    AFTER UPDATE ON calendars
    WHEN (NEW.id, NEW.source_id, NEW.remote_id, NEW.name, NEW.description,
          NEW.color, NEW.default_tz, NEW.read_only, NEW.sync_token, NEW.ctag,
          NEW.created_utc, NEW.updated_utc, NEW.deleted_utc)
         IS NOT
         (OLD.id, OLD.source_id, OLD.remote_id, OLD.name, OLD.description,
          OLD.color, OLD.default_tz, OLD.read_only, OLD.sync_token, OLD.ctag,
          OLD.created_utc, OLD.updated_utc, OLD.deleted_utc)
      AND NOT (OLD.deleted_utc IS NULL AND NEW.deleted_utc IS NOT NULL)
      AND (SELECT suppress FROM change_context WHERE id = 1) = 0
    BEGIN
      INSERT INTO change_log
        (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
         before_json, after_json, pushed_utc)
      VALUES (
        unixepoch(), 'calendar', NEW.id, NEW.scope, NEW.id, 'update',
        (SELECT origin FROM change_context WHERE id = 1),
        (SELECT actor FROM change_context WHERE id = 1),
        (SELECT batch_id FROM change_context WHERE id = 1),
        json_object('id', OLD.id, 'name', OLD.name, 'sync_token', OLD.sync_token,
                    'ctag', OLD.ctag, 'deleted_utc', OLD.deleted_utc),
        json_object('id', NEW.id, 'name', NEW.name, 'sync_token', NEW.sync_token,
                    'ctag', NEW.ctag, 'deleted_utc', NEW.deleted_utc),
        CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
                  IN ('remote', 'system')
             THEN unixepoch() END
      );
    END
    """,
    """
    CREATE TRIGGER trg_calendars_soft_delete
    AFTER UPDATE OF deleted_utc ON calendars
    WHEN OLD.deleted_utc IS NULL AND NEW.deleted_utc IS NOT NULL
      AND (SELECT suppress FROM change_context WHERE id = 1) = 0
    BEGIN
      INSERT INTO change_log
        (at_utc, entity_type, entity_id, scope, calendar_id, op, origin, actor, batch_id,
         before_json, pushed_utc)
      VALUES (
        unixepoch(), 'calendar', NEW.id, NEW.scope, NEW.id, 'delete',
        (SELECT origin FROM change_context WHERE id = 1),
        (SELECT actor FROM change_context WHERE id = 1),
        (SELECT batch_id FROM change_context WHERE id = 1),
        json_object('id', OLD.id, 'source_id', OLD.source_id,
                    'remote_id', OLD.remote_id, 'name', OLD.name),
        CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
                  IN ('remote', 'system')
             THEN unixepoch() END
      );
    END
    """,
    # Source change capture.
    """
    CREATE TRIGGER trg_sources_ai
    AFTER INSERT ON sources
    WHEN (SELECT suppress FROM change_context WHERE id = 1) = 0
    BEGIN
      INSERT INTO change_log
        (at_utc, entity_type, entity_id, scope, op, origin, actor, batch_id,
         after_json, pushed_utc)
      VALUES (
        unixepoch(), 'source', NEW.id, NEW.scope, 'insert',
        (SELECT origin FROM change_context WHERE id = 1),
        (SELECT actor FROM change_context WHERE id = 1),
        (SELECT batch_id FROM change_context WHERE id = 1),
        json_object('id', NEW.id, 'kind', NEW.kind, 'display_name', NEW.display_name),
        CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
                  IN ('remote', 'system')
             THEN unixepoch() END
      );
    END
    """,
    """
    CREATE TRIGGER trg_sources_au
    AFTER UPDATE ON sources
    WHEN (NEW.id, NEW.kind, NEW.display_name, NEW.config_json,
          NEW.capabilities_json, NEW.created_utc, NEW.updated_utc, NEW.deleted_utc)
         IS NOT
         (OLD.id, OLD.kind, OLD.display_name, OLD.config_json,
          OLD.capabilities_json, OLD.created_utc, OLD.updated_utc, OLD.deleted_utc)
      AND NOT (OLD.deleted_utc IS NULL AND NEW.deleted_utc IS NOT NULL)
      AND (SELECT suppress FROM change_context WHERE id = 1) = 0
    BEGIN
      INSERT INTO change_log
        (at_utc, entity_type, entity_id, scope, op, origin, actor, batch_id,
         before_json, after_json, pushed_utc)
      VALUES (
        unixepoch(), 'source', NEW.id, NEW.scope, 'update',
        (SELECT origin FROM change_context WHERE id = 1),
        (SELECT actor FROM change_context WHERE id = 1),
        (SELECT batch_id FROM change_context WHERE id = 1),
        json_object('id', OLD.id, 'kind', OLD.kind, 'display_name', OLD.display_name),
        json_object('id', NEW.id, 'kind', NEW.kind, 'display_name', NEW.display_name),
        CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
                  IN ('remote', 'system')
             THEN unixepoch() END
      );
    END
    """,
    """
    CREATE TRIGGER trg_sources_soft_delete
    AFTER UPDATE OF deleted_utc ON sources
    WHEN OLD.deleted_utc IS NULL AND NEW.deleted_utc IS NOT NULL
      AND (SELECT suppress FROM change_context WHERE id = 1) = 0
    BEGIN
      INSERT INTO change_log
        (at_utc, entity_type, entity_id, scope, op, origin, actor, batch_id,
         before_json, pushed_utc)
      VALUES (
        unixepoch(), 'source', NEW.id, NEW.scope, 'delete',
        (SELECT origin FROM change_context WHERE id = 1),
        (SELECT actor FROM change_context WHERE id = 1),
        (SELECT batch_id FROM change_context WHERE id = 1),
        json_object('id', OLD.id, 'kind', OLD.kind, 'display_name', OLD.display_name),
        CASE WHEN (SELECT origin FROM change_context WHERE id = 1)
                  IN ('remote', 'system')
             THEN unixepoch() END
      );
    END
    """,
)


# --------------------------------------------------------------------------- v2
#
# Notification-owned tables (profile store). Column names follow
# ``docs/design/core.md`` §5.11 exactly; the app design's §6.5 sketch is the
# same schema after reconciliation. ``dedup_key`` is
# ``sha256(store_id | event_id | reminder_id | recurrence_key)`` and the direct
# UNIQUE constraint is the duplicate-delivery spine.

V2_PROFILE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE reminder_instances (
      id                   TEXT PRIMARY KEY,
      store_id             TEXT NOT NULL,
      event_id             TEXT NOT NULL,
      reminder_id          TEXT NOT NULL,
      recurrence_key       TEXT NOT NULL,
      occurrence_start_utc INTEGER NOT NULL,
      offset_seconds       INTEGER NOT NULL,
      fire_at_utc          INTEGER NOT NULL,
      priority             TEXT NOT NULL DEFAULT 'normal'
                           CHECK (priority IN ('digest', 'normal', 'high', 'critical')),
      channel_policy_json  TEXT NOT NULL DEFAULT '{}'
                           CHECK (json_valid(channel_policy_json)),
      state                TEXT NOT NULL DEFAULT 'scheduled'
                           CHECK (state IN ('scheduled', 'firing', 'retrying',
                                            'delivered', 'snoozed', 'acknowledged',
                                            'suppressed', 'cancelled', 'failed',
                                            'dead_letter')),
      attempt              INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
      lease_until_utc      INTEGER,
      snooze_until_utc     INTEGER,
      dedup_key            TEXT NOT NULL,
      created_utc          INTEGER NOT NULL,
      updated_utc          INTEGER NOT NULL,
      UNIQUE (store_id, event_id, reminder_id, recurrence_key),
      UNIQUE (dedup_key)
    )
    """,
    """
    CREATE INDEX idx_reminder_instances_due
      ON reminder_instances(state, fire_at_utc)
    """,
    """
    CREATE INDEX idx_reminder_instances_event
      ON reminder_instances(store_id, event_id)
    """,
    """
    CREATE TABLE notification_deliveries (
      id                   INTEGER PRIMARY KEY AUTOINCREMENT,
      reminder_instance_id TEXT NOT NULL
                           REFERENCES reminder_instances(id) ON DELETE CASCADE,
      channel              TEXT NOT NULL,
      target               TEXT NOT NULL,
      attempt              INTEGER NOT NULL,
      started_utc          INTEGER NOT NULL,
      finished_utc         INTEGER,
      outcome              TEXT NOT NULL
                           CHECK (outcome IN ('success', 'transient', 'permanent', 'unknown')),
      exit_code            INTEGER,
      provider_id          TEXT,
      error                TEXT,
      latency_ms           INTEGER
    )
    """,
    """
    CREATE INDEX idx_notification_deliveries_instance
      ON notification_deliveries(reminder_instance_id)
    """,
    """
    CREATE TABLE notification_channels (
      id          TEXT PRIMARY KEY,
      kind        TEXT NOT NULL,
      enabled     INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
      config_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(config_json)),
      secret_ref  TEXT,
      created_utc INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE notification_prefs (
      key        TEXT PRIMARY KEY,
      value_json TEXT NOT NULL CHECK (json_valid(value_json)),
      updated_utc INTEGER NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE web_push_subscriptions (
      id               TEXT PRIMARY KEY,
      endpoint         TEXT NOT NULL UNIQUE,
      p256dh           TEXT NOT NULL,
      auth             TEXT NOT NULL,
      user_agent       TEXT,
      created_utc      INTEGER NOT NULL,
      last_success_utc INTEGER,
      failure_count    INTEGER NOT NULL DEFAULT 0
    )
    """,
)

# The global store owns coordination state only: one delivery lease and a
# content-free ledger of dedup keys already sent. Neither row contains event
# content, a target, or a message body.
V2_GLOBAL_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE reminder_lease (
      scope             TEXT PRIMARY KEY CHECK (scope = 'global'),
      owner_profile_id  TEXT NOT NULL,
      lease_expires_utc INTEGER NOT NULL,
      heartbeat_utc     INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE reminder_delivery_ledger (
      dedup_key     TEXT PRIMARY KEY,
      delivered_utc INTEGER NOT NULL,
      profile_id    TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX idx_reminder_ledger_delivered
      ON reminder_delivery_ledger(delivered_utc)
    """,
)
