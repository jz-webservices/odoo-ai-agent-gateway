# Odoo AI Agent Gateway

**Let AI agents work in Odoo without giving them an API key.**

AI agents (Claude, n8n, custom scripts, …) usually get a full Odoo API key – and with it every right of
the user behind it. `ai_agent_gateway` gives each agent **its own token** and a **small HTTP API with a
fixed list of actions**. Publishing always stays with a human.

> Status: early version, extracted from a module that runs in production on
> [jzwebservices.com](https://www.jzwebservices.com), where AI agents write and fact-check blog drafts every week.

## What an agent can do – and nothing more

| Capability | Actions | Scope |
|---|---|---|
| Read posts | `tags`, `posts.search` | only the blogs you select |
| Create and edit drafts | `posts.create`, `posts.write` | drafts are always unpublished; an agent edits only **its own** drafts, only until they are published |
| Attachments | `attachments.list/read/create` | images and HTML backups on its own drafts, plus files you explicitly share |
| Send mails | `mail.send` | one fixed recipient, daily limit |
| – | `me` | the agent's own permissions |

Every agent has its own permissions, rate limit and call log. HTML is sanitized, uploads are checked
(type, size, magic bytes), tokens are stored only as SHA-256 hashes and can be replaced or revoked.

## Quick start

1. Install the module (Odoo 19, Community or Enterprise – depends only on `website_blog` and `mail`).
2. **Settings → AI Agents → Agents → New**: tick the capabilities, choose blogs, click **Generate token**.
3. Call the gateway:

```bash
curl -s https://<your-odoo>/ai-agent-gateway/v1/me \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" -d '{}'
```

Answers are `{"result": …}` or `{"error": "…"}` with a matching HTTP status (401, 403, 404, 429, …).

More documentation (security model, agent examples) follows.

## License

[LGPL-3.0](LICENSE), like Odoo Community. © JZ Webservices.
