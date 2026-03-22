"""Webhook channel — receives HTTP webhooks and routes agent responses to other channels.

Setup:
    1. Enable in config.json under channels.webhook
    2. Create workspace/webhooks/{source}.md files with agent instructions
    3. Point your webhook providers at http://host:port/webhook/{source}

Supported providers:
    - GitHub (X-Hub-Signature-256, X-GitHub-Event)
    - Sentry (Sentry-Hook-Signature, Sentry-Hook-Resource)
    - Grafana (X-Grafana-Alerting-Signature, payload-based status)
    - GlitchTip (no signature, Slack-compatible payload with attachments)
    - Any provider that sends JSON and optionally X-Webhook-Secret

Config example:
    "webhook": {
        "enabled": true,
        "port": 18790,
        "host": "0.0.0.0",
        "allowFrom": ["github", "sentry", "grafana"],
        "sources": {
            "github": {
                "secret": "your-github-webhook-secret",
                "allowEvents": ["push", "pull_request", "issues"],
                "ignoreRepos": ["org/legacy-docs"],
                "ignoreBranches": ["dependabot/*", "renovate/*"],
                "ignoreLabels": ["automated", "bot"],
                "notifyChannel": "discord",
                "notifyChatId": "123456789"
            },
            "sentry": {
                "secret": "your-sentry-client-secret",
                "allowEvents": ["error", "issue"],
                "notifyChannel": "telegram",
                "notifyChatId": "987654321"
            },
            "grafana": {
                "secret": "your-grafana-shared-secret",
                "allowEvents": ["firing"],
                "notifyChannel": "discord",
                "notifyChatId": "123456789"
            }
        }
    }

How it works:
    POST /webhook/{source}
      1. Validates source is in allowFrom
      2. Verifies signature (GitHub HMAC-SHA256) if secret is configured
      3. Applies config-driven filters (events, repos, branches, labels)
      4. Loads workspace/webhooks/{source}.md for agent context
      5. Publishes InboundMessage to the bus
      6. Returns 202 Accepted immediately

    When the agent responds, send() re-publishes the response as an
    OutboundMessage on the source's notifyChannel/notifyChatId, so the
    existing ChannelManager delivers it to Discord, Telegram, etc.
"""

import fnmatch
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------

class WebhookSourceConfig(Base):
    """Per-source filter and routing config."""

    secret: str = ""
    allow_events: list[str] = Field(default_factory=list)
    ignore_repos: list[str] = Field(default_factory=list)
    ignore_branches: list[str] = Field(default_factory=list)
    ignore_labels: list[str] = Field(default_factory=list)
    require_assignee: str = ""
    notify_channel: str = ""
    notify_chat_id: str = ""


class WebhookConfig(Base):
    """Webhook channel configuration."""

    enabled: bool = False
    port: int = 18790
    host: str = "0.0.0.0"
    allow_from: list[str] = Field(default_factory=list)
    sources: dict[str, WebhookSourceConfig] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------

