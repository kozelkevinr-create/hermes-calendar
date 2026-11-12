(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !SDK.React || !SDK.hooks) {
    return;
  }

  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useMemo = SDK.hooks.useMemo;
  var useRef = SDK.hooks.useRef;

  var PLUGIN_NAME = "hermes-calendar";
  var API_ROOT = "/api/plugins/" + PLUGIN_NAME;
  var MAX_ICS_BYTES = 5 * 1024 * 1024;
  var EMPTY_LIST = [];
  var WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  var MONTH_NAMES = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December"
  ];
  var RECURRENCE_OPTIONS = [
    { value: "none", label: "Does not repeat" },
    { value: "daily", label: "Daily" },
    { value: "weekly", label: "Weekly" },
    { value: "monthly", label: "Monthly" },
    { value: "yearly", label: "Yearly" }
  ];
  var COMMON_ZONES = [
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Paris",
    "Europe/Madrid",
    "Africa/Johannesburg",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Australia/Sydney",
    "Pacific/Auckland"
  ];

  // ------------------------------------------------------------------ helpers

  function pad2(value) {
    return value < 10 ? "0" + value : String(value);
  }

  function browserTimezone() {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch (error) {
      return "UTC";
    }
  }

  function localDateKey(date) {
    return date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate());
  }

  function parseDateKey(key) {
    var parts = String(key).split("-");
    return new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]), 0, 0, 0, 0);
  }

  function addDays(date, count) {
    var copy = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    copy.setDate(copy.getDate() + count);
    return copy;
  }

  function todayKey() {
    return localDateKey(new Date());
  }

  function isoLocalDateTime(date) {
    return (
      localDateKey(date) +
      "T" +
      pad2(date.getHours()) +
      ":" +
      pad2(date.getMinutes()) +
      ":" +
      pad2(date.getSeconds())
    );
  }

  function formatMonthTitle(date) {
    return MONTH_NAMES[date.getMonth()] + " " + date.getFullYear();
  }

  function formatDayHeading(key) {
    var date = parseDateKey(key);
    try {
      return new Intl.DateTimeFormat(undefined, {
        weekday: "short",
        month: "short",
        day: "numeric",
        year: "numeric"
      }).format(date);
    } catch (error) {
      return key;
    }
  }

  function formatEventAria(ev) {
    var key = eventDateKey(ev);
    var title = ev.summary || "Untitled event";
    var time = ev.all_day ? "All day" : eventTimeLabel(ev);
    return title + ", " + formatDayHeading(key) + ", " + time;
  }

  function eventDateKey(ev) {
    if (ev.start && ev.start.date) {
      return ev.start.date;
    }
    if (ev.start && ev.start.local) {
      return String(ev.start.local).slice(0, 10);
    }
    if (typeof ev.start_utc === "number") {
      return localDateKey(new Date(ev.start_utc * 1000));
    }
    return "";
  }

  function eventTimeLabel(ev) {
    if (ev.all_day) {
      return "All day";
    }
    if (ev.start && ev.start.local) {
      return String(ev.start.local).slice(11, 16);
    }
    return "";
  }

  function eventSortKey(ev) {
    if (typeof ev.start_utc === "number") {
      return ev.start_utc;
    }
    return 0;
  }

  function eventScope(ev, fallback) {
    return ev.scope === "global" ? "global" : ev.scope === "profile" ? "profile" : fallback;
  }

  function eventOccurrenceStart(ev) {
    var value = ev.occurrence_start || ev.start || {};
    if (value.date) {
      return String(value.date);
    }
    if (value.local) {
      return String(value.local);
    }
    if (ev.recurrence_key) {
      return String(ev.recurrence_key);
    }
    return "";
  }

  function monthCells(cursor) {
    var first = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
    var start = addDays(first, -first.getDay());
    var cells = [];
    for (var index = 0; index < 42; index += 1) {
      var day = addDays(start, index);
      cells.push({
        key: localDateKey(day),
        date: day,
        inMonth: day.getMonth() === cursor.getMonth()
      });
    }
    return cells;
  }

  function rangeFor(view, cursor) {
    if (view === "agenda") {
      var agendaStart = new Date(cursor.getFullYear(), cursor.getMonth(), cursor.getDate());
      var agendaEnd = addDays(agendaStart, 90);
      // The all-day filter compares UTC dates, so query one day wider on each
      // side and let the agenda hide the extra days.
      return {
        start: addDays(agendaStart, -1),
        end: addDays(agendaEnd, 1),
        visibleStart: agendaStart,
        visibleEnd: agendaEnd
      };
    }
    var cells = monthCells(cursor);
    return {
      start: addDays(cells[0].date, -1),
      end: addDays(cells[41].date, 2),
      visibleStart: cells[0].date,
      visibleEnd: addDays(cells[41].date, 1)
    };
  }

  function api(path, init) {
    return SDK.fetchJSON(API_ROOT + path, init);
  }

  function apiJSON(path, method, body) {
    return api(path, {
      method: method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  }

  function errorText(error) {
    var message = error && error.message ? String(error.message) : "Request failed";
    var brace = message.indexOf("{");
    if (brace >= 0) {
      try {
        var payload = JSON.parse(message.slice(brace));
        if (payload && payload.error) {
          return String(payload.error);
        }
      } catch (ignored) {
        return message;
      }
    }
    return message;
  }

  function frequencyOf(rrule) {
    if (!rrule) {
      return "none";
    }
    var match = /FREQ=([A-Z]+)/.exec(String(rrule).toUpperCase());
    if (!match) {
      return "keep";
    }
    var value = match[1].toLowerCase();
    return value === "daily" || value === "weekly" || value === "monthly" || value === "yearly"
      ? value
      : "keep";
  }

  function countOf(rrule) {
    var match = /;COUNT=(\d+)/.exec(String(rrule || "").toUpperCase());
    return match ? match[1] : "";
  }

  function untilOf(rrule) {
    var match = /;UNTIL=(\d{8})/.exec(String(rrule || "").toUpperCase());
    if (!match) {
      return "";
    }
    return match[1].slice(0, 4) + "-" + match[1].slice(4, 6) + "-" + match[1].slice(6, 8);
  }

  function buildRRule(form) {
    if (form.recurrence === "none" || form.recurrence === "keep") {
      return null;
    }
    var rule = "FREQ=" + form.recurrence.toUpperCase();
    if (String(form.count).trim() !== "") {
      rule += ";COUNT=" + String(Number(form.count));
    } else if (form.until) {
      rule += ";UNTIL=" + form.until.replace(/-/g, "") + "T235959Z";
    }
    return rule;
  }

  function blankForm(scope, calendarId, timezone) {
    var day = todayKey();
    return {
      scope: scope,
      calendarId: calendarId || "",
      summary: "",
      description: "",
      location: "",
      allDay: false,
      startDate: day,
      startTime: "09:00",
      endDate: day,
      endTime: "10:00",
      timezone: timezone || "UTC",
      recurrence: "none",
      count: "",
      until: "",
      seriesScope: "all"
    };
  }

  function formFromEvent(ev, timezone) {
    var start = ev.start || {};
    var end = ev.end || {};
    var startKey = start.date || String(start.local || "").slice(0, 10) || todayKey();
    var endKey = end.date || String(end.local || "").slice(0, 10) || startKey;
    var rule = ev.master_rrule !== undefined ? ev.master_rrule : ev.rrule;
    return {
      scope: eventScope(ev, "profile"),
      calendarId: ev.calendar_id || "",
      summary: ev.summary || "",
      description: ev.description || "",
      location: ev.location || "",
      allDay: Boolean(ev.all_day),
      startDate: startKey,
      startTime: String(start.local || "").slice(11, 16) || "09:00",
      endDate: endKey,
      endTime: String(end.local || "").slice(11, 16) || "10:00",
      timezone: start.tz || timezone || "UTC",
      recurrence: frequencyOf(rule),
      count: countOf(rule),
      until: untilOf(rule),
      seriesScope: "all"
    };
  }

  function validateForm(form) {
    if (!form.calendarId) {
      return "Create or select a calendar first.";
    }
    if (!String(form.summary).trim()) {
      return "Title is required.";
    }
    if (String(form.summary).length > 500) {
      return "Title must be at most 500 characters.";
    }
    if (!form.startDate || !form.endDate) {
      return "Start and end dates are required.";
    }
    if (!String(form.timezone).trim()) {
      return "Timezone is required.";
    }
    if (String(form.timezone).length > 64) {
      return "Timezone must be at most 64 characters.";
    }
    if (form.allDay) {
      if (form.endDate <= form.startDate) {
        return "The end date must be after the start date (it is exclusive).";
      }
    } else {
      if (!form.startTime || !form.endTime) {
        return "Start and end times are required.";
      }
      var start = new Date(form.startDate + "T" + form.startTime + ":00");
      var end = new Date(form.endDate + "T" + form.endTime + ":00");
      if (!(end.getTime() > start.getTime())) {
        return "The end must be later than the start.";
      }
    }
    if (form.recurrence !== "none" && form.recurrence !== "keep") {
      if (String(form.count).trim() !== "" && form.until) {
        return "Choose either a repeat count or an until date, not both.";
      }
      if (String(form.count).trim() !== "") {
        var count = Number(form.count);
        if (!isFinite(count) || count < 1 || count > 999) {
          return "Repeat count must be between 1 and 999.";
        }
      }
    }
    return "";
  }

  function buildEventPayload(form) {
    var payload = {
      scope: form.scope,
      calendar_id: form.calendarId,
      summary: String(form.summary).trim(),
      timezone: String(form.timezone).trim(),
      all_day: Boolean(form.allDay),
      description: String(form.description).trim() ? form.description : null,
      location: String(form.location).trim() ? form.location : null
    };
    if (form.allDay) {
      payload.start = form.startDate;
      payload.end = form.endDate;
    } else {
      payload.start = form.startDate + "T" + form.startTime + ":00";
      payload.end = form.endDate + "T" + form.endTime + ":00";
    }
    var rule = buildRRule(form);
    if (form.recurrence === "keep") {
      return payload;
    }
    if (rule) {
      payload.rrule = rule;
    } else {
      payload.rrule = null;
    }
    return payload;
  }

  // ---------------------------------------------------------------- components

  function field(id, labelText, control, hint) {
    var describedBy = hint ? id + "-hint" : undefined;
    if (control && control.props && describedBy && !control.props["aria-describedby"]) {
      control = React.cloneElement(control, { "aria-describedby": describedBy });
    }
    return h(
      "div",
      { className: "hc-field" },
      h("label", { className: "hc-label", htmlFor: id }, labelText),
      control,
      hint ? h("p", { className: "hc-hint", id: id + "-hint" }, hint) : null
    );
  }

  function EventChip(ev, onOpen) {
    var time = eventTimeLabel(ev);
    var title = ev.summary || "Untitled event";
    return h(
      "button",
      {
        type: "button",
        key: (ev.id || "") + (ev.recurrence_key || ""),
        className: "hc-event" + (ev.all_day ? " hc-event-allday" : ""),
        "aria-label": formatEventAria(ev),
        onClick: function () {
          onOpen(ev);
        }
      },
      h("span", { className: "hc-event-time" }, ev.all_day ? "All day" : time),
      h("span", { className: "hc-event-title" }, title)
    );
  }

  function CalendarPage() {
    var timezone = useMemo(browserTimezone, []);
    var startDay = todayKey();

    var [scope, setScope] = useState("profile");
    var [calendarsByScope, setCalendarsByScope] = useState({ profile: EMPTY_LIST, global: EMPTY_LIST });
    var [calendarId, setCalendarId] = useState("");
    var [view, setView] = useState("month");
    var [cursor, setCursor] = useState(new Date());
    var [events, setEvents] = useState(EMPTY_LIST);
    var [loading, setLoading] = useState(false);
    var [status, setStatus] = useState({ kind: "", message: "" });
    var [formOpen, setFormOpen] = useState(false);
    var [form, setForm] = useState(blankForm("profile", "", timezone));
    var [editing, setEditing] = useState(null);
    var [busy, setBusy] = useState(false);
    var [confirmDelete, setConfirmDelete] = useState(false);
    var [calendarFormOpen, setCalendarFormOpen] = useState(false);
    var [calendarDraft, setCalendarDraft] = useState({ name: "", color: "", scope: "profile" });
    var [refreshCalendars, setRefreshCalendars] = useState(0);
    var [refreshEvents, setRefreshEvents] = useState(0);
    var titleRef = useRef(null);
    var fileRef = useRef(null);

    var calendars = useMemo(function () {
      return calendarsByScope[scope] || EMPTY_LIST;
    }, [calendarsByScope, scope]);

    var formCalendars = useMemo(function () {
      return calendarsByScope[form.scope] || EMPTY_LIST;
    }, [calendarsByScope, form.scope]);

    var range = useMemo(function () {
      return rangeFor(view, cursor);
    }, [view, cursor]);

    var cells = useMemo(function () {
      return monthCells(cursor);
    }, [cursor]);

    var eventsByDay = useMemo(function () {
      var map = {};
      events.forEach(function (ev) {
        var key = eventDateKey(ev);
        if (!key) {
          return;
        }
        if (!map[key]) {
          map[key] = [];
        }
        map[key].push(ev);
      });
      Object.keys(map).forEach(function (key) {
        map[key].sort(function (left, right) {
          return eventSortKey(left) - eventSortKey(right);
        });
      });
      return map;
    }, [events]);

    var agendaDays = useMemo(function () {
      var firstKey = localDateKey(range.visibleStart);
      var lastKey = localDateKey(range.visibleEnd);
      return Object.keys(eventsByDay)
        .filter(function (key) {
          return key >= firstKey && key < lastKey;
        })
        .sort();
    }, [eventsByDay, range]);

    useEffect(
      function () {
        var cancelled = false;
        Promise.allSettled([
          api("/calendars?scope=profile"),
          api("/calendars?scope=global")
        ]).then(function (results) {
          if (cancelled) {
            return;
          }
          var next = { profile: EMPTY_LIST, global: EMPTY_LIST };
          var failed = [];
          ["profile", "global"].forEach(function (name, index) {
            var result = results[index];
            if (result.status === "fulfilled" && result.value && result.value.calendars) {
              next[name] = result.value.calendars;
            } else {
              failed.push(name);
            }
          });
          setCalendarsByScope(next);
          if (failed.length) {
            setStatus({
              kind: "error",
              message: "Could not load " + failed.join(" and ") + " calendars."
            });
          }
        });
        return function () {
          cancelled = true;
        };
      },
      [refreshCalendars]
    );

    useEffect(
      function () {
        setCalendarId(function (current) {
          if (current && calendars.some(function (item) {
            return item.id === current;
          })) {
            return current;
          }
          return calendars.length ? calendars[0].id : "";
        });
      },
      [calendars]
    );

    useEffect(
      function () {
        if (!calendarId) {
          setEvents(EMPTY_LIST);
          return undefined;
        }
        var cancelled = false;
        setLoading(true);
        var params = new URLSearchParams();
        params.set("scope", scope);
        params.set("calendar_id", calendarId);
        params.set("timezone", timezone);
        params.set("start", isoLocalDateTime(range.start));
        params.set("end", isoLocalDateTime(range.end));
        api("/events?" + params.toString())
          .then(function (payload) {
            if (cancelled) {
              return;
            }
            setEvents(payload.events || EMPTY_LIST);
          })
          .catch(function (error) {
            if (!cancelled) {
              setEvents(EMPTY_LIST);
              setStatus({ kind: "error", message: errorText(error) });
            }
          })
          .then(function () {
            if (!cancelled) {
              setLoading(false);
            }
          });
        return function () {
          cancelled = true;
        };
      },
      [scope, calendarId, timezone, range, refreshEvents]
    );

    useEffect(
      function () {
        if (formOpen && titleRef.current) {
          titleRef.current.focus();
        }
      },
      [formOpen, editing]
    );

    function notice(kind, message) {
      setStatus({ kind: kind, message: message });
    }

    function changeScope(nextScope) {
      var list = calendarsByScope[nextScope] || EMPTY_LIST;
      setScope(nextScope);
      setCalendarId(list.length ? list[0].id : "");
      setFormOpen(false);
      setEditing(null);
      setConfirmDelete(false);
      notice("", "");
    }

    function openCreate() {
      var list = calendarsByScope[scope] || EMPTY_LIST;
      if (!list.length) {
        notice("error", "Create a calendar in this store first.");
        setCalendarDraft({ name: "", color: "", scope: scope });
        setCalendarFormOpen(true);
        return;
      }
      setEditing(null);
      setConfirmDelete(false);
      setForm(blankForm(scope, list[0].id, timezone));
      setFormOpen(true);
      notice("", "");
    }

    function openEdit(ev) {
      var evScope = eventScope(ev, scope);
      var masterRule = ev.master_rrule !== undefined ? ev.master_rrule : ev.rrule;
      setEditing({
        id: ev.master_id || ev.id,
        rev: ev.master_rev || ev.rev,
        occurrenceStart: eventOccurrenceStart(ev),
        recurring: Boolean(masterRule || ev.recurrence_key || ev.is_override),
        allDay: Boolean(ev.all_day),
        originalRrule: masterRule || null
      });
      setForm(formFromEvent(ev, timezone));
      setConfirmDelete(false);
      setFormOpen(true);
      notice("", "");
    }

    function updateForm(patch) {
      setForm(function (current) {
        return Object.assign({}, current, patch);
      });
      setConfirmDelete(false);
    }

    function changeFormScope(nextScope) {
      var list = calendarsByScope[nextScope] || EMPTY_LIST;
      updateForm({
        scope: nextScope,
        calendarId: list.length ? list[0].id : ""
      });
    }

    function submitForm(eventObject) {
      eventObject.preventDefault();
      if (busy) {
        return;
      }
      var problem = validateForm(form);
      if (problem) {
        notice("error", problem);
        return;
      }
      var payload = buildEventPayload(form);
      if (editing) {
        if (editing.allDay) {
          delete payload.start;
          delete payload.end;
        }
        payload.expected_rev = editing.rev;
        payload.series_scope = editing.recurring ? form.seriesScope : "all";
        if (payload.series_scope === "this") {
          payload.occurrence_start = editing.occurrenceStart;
          // The series rule belongs to the master, never to a single
          // occurrence override.
          delete payload.rrule;
        }
      }
      setBusy(true);
      var request = editing
        ? apiJSON("/events/" + encodeURIComponent(editing.id), "PATCH", payload)
        : apiJSON("/events", "POST", payload);
      request
        .then(function () {
          setFormOpen(false);
          setEditing(null);
          setScope(form.scope);
          setCalendarId(form.calendarId);
          setRefreshEvents(function (value) {
            return value + 1;
          });
          notice("ok", editing ? "Event updated." : "Event created.");
        })
        .catch(function (error) {
          notice("error", errorText(error));
        })
        .then(function () {
          setBusy(false);
        });
    }

    function removeEvent() {
      if (!editing || busy) {
        return;
      }
      if (!confirmDelete) {
        setConfirmDelete(true);
        return;
      }
      var params = new URLSearchParams();
      params.set("scope", form.scope);
      params.set("expected_rev", String(editing.rev));
      if (editing.recurring && form.seriesScope === "this") {
        params.set("series_scope", "this");
        params.set("occurrence_start", editing.occurrenceStart);
      }
      setBusy(true);
      api("/events/" + encodeURIComponent(editing.id) + "?" + params.toString(), {
        method: "DELETE"
      })
        .then(function () {
          setFormOpen(false);
          setEditing(null);
          setConfirmDelete(false);
          setRefreshEvents(function (value) {
            return value + 1;
          });
          notice("ok", "Event deleted.");
        })
        .catch(function (error) {
          notice("error", errorText(error));
        })
        .then(function () {
          setBusy(false);
        });
    }

    function submitCalendar(eventObject) {
      eventObject.preventDefault();
      if (busy) {
        return;
      }
      var name = String(calendarDraft.name).trim();
      if (!name) {
        notice("error", "Calendar name is required.");
        return;
      }
      if (name.length > 200) {
        notice("error", "Calendar name must be at most 200 characters.");
        return;
      }
      setBusy(true);
      apiJSON("/calendar", "POST", {
        scope: calendarDraft.scope,
        name: name,
        color: String(calendarDraft.color).trim() || null
      })
        .then(function (payload) {
          setCalendarFormOpen(false);
          setCalendarDraft({ name: "", color: "", scope: calendarDraft.scope });
          setRefreshCalendars(function (value) {
            return value + 1;
          });
          setScope(calendarDraft.scope);
          if (payload.calendar && payload.calendar.id) {
            setCalendarId(payload.calendar.id);
          }
          notice("ok", "Calendar created.");
        })
        .catch(function (error) {
          notice("error", errorText(error));
        })
        .then(function () {
          setBusy(false);
        });
    }

    function importCalendar(eventObject) {
      var file = eventObject.target.files && eventObject.target.files[0];
      if (!file) {
        return;
      }
      if (!calendarId) {
        notice("error", "Select a calendar before importing.");
        return;
      }
      if (file.size > MAX_ICS_BYTES) {
        notice("error", "The calendar file must be at most 5 MiB.");
        return;
      }
      file.text().then(function (text) {
        setBusy(true);
        return apiJSON("/import", "POST", {
          scope: scope,
          calendar_id: calendarId,
          text: text
        });
      }).then(function (payload) {
        if (!payload) {
          return;
        }
        setRefreshEvents(function (value) {
          return value + 1;
        });
        var summary =
          "Imported " +
          payload.imported +
          ", updated " +
          payload.updated +
          ", skipped " +
          payload.skipped +
          ".";
        if (payload.warnings && payload.warnings.length) {
          summary += " " + payload.warnings.length + " warning(s).";
        }
        notice("ok", summary);
      }).catch(function (error) {
        notice("error", errorText(error));
      }).then(function () {
        setBusy(false);
        if (fileRef.current) {
          fileRef.current.value = "";
        }
      });
    }

    function exportCalendar() {
      if (!calendarId || busy) {
        return;
      }
      var params = new URLSearchParams();
      params.set("scope", scope);
      params.set("calendar_id", calendarId);
      params.set("timezone", timezone);
      params.set("start", isoLocalDateTime(range.visibleStart));
      params.set("end", isoLocalDateTime(range.visibleEnd));
      setBusy(true);
      api("/export?" + params.toString())
        .then(function (payload) {
          var blob = new Blob([payload.ics], { type: "text/calendar;charset=utf-8" });
          var url = URL.createObjectURL(blob);
          var anchor = document.createElement("a");
          anchor.href = url;
          anchor.download = "calendar.ics";
          anchor.textContent = "Download calendar";
          document.body.appendChild(anchor);
          anchor.click();
          document.body.removeChild(anchor);
          window.setTimeout(function () {
            URL.revokeObjectURL(url);
          }, 1000);
          notice("ok", "Exported " + payload.count + " event(s).");
        })
        .catch(function (error) {
          notice("error", errorText(error));
        })
        .then(function () {
          setBusy(false);
        });
    }

    function shiftMonth(direction) {
      if (view === "agenda") {
        setCursor(function (current) {
          return addDays(current, direction * 30);
        });
        return;
      }
      setCursor(function (current) {
        return new Date(current.getFullYear(), current.getMonth() + direction, 1);
      });
    }

    function recurrenceOptions() {
      var options = RECURRENCE_OPTIONS.slice();
      if (editing && editing.originalRrule) {
        options = [{ value: "keep", label: "Keep existing rule" }].concat(options);
      }
      return options;
    }

    function renderStatus() {
      if (!status.message) {
        return null;
      }
      return h(
        "p",
        {
          className: "hc-status" + (status.kind ? " hc-status-" + status.kind : ""),
          role: "status",
          "aria-live": "polite"
        },
        status.message
      );
    }

    function renderCalendarForm() {
      if (!calendarFormOpen) {
        return null;
      }
      return h(
        "form",
        { className: "hc-card hc-form", onSubmit: submitCalendar },
        h("h2", { className: "hc-card-title" }, "New calendar"),
        field(
          "hc-cal-name",
          "Calendar name",
          h("input", {
            id: "hc-cal-name",
            className: "hc-input",
            type: "text",
            maxLength: 200,
            required: true,
            value: calendarDraft.name,
            onChange: function (eventObject) {
              setCalendarDraft(Object.assign({}, calendarDraft, { name: eventObject.target.value }));
            }
          })
        ),
        field(
          "hc-cal-color",
          "Color (optional)",
          h("input", {
            id: "hc-cal-color",
            className: "hc-input",
            type: "text",
            maxLength: 32,
            placeholder: "#336699",
            value: calendarDraft.color,
            onChange: function (eventObject) {
              setCalendarDraft(Object.assign({}, calendarDraft, { color: eventObject.target.value }));
            }
          })
        ),
        field(
          "hc-cal-scope",
          "Store",
          h(
            "select",
            {
              id: "hc-cal-scope",
              className: "hc-input",
              value: calendarDraft.scope,
              onChange: function (eventObject) {
                setCalendarDraft(
                  Object.assign({}, calendarDraft, { scope: eventObject.target.value })
                );
              }
            },
            h("option", { value: "profile" }, "Profile (private)"),
            h("option", { value: "global" }, "Shared (global)")
          ),
          "The global store holds one shared calendar for every profile."
        ),
        h(
          "div",
          { className: "hc-actions" },
          h(
            "button",
            { type: "submit", className: "hc-button hc-primary", disabled: busy },
            busy ? "Saving…" : "Create calendar"
          ),
          h(
            "button",
            {
              type: "button",
              className: "hc-button",
              onClick: function () {
                setCalendarFormOpen(false);
              }
            },
            "Cancel"
          )
        )
      );
    }

    function renderEventForm() {
      if (!formOpen) {
        return null;
      }
      var allDayLocked = Boolean(editing && editing.allDay);
      return h(
        "form",
        { className: "hc-card hc-form", onSubmit: submitForm },
        h("h2", { className: "hc-card-title" }, editing ? "Edit event" : "New event"),
        field(
          "hc-summary",
          "Title",
          h("input", {
            id: "hc-summary",
            ref: titleRef,
            className: "hc-input",
            type: "text",
            required: true,
            maxLength: 500,
            value: form.summary,
            onChange: function (eventObject) {
              updateForm({ summary: eventObject.target.value });
            }
          })
        ),
        h(
          "div",
          { className: "hc-row" },
          field(
            "hc-scope",
            "Store",
            h(
              "select",
              {
                id: "hc-scope",
                className: "hc-input",
                disabled: Boolean(editing),
                value: form.scope,
                onChange: function (eventObject) {
                  changeFormScope(eventObject.target.value);
                }
              },
              h("option", { value: "profile" }, "Profile (private)"),
              h("option", { value: "global" }, "Shared (global)")
            ),
            editing ? "An event cannot be moved between stores after creation." : undefined
          ),
          field(
            "hc-calendar",
            "Calendar",
            h(
              "select",
              {
                id: "hc-calendar",
                className: "hc-input",
                disabled: Boolean(editing),
                value: form.calendarId,
                onChange: function (eventObject) {
                  updateForm({ calendarId: eventObject.target.value });
                }
              },
              formCalendars.length
                ? formCalendars.map(function (calendar) {
                    return h(
                      "option",
                      { key: calendar.id, value: calendar.id },
                      calendar.name + (calendar.read_only ? " (read-only)" : "")
                    );
                  })
                : h("option", { value: "" }, "No calendars in this store")
            )
          )
        ),
        h(
          "div",
          { className: "hc-check" },
          h("input", {
            id: "hc-allday",
            type: "checkbox",
            checked: form.allDay,
            disabled: Boolean(editing),
            onChange: function (eventObject) {
              updateForm({ allDay: eventObject.target.checked });
            }
          }),
          h("label", { htmlFor: "hc-allday" }, "All-day event"),
          editing
            ? h("span", { className: "hc-hint" }, "All-day mode cannot be changed after creation.")
            : null
        ),
        h(
          "div",
          { className: "hc-row" },
          field(
            "hc-start-date",
            "Start date",
            h("input", {
              id: "hc-start-date",
              className: "hc-input",
              type: "date",
              required: true,
              disabled: allDayLocked,
              value: form.startDate,
              onChange: function (eventObject) {
                updateForm({ startDate: eventObject.target.value });
              }
            })
          ),
          form.allDay
            ? field(
                "hc-end-date",
                "End date (exclusive)",
                h("input", {
                  id: "hc-end-date",
                  className: "hc-input",
                  type: "date",
                  required: true,
                  disabled: allDayLocked,
                  value: form.endDate,
                  onChange: function (eventObject) {
                    updateForm({ endDate: eventObject.target.value });
                  }
                }),
                allDayLocked
                  ? "Changing the dates of an existing all-day event is not supported yet."
                  : "The end date is not included."
              )
            : field(
                "hc-end-date",
                "End date",
                h("input", {
                  id: "hc-end-date",
                  className: "hc-input",
                  type: "date",
                  required: true,
                  value: form.endDate,
                  onChange: function (eventObject) {
                    updateForm({ endDate: eventObject.target.value });
                  }
                })
              )
        ),
        form.allDay
          ? null
          : h(
              "div",
              { className: "hc-row" },
              field(
                "hc-start-time",
                "Start time",
                h("input", {
                  id: "hc-start-time",
                  className: "hc-input",
                  type: "time",
                  required: true,
                  value: form.startTime,
                  onChange: function (eventObject) {
                    updateForm({ startTime: eventObject.target.value });
                  }
                })
              ),
              field(
                "hc-end-time",
                "End time",
                h("input", {
                  id: "hc-end-time",
                  className: "hc-input",
                  type: "time",
                  required: true,
                  value: form.endTime,
                  onChange: function (eventObject) {
                    updateForm({ endTime: eventObject.target.value });
                  }
                })
              )
            ),
        field(
          "hc-timezone",
          "Timezone",
          h("input", {
            id: "hc-timezone",
            className: "hc-input",
            type: "text",
            list: "hc-timezone-list",
            required: true,
            maxLength: 64,
            value: form.timezone,
            onChange: function (eventObject) {
              updateForm({ timezone: eventObject.target.value });
            }
          }),
          "IANA zone used for times without an offset. Browser default: " + timezone + "."
        ),
        h(
          "datalist",
          { id: "hc-timezone-list" },
          COMMON_ZONES.map(function (zone) {
            return h("option", { key: zone, value: zone });
          })
        ),
        h(
          "div",
          { className: "hc-row" },
          field(
            "hc-recurrence",
            "Repeats",
            h(
              "select",
              {
                id: "hc-recurrence",
                className: "hc-input",
                value: form.recurrence,
                onChange: function (eventObject) {
                  updateForm({ recurrence: eventObject.target.value });
                }
              },
              recurrenceOptions().map(function (option) {
                return h("option", { key: option.value, value: option.value }, option.label);
              })
            )
          ),
          field(
            "hc-count",
            "Repeat count",
            h("input", {
              id: "hc-count",
              className: "hc-input",
              type: "number",
              min: 1,
              max: 999,
              value: form.count,
              onChange: function (eventObject) {
                updateForm({ count: eventObject.target.value });
              }
            })
          ),
          field(
            "hc-until",
            "Repeat until",
            h("input", {
              id: "hc-until",
              className: "hc-input",
              type: "date",
              value: form.until,
              onChange: function (eventObject) {
                updateForm({ until: eventObject.target.value });
              }
            })
          )
        ),
        editing && editing.recurring
          ? field(
              "hc-series-scope",
              "Apply to",
              h(
                "select",
                {
                  id: "hc-series-scope",
                  className: "hc-input",
                  value: form.seriesScope,
                  onChange: function (eventObject) {
                    updateForm({ seriesScope: eventObject.target.value });
                  }
                },
                h("option", { value: "all" }, "Whole series"),
                h("option", { value: "this" }, "This occurrence only")
              )
            )
          : null,
        field(
          "hc-description",
          "Description (optional)",
          h("textarea", {
            id: "hc-description",
            className: "hc-input hc-textarea",
            rows: 3,
            maxLength: 20000,
            value: form.description,
            onChange: function (eventObject) {
              updateForm({ description: eventObject.target.value });
            }
          })
        ),
        field(
          "hc-location",
          "Location (optional)",
          h("input", {
            id: "hc-location",
            className: "hc-input",
            type: "text",
            maxLength: 2000,
            value: form.location,
            onChange: function (eventObject) {
              updateForm({ location: eventObject.target.value });
            }
          })
        ),
        h(
          "div",
          { className: "hc-actions" },
          h(
            "button",
            { type: "submit", className: "hc-button hc-primary", disabled: busy },
            busy ? "Saving…" : editing ? "Save changes" : "Create event"
          ),
          h(
            "button",
            {
              type: "button",
              className: "hc-button",
              onClick: function () {
                setFormOpen(false);
                setEditing(null);
                setConfirmDelete(false);
              }
            },
            "Cancel"
          ),
          editing
            ? h(
                "button",
                {
                  type: "button",
                  className: "hc-button hc-danger",
                  disabled: busy,
                  onClick: removeEvent
                },
                confirmDelete ? "Confirm delete" : "Delete"
              )
            : null
        )
      );
    }

    function renderMonth() {
      return h(
        "div",
        { className: "hc-month" },
        h(
          "div",
          { className: "hc-weekdays", "aria-hidden": "true" },
          WEEKDAYS.map(function (day) {
            return h("div", { key: day, className: "hc-weekday" }, day);
          })
        ),
        h(
          "div",
          { className: "hc-grid", "aria-label": formatMonthTitle(cursor) },
          cells.map(function (cell) {
            var dayEvents = eventsByDay[cell.key] || EMPTY_LIST;
            return h(
              "div",
              {
                key: cell.key,
                role: "group",
                className:
                  "hc-day" +
                  (cell.inMonth ? "" : " hc-day-outside") +
                  (cell.key === startDay ? " hc-day-today" : ""),
                "aria-label": formatDayHeading(cell.key)
              },
              h("div", { className: "hc-day-number" }, String(cell.date.getDate())),
              h(
                "div",
                { className: "hc-day-events" },
                dayEvents.slice(0, 4).map(function (ev) {
                  return EventChip(ev, openEdit);
                }),
                dayEvents.length > 4
                  ? h(
                      "button",
                      {
                        type: "button",
                        className: "hc-more",
                        onClick: function () {
                          setView("agenda");
                        }
                      },
                      "+" + (dayEvents.length - 4) + " more"
                    )
                  : null
              )
            );
          })
        )
      );
    }

    function renderAgenda() {
      if (!agendaDays.length) {
        return null;
      }
      return h(
        "div",
        { className: "hc-agenda" },
        agendaDays.map(function (key) {
          return h(
            "section",
            { key: key, className: "hc-agenda-day", "aria-label": formatDayHeading(key) },
            h("h3", { className: "hc-agenda-heading" }, formatDayHeading(key)),
            h(
              "ul",
              { className: "hc-agenda-list" },
              eventsByDay[key].map(function (ev) {
                return h(
                  "li",
                  { key: (ev.id || "") + (ev.recurrence_key || "") },
                  EventChip(ev, openEdit)
                );
              })
            )
          );
        })
      );
    }

    function renderEmpty() {
      if (loading) {
        return h("p", { className: "hc-empty" }, "Loading…");
      }
      if (!calendars.length) {
        return h(
          "div",
          { className: "hc-card hc-empty" },
          h("h2", { className: "hc-card-title" }, "No calendars in this store"),
          h(
            "p",
            null,
            scope === "global"
              ? "The shared store normally has one calendar. If it is missing, open it once from the profile store or check the shared-store diagnostics in Status."
              : "The profile store starts empty. Create a calendar to add your first event."
          ),
          h(
            "button",
            {
              type: "button",
              className: "hc-button hc-primary",
              onClick: function () {
                setCalendarDraft({ name: "", color: "", scope: scope });
                setCalendarFormOpen(true);
              }
            },
            "Create a calendar"
          )
        );
      }
      if (!events.length) {
        return h(
          "div",
          { className: "hc-card hc-empty" },
          h("h2", { className: "hc-card-title" }, "Nothing scheduled"),
          h(
            "p",
            null,
            view === "month"
              ? "No events in " + formatMonthTitle(cursor) + "."
              : "No events in the next 90 days."
          ),
          h(
            "button",
            {
              type: "button",
              className: "hc-button hc-primary",
              onClick: openCreate
            },
            "Create an event"
          )
        );
      }
      return null;
    }

    var empty = renderEmpty();

    return h(
      "div",
      { className: "hc-root" },
      h(
        "div",
        { className: "hc-header" },
        h("div", null, h("h1", { className: "hc-title" }, "Calendar")),
        h(
          "div",
          { className: "hc-scope-switch", role: "group", "aria-label": "Calendar store" },
          h(
            "button",
            {
              type: "button",
              className: "hc-button" + (scope === "profile" ? " hc-active" : ""),
              "aria-pressed": scope === "profile",
              onClick: function () {
                changeScope("profile");
              }
            },
            "Profile"
          ),
          h(
            "button",
            {
              type: "button",
              className: "hc-button" + (scope === "global" ? " hc-active" : ""),
              "aria-pressed": scope === "global",
              onClick: function () {
                changeScope("global");
              }
            },
            "Shared (global)"
          )
        )
      ),
      h(
        "div",
        { className: "hc-toolbar" },
        h(
          "div",
          { className: "hc-toolbar-group" },
          h(
            "button",
            {
              type: "button",
              className: "hc-button",
              "aria-label": view === "agenda" ? "Previous 30 days" : "Previous month",
              onClick: function () {
                shiftMonth(-1);
              }
            },
            "Previous"
          ),
          h(
            "button",
            {
              type: "button",
              className: "hc-button",
              onClick: function () {
                setCursor(new Date());
              }
            },
            "Today"
          ),
          h(
            "button",
            {
              type: "button",
              className: "hc-button",
              "aria-label": view === "agenda" ? "Next 30 days" : "Next month",
              onClick: function () {
                shiftMonth(1);
              }
            },
            "Next"
          )
        ),
        h(
          "span",
          { className: "hc-period", "aria-live": "polite" },
          view === "month"
            ? formatMonthTitle(cursor)
            : "90 days from " + formatDayHeading(localDateKey(cursor))
        ),
        h(
          "div",
          { className: "hc-toolbar-group" },
          h(
            "button",
            {
              type: "button",
              className: "hc-button" + (view === "month" ? " hc-active" : ""),
              "aria-pressed": view === "month",
              onClick: function () {
                setView("month");
              }
            },
            "Month"
          ),
          h(
            "button",
            {
              type: "button",
              className: "hc-button" + (view === "agenda" ? " hc-active" : ""),
              "aria-pressed": view === "agenda",
              onClick: function () {
                setView("agenda");
              }
            },
            "Agenda"
          )
        )
      ),
      renderStatus(),
      h(
        "div",
        { className: "hc-controls" },
        field(
          "hc-view-calendar",
          "Calendar (" + (scope === "profile" ? "profile: private" : "global: shared") + ")",
          h(
            "select",
            {
              id: "hc-view-calendar",
              className: "hc-input",
              value: calendarId,
              onChange: function (eventObject) {
                setCalendarId(eventObject.target.value);
              }
            },
            calendars.length
              ? calendars.map(function (calendar) {
                  return h(
                    "option",
                    { key: calendar.id, value: calendar.id },
                    calendar.name + (calendar.read_only ? " (read-only)" : "")
                  );
                })
              : h("option", { value: "" }, "No calendars")
          )
        ),
        h(
          "div",
          { className: "hc-controls-actions" },
          h(
            "button",
            {
              type: "button",
              className: "hc-button hc-primary",
              onClick: openCreate,
              disabled: busy || !calendars.length
            },
            "New event"
          ),
          h(
            "button",
            {
              type: "button",
              className: "hc-button",
              onClick: function () {
                setCalendarDraft({ name: "", color: "", scope: scope });
                setCalendarFormOpen(true);
              }
            },
            "New calendar"
          ),
          h(
            "button",
            {
              type: "button",
              className: "hc-button",
              onClick: function () {
                if (fileRef.current) {
                  fileRef.current.value = "";
                  fileRef.current.click();
                }
              },
              disabled: busy || !calendarId
            },
            "Import ICS"
          ),
          h("input", {
            ref: fileRef,
            type: "file",
            accept: ".ics,text/calendar",
            className: "hc-hidden-input",
            tabIndex: -1,
            onChange: importCalendar
          }),
          h(
            "button",
            {
              type: "button",
              className: "hc-button",
              onClick: exportCalendar,
              disabled: busy || !calendarId
            },
            "Export ICS"
          )
        )
      ),
      h(
        "p",
        { className: "hc-timezone-note" },
        "Times are shown in each event's zone. New events default to your browser timezone, " +
          timezone +
          "."
      ),
      renderCalendarForm(),
      renderEventForm(),
      empty,
      empty ? null : view === "month" ? renderMonth() : renderAgenda(),
      h(
        "section",
        { className: "hc-card hc-slot", "aria-label": "Notification panel placeholder" },
        h("h2", { className: "hc-card-title" }, "Notifications"),
        h(
          "p",
          { className: "hc-hint" },
          "Reserved for the reminder notification panel (owned by a later task). This area is intentionally empty; no reminder data is loaded here."
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register(PLUGIN_NAME, CalendarPage);
})();
