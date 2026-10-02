"""Connector registry.

Built-in connectors are registered explicitly; third-party packages can contribute connectors through the
``flowforge.connectors`` entry-point group (``my_pkg.connector:MyConnector``) without forking the platform.
"""

from __future__ import annotations

import logging
from importlib.metadata import entry_points

from app.connectors.sdk import Connector

logger = logging.getLogger(__name__)

_registry: dict[str, Connector] = {}
_loaded = False


def register(connector_cls: type[Connector]) -> type[Connector]:
    key = connector_cls.key
    if key in _registry and type(_registry[key]) is not connector_cls:
        raise ValueError(f"Connector key '{key}' already registered")
    _registry[key] = connector_cls()
    return connector_cls


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    from app.ai.provider_connectors import AI_PROVIDER_CONNECTORS
    from app.connectors.builtin.email import EmailConnector
    from app.connectors.builtin.google_drive import GoogleDriveConnector
    from app.connectors.builtin.http_rest import RestApiConnector
    from app.connectors.builtin.hubspot import HubSpotConnector
    from app.connectors.builtin.jira import JiraConnector
    from app.connectors.builtin.mysql import MySqlConnector
    from app.connectors.builtin.pipedrive import PipedriveConnector
    from app.connectors.builtin.postgres import PostgresConnector
    from app.connectors.builtin.sendgrid import SendGridConnector
    from app.connectors.builtin.slack import SlackConnector
    from app.connectors.builtin.teams import TeamsConnector
    from app.connectors.builtin.twilio import TwilioConnector
    from app.connectors.builtin.vonage import VonageConnector

    for cls in (
        RestApiConnector,
        SlackConnector,
        TeamsConnector,
        EmailConnector,
        SendGridConnector,
        PostgresConnector,
        MySqlConnector,
        HubSpotConnector,
        PipedriveConnector,
        JiraConnector,
        GoogleDriveConnector,
        TwilioConnector,
        VonageConnector,
        *AI_PROVIDER_CONNECTORS,
    ):
        register(cls)
    for ep in entry_points(group="flowforge.connectors"):
        try:
            register(ep.load())
            logger.info("Loaded connector plugin %s", ep.name)
        except Exception:
            logger.exception("Failed to load connector plugin %s", ep.name)


def get_connector(key: str) -> Connector:
    _load()
    try:
        return _registry[key]
    except KeyError as exc:
        raise KeyError(f"Unknown connector '{key}'") from exc


def all_connectors() -> list[Connector]:
    _load()
    return sorted(_registry.values(), key=lambda c: (c.category, c.name))


def connectors_with_capability(capability: str) -> dict[str, str]:
    """Map connector key -> action key implementing ``capability``."""
    _load()
    out: dict[str, str] = {}
    for c in _registry.values():
        for a in c.actions.values():
            if a.capability == capability:
                out[c.key] = a.key
    return out
