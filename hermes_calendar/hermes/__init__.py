"""Hermes-specific adapters.

Wraps the plugin contract: ``ctx`` surfaces, profile resolution, ``hermes send``,
cron safety-net installation, and plugin-data paths. Keep these behind narrow
interfaces so the rest of the package stays testable without Hermes installed.
"""