class WebhookChannel(BaseChannel):
    """HTTP webhook receiver that feeds payloads into the agent loop."""

    name = "webhook"
    display_name = "Webhook"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebhookConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WebhookConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebhookConfig = config
        self._runner: web.AppRunner | None = None
        self._workspace: Path | None = None

    async def start(self) -> None:
        """Start the HTTP server."""
        self._running = True
        self._workspace = Path.home() / ".nanobot" / "workspace"

        app = web.Application()
        app.router.add_post("/webhook/{source}", self._handle_request)
        app.router.add_get("/webhook/health", self._handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.host, self.config.port)
        await site.start()
        logger.info("Webhook channel listening on {}:{}", self.config.host, self.config.port)

    async def stop(self) -> None:
        """Stop the HTTP server."""
        self._running = False
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def send(self, msg: OutboundMessage) -> None:
        """Forward agent response to the source's configured notify channel."""
        notify = msg.metadata.get("_webhook_notify")
        if not notify:
            return

        await self.bus.publish_outbound(OutboundMessage(
            channel=notify["channel"],
            chat_id=notify["chat_id"],
            content=msg.content,
        ))

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_request(self, request: web.Request) -> web.Response:
        source = request.match_info["source"]

        # 1. Check allowFrom
        if not self.is_allowed(source):
            logger.warning("Webhook source '{}' not in allowFrom", source)
            return web.json_response({"error": "source not allowed"}, status=403)

        # 2. Read body
        try:
            body = await request.read()
            payload = json.loads(body)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("Webhook bad payload from '{}': {}", source, e)
            return web.json_response({"error": "invalid JSON"}, status=400)

        source_config = self.config.sources.get(source, WebhookSourceConfig())

        # 3. Verify signature
        if source_config.secret:
            if not self._verify_signature(source, source_config.secret, body, request):
                return web.json_response({"error": "signature verification failed"}, status=401)

        # 4. Apply filters
        if not self._passes_filters(source_config, payload, request):
            return web.json_response({"status": "filtered"}, status=200)

        # 5. Build context and publish
        context = self._build_context(source, payload, request)
        metadata: dict[str, Any] = {"_webhook_source": source}

        if source_config.notify_channel and source_config.notify_chat_id:
            metadata["_webhook_notify"] = {
                "channel": source_config.notify_channel,
                "chat_id": source_config.notify_chat_id,
            }

        await self._handle_message(
            sender_id=source,
            chat_id=f"webhook:{source}",
            content=context,
            metadata=metadata,
        )

        return web.json_response({"status": "accepted"}, status=202)

    # ------------------------------------------------------------------
    # Signature verification
    # ------------------------------------------------------------------

    def _verify_signature(
        self, source: str, secret: str, body: bytes, request: web.Request,
    ) -> bool:
        """Verify webhook signature.

        Supports:
            - GitHub: X-Hub-Signature-256 (HMAC-SHA256, prefixed with "sha256=")
            - Sentry: Sentry-Hook-Signature (HMAC-SHA256, raw hex)
            - Grafana: X-Grafana-Alerting-Signature (HMAC-SHA256, raw hex)
            - Generic: X-Webhook-Secret (exact match, for simple providers)
        """
        expected_hex = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        # GitHub: X-Hub-Signature-256
        gh_sig = request.headers.get("X-Hub-Signature-256")
        if gh_sig:
            if hmac.compare_digest(gh_sig, "sha256=" + expected_hex):
                return True
            logger.warning("Webhook '{}': GitHub signature mismatch", source)
            return False

        # Sentry: Sentry-Hook-Signature
        sentry_sig = request.headers.get("Sentry-Hook-Signature")
        if sentry_sig:
            if hmac.compare_digest(sentry_sig, expected_hex):
                return True
            logger.warning("Webhook '{}': Sentry signature mismatch", source)
            return False

        # Grafana: X-Grafana-Alerting-Signature
        grafana_sig = request.headers.get("X-Grafana-Alerting-Signature")
        if grafana_sig:
            if hmac.compare_digest(grafana_sig, expected_hex):
                return True
            logger.warning("Webhook '{}': Grafana signature mismatch", source)
            return False

        # Generic: X-Webhook-Secret (exact match)
        header_secret = request.headers.get("X-Webhook-Secret")
        if header_secret:
            if hmac.compare_digest(header_secret, secret):
                return True
            logger.warning("Webhook '{}': secret mismatch", source)
            return False

        logger.warning("Webhook '{}': secret configured but no signature header found", source)
        return False

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def _passes_filters(
        self, cfg: WebhookSourceConfig, payload: dict, request: web.Request,
    ) -> bool:
        """Apply config-driven filters. Returns True if the event should be processed."""
        event_type = self._detect_event_type(payload, request)

        # Event type whitelist
        if cfg.allow_events and event_type not in cfg.allow_events:
            logger.debug("Webhook filtered: event '{}' not in allowEvents", event_type)
            return False

        # Repository filter
        repo = (payload.get("repository") or {}).get("full_name", "")
        if repo and cfg.ignore_repos:
            if any(fnmatch.fnmatch(repo, pat) for pat in cfg.ignore_repos):
                logger.debug("Webhook filtered: repo '{}' in ignoreRepos", repo)
                return False

        # Branch filter (push events)
        ref = payload.get("ref", "")
        if ref and cfg.ignore_branches:
            # refs/heads/branch-name -> branch-name
            branch = ref.removeprefix("refs/heads/")
            if any(fnmatch.fnmatch(branch, pat) for pat in cfg.ignore_branches):
                logger.debug("Webhook filtered: branch '{}' in ignoreBranches", branch)
                return False

        # Label filter (issues, PRs)
        if cfg.ignore_labels:
            labels = self._extract_labels(payload)
            if labels & set(cfg.ignore_labels):
                logger.debug("Webhook filtered: labels {} in ignoreLabels", labels)
                return False

        # Assignee filter
        if cfg.require_assignee:
            assignees = self._extract_assignees(payload)
            if assignees is not None and cfg.require_assignee not in assignees:
                logger.debug("Webhook filtered: '{}' not in assignees {}", cfg.require_assignee, assignees)
                return False

        # GitHub projects_v2_item: only pass through actual status field changes
        if event_type == "projects_v2_item":
            if not self._is_project_status_change(payload):
                return False

        return True

    @staticmethod
    def _is_project_status_change(payload: dict) -> bool:
        """Filter projects_v2_item events to only status field changes.

        Drops: created, reordered, deleted, non-status field edits.
        Passes: action=edited with a field change on a single_select field.
        """
        action = payload.get("action", "")
        if action != "edited":
            logger.debug("Webhook filtered: projects_v2_item action '{}' != 'edited'", action)
            return False

        changes = payload.get("changes", {})
        field_value = changes.get("field_value", {})
        field_type = field_value.get("field_type", "")
        if field_type != "single_select":
            logger.debug("Webhook filtered: projects_v2_item field_type '{}' != 'single_select'", field_type)
            return False

        field_name = field_value.get("field_name", "")
        if field_name != "Status":
            logger.debug("Webhook filtered: projects_v2_item field '{}' != 'Status'", field_name)
            return False

        logger.debug("Webhook passed: projects_v2_item status change detected")
        return True

    @staticmethod
    def _detect_event_type(payload: dict, request: web.Request) -> str:
        """Detect the event type from headers or payload.

        Supported providers:
            - GitHub: X-GitHub-Event header (e.g. "push", "pull_request")
            - Sentry: Sentry-Hook-Resource header (e.g. "error", "issue")
            - Grafana: status field in payload body ("firing", "resolved")
            - Generic: falls back to event/type/action fields in payload
        """
        # GitHub
        gh_event = request.headers.get("X-GitHub-Event")
        if gh_event:
            return gh_event

        # Sentry
        sentry_hook = request.headers.get("Sentry-Hook-Resource")
        if sentry_hook:
            return sentry_hook

        # Grafana alerting — status is "firing" or "resolved"
        if "alerts" in payload and "status" in payload:
            return payload["status"]

        # GlitchTip — Slack-compatible format with "text" and "attachments"
        if "attachments" in payload and "text" in payload:
            return "alert"

        # Generic fallback
        return payload.get("event", payload.get("type", payload.get("action", "unknown")))

    @staticmethod
    def _extract_labels(payload: dict) -> set[str]:
        """Extract label names from GitHub-style issue/PR payloads."""
        labels: set[str] = set()
        for key in ("pull_request", "issue"):
            obj = payload.get(key, {})
            for label in obj.get("labels", []):
                name = label.get("name") if isinstance(label, dict) else str(label)
                if name:
                    labels.add(name)
        return labels

    @staticmethod
    def _extract_assignees(payload: dict) -> set[str] | None:
        """Extract assignee logins from GitHub issue/PR payloads.

        Returns None for event types that don't have assignees (e.g. push),
        so the filter is only applied when assignee info is present.
        """
        for key in ("pull_request", "issue"):
            obj = payload.get(key)
            if obj is None:
                continue
            assignees: set[str] = set()
            for a in obj.get("assignees", []):
                login = a.get("login") if isinstance(a, dict) else str(a)
                if login:
                    assignees.add(login)
            assignee = obj.get("assignee")
            if isinstance(assignee, dict) and assignee.get("login"):
                assignees.add(assignee["login"])
            return assignees
        return None

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def _build_context(
        self, source: str, payload: dict, request: web.Request,
    ) -> str:
        """Combine source instructions from workspace with the webhook payload."""
        instructions = self._load_instructions(source)
        event_type = self._detect_event_type(payload, request)

        parts = []
        if instructions:
            parts.append(instructions)
        parts.append(f"## Webhook Event\n\n**Source:** {source}\n**Event:** {event_type}")
        parts.append(f"```json\n{json.dumps(payload, indent=2, default=str)}\n```")

        return "\n\n---\n\n".join(parts)

    def _load_instructions(self, source: str) -> str:
        """Load workspace/webhooks/{source}.md if it exists."""
        if not self._workspace:
            return ""
        path = self._workspace / "webhooks" / f"{source}.md"
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip()
            except Exception as e:
                logger.warning("Failed to read webhook instructions {}: {}", path, e)
        return ""
