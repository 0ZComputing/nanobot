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
import os
from pathlib import Path
from typing import Any

import aiohttp
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
    # GitHub Projects v2 filters — code-level routing to avoid LLM calls
    project_node_id: str = ""
    actionable_statuses: list[str] = Field(default_factory=list)
    notify_statuses: list[str] = Field(default_factory=list)

    # Status name → workflow name mapping for structured context
    status_workflows: dict[str, str] = Field(default_factory=dict)

    # Sender filter — skip events triggered by these GitHub users (prevents self-trigger loops)
    ignore_senders: list[str] = Field(default_factory=list)


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

        # Skip noisy noop responses — only forward actionable notifications
        content_lower = (msg.content or "").lower()
        if "noop" in content_lower:
            logger.debug("Webhook send: skipping noop response")
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

        event_type = self._detect_event_type(payload, request)

        # 5. Code-level enrichment and routing for projects_v2_item events
        #    Avoids LLM calls for noops, wrong assignees, and notify-only statuses.
        if event_type == "projects_v2_item":
            return await self._handle_project_item(source, source_config, payload)

        # 5b. Issues assigned event — check if issue has an actionable project status
        if event_type == "issues" and payload.get("action") == "assigned":
            return await self._handle_issue_assigned(source, source_config, payload)

        # 6. Non-project events: build context and publish to LLM as before
        context = self._build_context(source, payload, request, event_type=event_type)
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

    async def _handle_project_item(
        self, source: str, cfg: WebhookSourceConfig, payload: dict,
    ) -> web.Response:
        """Handle projects_v2_item events with code-level enrichment and routing.

        1. Fetch issue details + status via GraphQL
        2. Check assignee in code
        3. Route by status: noop, notify-only, or LLM workflow
        """
        # Sender filter — skip events triggered by the bot's own PAT (prevents self-trigger loops)
        sender = (payload.get("sender") or {}).get("login", "")
        if cfg.ignore_senders and sender in cfg.ignore_senders:
            logger.debug("Webhook filtered: sender '{}' in ignoreSenders", sender)
            return web.json_response({"status": "filtered", "reason": "self-trigger"}, status=200)

        # Enrich: fetch issue details and current status via GraphQL
        enriched = await self._enrich_project_item(payload)
        if enriched is None:
            logger.debug("Webhook filtered: projects_v2_item enrichment failed")
            return web.json_response({"status": "filtered", "reason": "enrichment failed"}, status=200)

        status_name = enriched["status_name"]
        issue_number = enriched["issue_number"]
        issue_title = enriched["issue_title"]
        assignees = enriched["assignees"]

        # Assignee check — only process issues assigned to the configured user.
        # If assignee was just changed (race condition), the issues/assigned handler
        # will catch it when the new assignee event arrives.
        if cfg.require_assignee and cfg.require_assignee not in assignees:
            logger.debug("Webhook filtered: '{}' not in issue #{} assignees {}", cfg.require_assignee, issue_number, assignees)
            return web.json_response({"status": "filtered", "reason": "assignee"}, status=200)

        # Notify-only statuses — send a simple Discord message, no LLM needed
        if cfg.notify_statuses and status_name in cfg.notify_statuses:
            logger.info("Webhook notify-only: issue #{} '{}' → {}", issue_number, issue_title, status_name)
            if cfg.notify_channel and cfg.notify_chat_id:
                await self.bus.publish_outbound(OutboundMessage(
                    channel=cfg.notify_channel,
                    chat_id=cfg.notify_chat_id,
                    content=f"Issue #{issue_number}: {issue_title} → **{status_name}**",
                ))
            return web.json_response({"status": "notified", "issue": issue_number, "status_name": status_name}, status=200)

        # Actionable statuses — only these go to the LLM
        if cfg.actionable_statuses and status_name not in cfg.actionable_statuses:
            logger.debug("Webhook filtered: status '{}' not actionable for issue #{}", status_name, issue_number)
            return web.json_response({"status": "filtered", "reason": "noop status"}, status=200)

        # Resolve workflow name from config mapping
        workflow = cfg.status_workflows.get(status_name, "")

        # Build structured context for the LLM — no raw payload
        context = self._build_project_context(source, enriched, workflow)
        metadata: dict[str, Any] = {
            "_webhook_source": source,
            "_enriched": enriched,
        }

        if cfg.notify_channel and cfg.notify_chat_id:
            metadata["_webhook_notify"] = {
                "channel": cfg.notify_channel,
                "chat_id": cfg.notify_chat_id,
            }

        logger.info("Webhook → LLM: issue #{} '{}' status={} workflow={}", issue_number, issue_title, status_name, workflow)

        await self._handle_message(
            sender_id=source,
            chat_id=f"webhook:{source}",
            content=context,
            metadata=metadata,
        )

        # Chain: ready-for-planning moves status to "In Planning", but the resulting
        # webhook is dropped by ignoreSenders (bot's own PAT).  Fire in-planning
        # automatically so the planning agent actually spawns.
        chain_workflow = cfg.status_workflows.get({
            "ready-for-planning": "In Planning",
            "ready-for-dev": "In Development",
        }.get(workflow, ""), "")
        if chain_workflow:
            enriched_next = {**enriched, "status_name": {
                "ready-for-planning": "In Planning",
                "ready-for-dev": "In Development",
            }[workflow]}
            chain_context = self._build_project_context(source, enriched_next, chain_workflow)
            chain_meta: dict[str, Any] = {
                "_webhook_source": source,
                "_enriched": enriched_next,
            }
            if cfg.notify_channel and cfg.notify_chat_id:
                chain_meta["_webhook_notify"] = {
                    "channel": cfg.notify_channel,
                    "chat_id": cfg.notify_chat_id,
                }
            logger.info("Webhook chain: {} → {} for issue #{}", workflow, chain_workflow, issue_number)
            await self._handle_message(
                sender_id=source,
                chat_id=f"webhook:{source}",
                content=chain_context,
                metadata=chain_meta,
            )

        return web.json_response({"status": "accepted", "issue": issue_number, "workflow": workflow}, status=202)

    async def _handle_issue_assigned(
        self, source: str, cfg: WebhookSourceConfig, payload: dict,
    ) -> web.Response:
        """Handle issues assigned events — trigger workflow if issue has an actionable project status.

        Covers the race condition where a status change and assignee change happen
        near-simultaneously. The status change event may see the old assignee, so
        this handler catches the assignment and checks if the issue needs processing.
        """
        assignee = (payload.get("assignee") or {}).get("login", "")
        if cfg.require_assignee and assignee != cfg.require_assignee:
            logger.debug("Webhook filtered: assigned '{}' != required '{}'", assignee, cfg.require_assignee)
            return web.json_response({"status": "filtered", "reason": "wrong assignee"}, status=200)

        issue = payload.get("issue") or {}
        issue_number = issue.get("number")
        if not issue_number:
            return web.json_response({"status": "filtered", "reason": "no issue number"}, status=200)

        # Look up this issue's project board status
        repo = (payload.get("repository") or {}).get("full_name", "")
        owner, repo_name = repo.split("/", 1) if "/" in repo else ("", "")
        enriched = await self._enrich_issue_project_status(issue_number, cfg, owner, repo_name)
        if enriched is None:
            logger.debug("Webhook filtered: issue #{} not on project board or enrichment failed", issue_number)
            return web.json_response({"status": "filtered", "reason": "not on board"}, status=200)

        status_name = enriched["status_name"]
        issue_title = enriched["issue_title"]

        # Only trigger for actionable statuses
        if cfg.actionable_statuses and status_name not in cfg.actionable_statuses:
            logger.debug("Webhook filtered: issue #{} status '{}' not actionable (assigned event)", issue_number, status_name)
            return web.json_response({"status": "filtered", "reason": "noop status"}, status=200)

        workflow = cfg.status_workflows.get(status_name, "")

        # Build structured context and send to LLM
        context = self._build_project_context(source, enriched, workflow)
        metadata: dict[str, Any] = {
            "_webhook_source": source,
            "_enriched": enriched,
        }

        if cfg.notify_channel and cfg.notify_chat_id:
            metadata["_webhook_notify"] = {
                "channel": cfg.notify_channel,
                "chat_id": cfg.notify_chat_id,
            }

        logger.info("Webhook → LLM (assigned): issue #{} '{}' status={} workflow={}", issue_number, issue_title, status_name, workflow)

        await self._handle_message(
            sender_id=source,
            chat_id=f"webhook:{source}",
            content=context,
            metadata=metadata,
        )

        return web.json_response({"status": "accepted", "issue": issue_number, "workflow": workflow}, status=202)

    _ISSUE_PROJECT_QUERY = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        issue(number: $number) {
          title
          body
          labels(first: 10) { nodes { name } }
          projectItems(first: 10) {
            nodes {
              id
              project { id }
              fieldValueByName(name: "Status") {
                ... on ProjectV2ItemFieldSingleSelectValue { name }
              }
            }
          }
        }
      }
    }
    """.strip()

    async def _enrich_issue_project_status(
        self, issue_number: int, cfg: WebhookSourceConfig,
        owner: str = "", repo: str = "",
    ) -> dict[str, Any] | None:
        """Look up an issue's project board status by issue number.

        Returns enriched dict compatible with _build_project_context, or None
        if the issue is not on the configured project board.
        """
        gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        if not gh_token:
            return None

        # Extract owner/repo from the project — we need to know the repo
        # For now, this is hardcoded to match the project board context.
        # The repo info comes from the webhook payload's repository field,
        # but we use the config's project_node_id to match the right project item.

        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    "https://api.github.com/graphql",
                    json={
                        "query": self._ISSUE_PROJECT_QUERY,
                        "variables": {
                            "owner": owner,
                            "repo": repo,
                            "number": issue_number,
                        },
                    },
                    headers={
                        "Authorization": f"Bearer {gh_token}",
                        "Accept": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                )
                if resp.status != 200:
                    return None
                data = await resp.json()
        except Exception as e:
            logger.warning("Webhook: issue project status query error: {}", e)
            return None

        if data.get("errors"):
            logger.warning("Webhook: issue project status query errors: {}", data["errors"])

        issue_data = ((data.get("data") or {}).get("repository") or {}).get("issue") or {}
        if not issue_data:
            return None

        # Find the project item matching our configured project
        project_items = (issue_data.get("projectItems") or {}).get("nodes", [])
        item_node_id = ""
        status_name = ""
        for item in project_items:
            project = item.get("project") or {}
            if cfg.project_node_id and project.get("id") != cfg.project_node_id:
                continue
            item_node_id = item.get("id", "")
            status_field = item.get("fieldValueByName") or {}
            status_name = status_field.get("name", "")
            break

        if not item_node_id:
            return None

        labels = {n.get("name") for n in (issue_data.get("labels") or {}).get("nodes", []) if n.get("name")}

        return {
            "item_node_id": item_node_id,
            "issue_number": issue_number,
            "issue_title": issue_data.get("title", ""),
            "issue_body": issue_data.get("body", ""),
            "assignees": set(),  # Not needed — we already validated the assignee from the payload
            "labels": labels,
            "status_name": status_name,
        }

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

        # Assignee filter — skip for issues/assigned events (payload shows pre-assignment state;
        # the _handle_issue_assigned handler checks the new assignee from payload.assignee instead)
        if cfg.require_assignee and not (event_type == "issues" and payload.get("action") == "assigned"):
            assignees = self._extract_assignees(payload)
            if assignees is not None and cfg.require_assignee not in assignees:
                logger.debug("Webhook filtered: '{}' not in assignees {}", cfg.require_assignee, assignees)
                return False

        # GitHub projects_v2_item: only pass through actual status field changes
        if event_type == "projects_v2_item":
            if not self._is_project_status_change(payload, cfg):
                return False

        return True

    @staticmethod
    def _is_project_status_change(payload: dict, cfg: WebhookSourceConfig) -> bool:
        """Filter projects_v2_item events to only status field changes.

        Drops: created, reordered, deleted, non-status field edits, wrong project.
        Passes: action=edited with a Status field change on the configured project.
        """
        action = payload.get("action", "")
        if action != "edited":
            logger.debug("Webhook filtered: projects_v2_item action '{}' != 'edited'", action)
            return False

        # Project node ID filter — skip events from other projects
        if cfg.project_node_id:
            item_project = (payload.get("projects_v2_item") or {}).get("project_node_id", "")
            if item_project != cfg.project_node_id:
                logger.debug("Webhook filtered: project '{}' != configured '{}'", item_project, cfg.project_node_id)
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

    _ENRICH_QUERY = """
    query($id: ID!) {
      node(id: $id) {
        ... on ProjectV2Item {
          id
          content {
            ... on Issue {
              number
              title
              body
              assignees(first: 10) { nodes { login } }
              labels(first: 10) { nodes { name } }
            }
          }
          fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
      }
    }
    """.strip()

    async def _enrich_project_item(self, payload: dict) -> dict[str, Any] | None:
        """Fetch issue details and status via GitHub GraphQL for a projects_v2_item event.

        Returns a dict with issue_number, issue_title, issue_body, assignees,
        labels, status_name, item_node_id — or None if the API call fails.
        """
        item = payload.get("projects_v2_item") or {}
        item_node_id = item.get("node_id", "")
        if not item_node_id:
            logger.warning("Webhook: projects_v2_item missing node_id")
            return None

        gh_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        if not gh_token:
            logger.warning("Webhook: no GH_TOKEN/GITHUB_TOKEN for GraphQL enrichment")
            return None

        try:
            async with aiohttp.ClientSession() as session:
                resp = await session.post(
                    "https://api.github.com/graphql",
                    json={"query": self._ENRICH_QUERY, "variables": {"id": item_node_id}},
                    headers={
                        "Authorization": f"Bearer {gh_token}",
                        "Accept": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=15),
                )
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Webhook: GraphQL enrichment failed ({}): {}", resp.status, body[:200])
                    return None
                data = await resp.json()
        except Exception as e:
            logger.warning("Webhook: GraphQL enrichment error: {}", e)
            return None

        # GitHub GraphQL can return 200 with errors (permissions, rate limits, invalid IDs)
        if data.get("errors"):
            logger.warning("Webhook: GraphQL enrichment errors: {}", data["errors"])

        node = (data.get("data") or {}).get("node") or {}
        content = node.get("content") or {}
        status_field = node.get("fieldValueByName") or {}

        issue_number = content.get("number")
        if not issue_number:
            logger.debug("Webhook: enrichment returned no issue number (item may not be an issue)")
            return None

        assignees = {n.get("login") for n in (content.get("assignees") or {}).get("nodes", []) if n.get("login")}
        labels = {n.get("name") for n in (content.get("labels") or {}).get("nodes", []) if n.get("name")}

        return {
            "item_node_id": item_node_id,
            "issue_number": issue_number,
            "issue_title": content.get("title", ""),
            "issue_body": content.get("body", ""),
            "assignees": assignees,
            "labels": labels,
            "status_name": status_field.get("name", ""),
        }

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
        *, event_type: str = "",
    ) -> str:
        """Combine source instructions from workspace with the webhook payload."""
        instructions = self._load_instructions(source)
        if not event_type:
            event_type = self._detect_event_type(payload, request)

        parts = []
        if instructions:
            parts.append(instructions)
        parts.append(f"## Webhook Event\n\n**Source:** {source}\n**Event:** {event_type}")
        parts.append(f"```json\n{json.dumps(payload, indent=2, default=str)}\n```")

        return "\n\n---\n\n".join(parts)

    def _build_project_context(
        self, source: str, enriched: dict[str, Any], workflow: str,
    ) -> str:
        """Build structured context for a projects_v2_item event.

        Instead of sending the raw payload, sends pre-fetched issue details
        and the resolved workflow name so the LLM can skip straight to execution.
        """
        instructions = self._load_instructions(source)
        labels_str = ", ".join(sorted(enriched["labels"])) if enriched["labels"] else "none"

        parts = []
        if instructions:
            parts.append(instructions)

        parts.append(
            f"## Webhook Event\n\n"
            f"**Source:** {source}\n"
            f"**Event:** projects_v2_item\n"
            f"**Status:** {enriched['status_name']}\n"
            f"**Workflow:** {workflow}"
        )

        parts.append(
            f"## Issue Details\n\n"
            f"- **Number:** {enriched['issue_number']}\n"
            f"- **Title:** {enriched['issue_title']}\n"
            f"- **Labels:** {labels_str}\n"
            f"- **Item Node ID:** {enriched['item_node_id']}\n\n"
            f"### Body\n\n{enriched['issue_body'] or '(empty)'}"
        )

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
