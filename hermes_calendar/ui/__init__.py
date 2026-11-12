"""Dashboard-facing UI support.

The calendar UI is a Hermes web-dashboard plugin shipped in the repository's
``dashboard/`` directory: a hand-written JS/CSS bundle plus
:mod:`hermes_calendar.ui.dashboard_api`, mounted by the dashboard at
``/api/plugins/hermes-calendar/``. There is no separate web server, port, or
PWA; the dashboard's existing origin and auth gate serve the tab. The API reads
and writes through the same service layer and stores as the Hermes tools.
"""
