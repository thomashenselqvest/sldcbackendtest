---
name: turnstone-admin
description: Use this skill to administer Turnstone itself: settings, models, MCP servers, skills, personas, tool and prompt policies, users, roles, API tokens, schedules, channels, memories, usage and audit. It covers everything the admin web UI can do, through the console and server REST APIs and the Turnstone Python SDK. Trigger phrases: "change a Turnstone setting", "add a model", "register an MCP server", "create a persona", "add a skill", "give user X admin", "create an API token", "turn on auto-approve".
version: 1.0.0
---

# Administering Turnstone

You act as the `turnstone-agent` admin user. Everything you change is real and takes effect for all users of this Turnstone deployment, so make the change the user asked for and nothing more. Before you delete or overwrite something, read its current state and say what you will change.

## Tooling

All calls go through `${TURNSTONE_SKILL_DIR}/scripts/tsadmin.py`. It loads the admin token and service URLs from this skill's `assets/` files.

- **Console API** (default, `http://turnstone-console:8090`): all `/v1/api/admin/*` endpoints. This is what the admin web UI uses.
- **Server API** (`--server`, `http://turnstone-server:8080`): workstreams, sends, approvals, workstream-scoped settings.

```bash
T=${TURNSTONE_SKILL_DIR}/scripts/tsadmin.py
python3 $T whoami                                  # confirm identity and permissions
python3 $T endpoints personas                      # find endpoints (filter is a substring)
python3 $T describe POST /v1/api/admin/personas    # parameters + request body schema
python3 $T call GET /v1/api/admin/settings
python3 $T call PUT /v1/api/admin/settings/tools.search --data '{"value": "off"}'
python3 $T call POST /v1/api/admin/personas --data @/tmp/persona.json
python3 $T endpoints workstreams --server
```

Always run `endpoints` and `describe` before calling an endpoint you have not used in this session. Do not guess field names. The OpenAPI spec is the source of truth, and it covers every admin feature.

For multi-step work, use the typed Python SDK with the same credentials:

```python
import sys; sys.path.insert(0, "${TURNSTONE_SKILL_DIR}/scripts")
from tsadmin import console, server, call

with console() as c:
    print(c.list_settings())
    c.update_setting("model.default_alias", "opus")
    c.create_policy(...)   # list_/create_/update_/delete_ exist for: skills, policies, roles,
                           # schedules, settings, mcp_servers; plus get_usage, get_audit, memories
# No SDK method for an area (personas, model definitions, users, tokens, prompt policies,
# channels, tls, orgs, ...)? Use call():
call("GET", "/v1/api/admin/personas")
```

## Areas

| Area | Endpoint prefix (console unless noted) |
|---|---|
| Settings | `/v1/api/admin/settings`, `/v1/api/admin/settings/{key}` (`{"value": ...}`). The schema is at `.../settings/schema`. |
| Models | `/v1/api/admin/model-definitions` |
| MCP servers | `/v1/api/admin/mcp-servers`, registry at `/v1/api/admin/mcp-registry` |
| Skills | `/v1/api/admin/skills` (+ `/resources`, `/discover`, `/install`) |
| Personas | `/v1/api/admin/personas` |
| Tool policies / prompt policies | `/v1/api/admin/policies`, `/v1/api/admin/prompt-policies` |
| Users, roles, tokens, OIDC | `/v1/api/admin/users`, `/roles`, `/tokens`, `/oidc-identities` |
| Schedules, channels | `/v1/api/admin/schedules`, `/v1/api/admin/channels` |
| Memories, usage, audit, verdicts | `/v1/api/admin/memories`, `/usage`, `/audit`, `/verdicts`, `/output-assessments` |
| Nodes, TLS, orgs | `/v1/api/admin/nodes`, `/node-metadata`, `/tls`, `/orgs` |

## Gotchas

- **Model definitions** don't reach the server nodes until you call `POST /v1/api/admin/model-definitions/reload`. Without that, new aliases fail with "Unknown model alias". Check the reload response: it lists the aliases each node loaded.
- **Anthropic model aliases.** Use `provider: "anthropic"` with the plain model ID, e.g. `claude-opus-5-5`. If a model has no built-in profile, set `capabilities` overrides on the definition: `thinking_mode`, `effort_levels`, `context_window`, `max_output_tokens`, `supports_temperature`.
- **MCP servers.** After a change, `POST /v1/api/admin/mcp-servers/reload`, then `GET /v1/api/admin/mcp-servers` and check `status.<node>.connected` and the tool count.
- **`tools.skip_permissions`** is read only at server startup and applies only to workstreams created afterwards. The API reports `restart_required: false`, which is wrong for this setting. Tell the user that a restart of `deploy/turnstone-server` is needed. You cannot restart it from here.
- **Settings precedence:** database value > `config.toml` > default. `DELETE /v1/api/admin/settings/{key}` reverts to the lower layers.
- **Personas** are stamped onto a workstream when it's created. Editing a persona never changes existing workstreams.
- **Skills** have a 32 KB content limit. Skills installed from a registry are read-only except for runtime config.
- **Secrets.** Never print API keys or tokens back to the user. GET responses mask them unless you pass `?reveal=true`; don't use that unless asked.
- **Your own access.** Don't delete or demote the `turnstone-agent` user or revoke its token. That would lock this skill out.

## After every change

Read the object back, or run the matching `list`/`GET`, to confirm the change took effect. Then report in one line what changed: old value → new value.
