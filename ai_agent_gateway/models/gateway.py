"""AI Agent Gateway: everything an AI agent may do, and nothing more.

Agents call POST /ai-agent-gateway/v1/<action> with their own token (see the
controller). The token belongs to an ``ai.agent`` record, not to an Odoo
user, so it has no rights anywhere else. Each action checks its input against
fixed rules and the agent's scope, then works with sudo().
"""
import base64
import binascii
import json
import logging
import re

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html_sanitize

from .ai_agent import TOKEN_PREFIX, hash_token

_logger = logging.getLogger(__name__)

POST_READ_FIELDS = {
    'id', 'name', 'subtitle', 'content', 'website_url', 'website_meta_title',
    'website_meta_description', 'tag_ids', 'blog_id', 'create_date',
    'post_date', 'is_published', 'ai_agent_id',
}
POST_WRITE_FIELDS = {
    'name', 'subtitle', 'content', 'website_meta_title',
    'website_meta_description', 'cover_properties', 'tag_ids',
}
TEXT_LIMITS = {
    'name': 200, 'subtitle': 300, 'website_meta_title': 100,
    'website_meta_description': 300,
}
CONTENT_LIMIT = 300_000             # characters of HTML
ATTACHMENT_LIMIT = 8 * 1024 * 1024  # decoded bytes
ATTACHMENT_TYPES = {
    # mimetype: (magic bytes, may be public)
    'image/jpeg': (b'\xff\xd8\xff', True),
    'image/png': (b'\x89PNG\r\n\x1a\n', True),
    'image/webp': (b'RIFF', True),
    'text/html': (None, False),
}
ATTACHMENT_NAME = re.compile(r'^[\w.\-]{1,120}$')
COVER_KEYS = {
    'background-image', 'background_color_class', 'background_color_style',
    'opacity', 'resize_class', 'text_align_class',
}
COVER_IMAGE = re.compile(r'^(none|url\(/web/image/\d+(/[\w.\-]+)?\))$')
COVER_VALUE = re.compile(r'^[\w .\-]*$')
ORDERS = {'create_date desc', 'post_date desc'}
MAX_SEARCH_LIMIT = 30


class GatewayError(Exception):
    """Rejected request; the message goes back to the agent."""

    def __init__(self, message, status=400, agent=None):
        super().__init__(message)
        self.status = status
        self.agent = agent


# ----------------------------------------------------------------------
# Input checks
# ----------------------------------------------------------------------

def _int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewayError(f'"{name}" must be an integer')
    return value


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip():
        raise GatewayError(f'"{name}" must be a non-empty string')
    if len(value) > limit:
        raise GatewayError(f'"{name}" is longer than {limit} characters')
    return value.strip()


def _clean_html(html, limit=CONTENT_LIMIT):
    if not isinstance(html, str) or not html.strip():
        raise GatewayError('HTML must be a non-empty string')
    if len(html) > limit:
        raise GatewayError(f'HTML is longer than {limit} characters')
    return html_sanitize(
        html, sanitize_attributes=True, sanitize_style=True, strip_style=True,
        strip_classes=False, sanitize_form=True)


def _clean_cover(value):
    try:
        cover = json.loads(value) if isinstance(value, str) else None
    except ValueError:
        cover = None
    if not isinstance(cover, dict):
        raise GatewayError('"cover_properties" must be a JSON object string')
    unknown = set(cover) - COVER_KEYS
    if unknown:
        raise GatewayError(f'"cover_properties" has unknown keys: {sorted(unknown)}')
    for key, val in cover.items():
        pattern = COVER_IMAGE if key == 'background-image' else COVER_VALUE
        if not isinstance(val, str) or not pattern.match(val):
            raise GatewayError(f'"cover_properties.{key}" has an invalid value')
    return json.dumps(cover)


def _json_safe(row):
    out = {}
    for key, val in row.items():
        if isinstance(val, tuple):            # many2one -> [id, name]
            val = list(val)
        elif hasattr(val, 'isoformat'):
            val = fields.Datetime.to_string(val)
        out[key] = val
    return out


def _summary(payload, result):
    keys = ('id', 'post_id', 'blog_id', 'name', 'subject')
    shown = {k: payload[k] for k in keys
             if isinstance(payload, dict) and k in payload
             and isinstance(payload[k], (int, str)) and not isinstance(payload[k], bool)}
    if isinstance(result, dict) and 'id' in result:
        shown['result_id'] = result['id']
    return json.dumps(shown, ensure_ascii=False)


class AiAgentGateway(models.AbstractModel):
    _name = 'ai.agent.gateway'
    _description = 'AI Agent Gateway'

    # action: (capability field on ai.agent or None, method)
    ACTIONS = {
        'me': (None, '_action_me'),
        'tags': ('can_read_posts', '_action_tags'),
        'posts.search': ('can_read_posts', '_action_posts_search'),
        'posts.create': ('can_write_drafts', '_action_posts_create'),
        'posts.write': ('can_write_drafts', '_action_posts_write'),
        'attachments.list': ('can_use_attachments', '_action_attachments_list'),
        'attachments.read': ('can_use_attachments', '_action_attachments_read'),
        'attachments.create': ('can_use_attachments', '_action_attachments_create'),
        'mail.send': ('can_send_mail', '_action_mail_send'),
    }

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @api.model
    def _authenticate(self, token):
        if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
            raise GatewayError('Invalid token', 401)
        agent = self.env['ai.agent'].sudo().search([('token_hash', '=', hash_token(token))], limit=1)
        if not agent:
            raise GatewayError('Invalid token', 401)
        return agent

    @api.model
    def dispatch(self, action, token, payload, remote_addr=None):
        """Check the token, run one action, log it. Raises GatewayError."""
        agent = self._authenticate(token)
        try:
            if action not in self.ACTIONS:
                raise GatewayError(f'Unknown action "{action}". Allowed: {sorted(self.ACTIONS)}', 404)
            capability, method = self.ACTIONS[action]
            if capability and not agent[capability]:
                raise GatewayError(f'This agent is not allowed to use "{action}"', 403)
            if not isinstance(payload, dict):
                raise GatewayError('Request body must be a JSON object')
            if agent._limit_reached():
                raise GatewayError(
                    f'Request limit reached ({agent.request_limit_per_hour} per hour)', 429)
            gateway = self.sudo().with_context(lang='en_US')
            result = getattr(gateway, method)(agent, payload)
        except GatewayError as e:
            e.agent = agent
            raise
        except UserError as e:      # also ValidationError / AccessError
            raise GatewayError(str(e), 400, agent) from e
        self.env['ai.agent.log']._record(agent, action, 200, _summary(payload, result), '', remote_addr)
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _own_draft(self, agent, post_id):
        """A draft this agent created itself and that is still unpublished."""
        post = self.env['blog.post'].browse(_int(post_id, 'post_id')).exists()
        if not post or post.ai_agent_id != agent:
            raise GatewayError('Post not found or not created by this agent', 404)
        if post.is_published:
            raise GatewayError('Post is published - the agent cannot change it any more', 403)
        return post

    def _post_vals(self, payload, allowed):
        unknown = set(payload) - allowed
        if unknown:
            raise GatewayError(f'Fields not allowed: {sorted(unknown)}')
        vals = {}
        for key, limit in TEXT_LIMITS.items():
            if key in payload:
                vals[key] = _text(payload[key], key, limit)
        if 'content' in payload:
            vals['content'] = _clean_html(payload['content'])
        if 'cover_properties' in payload:
            vals['cover_properties'] = _clean_cover(payload['cover_properties'])
        if 'tag_ids' in payload:
            tag_ids = payload['tag_ids']
            if not isinstance(tag_ids, list):
                raise GatewayError('"tag_ids" must be a list of tag ids')
            tag_ids = [_int(t, 'tag_ids') for t in tag_ids]
            tags = self.env['blog.tag'].browse(tag_ids).exists()
            if len(tags) != len(set(tag_ids)):
                raise GatewayError('"tag_ids" contains unknown tags (agents cannot create tags)')
            vals['tag_ids'] = [fields.Command.set(tags.ids)]
        return vals

    def _post_data(self, posts, field_names):
        rows = posts.read([f for f in field_names if f != 'website_url'])
        if 'website_url' in field_names:
            urls = {p.id: p.website_url for p in posts}
            for row in rows:
                row['website_url'] = urls[row['id']]
        return [_json_safe(row) for row in rows]

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _action_me(self, agent, payload):
        """What this agent may do - lets an agent check its own setup."""
        return {
            'name': agent.name,
            'actions': sorted(a for a, (cap, _m) in self.ACTIONS.items() if not cap or agent[cap]),
            'readable_blogs': [{'id': b.id, 'name': b.name}
                               for b in agent.read_blog_ids | agent.write_blog_ids],
            'draft_blogs': [{'id': b.id, 'name': b.name} for b in agent.write_blog_ids],
            'shared_files': [{'id': a.id, 'name': a.name} for a in agent.attachment_ids],
            'mails_left_today': max(agent.mail_limit_per_day - agent._mails_sent_today(), 0)
            if agent.can_send_mail else 0,
        }

    def _action_tags(self, agent, payload):
        return {'tags': self.env['blog.tag'].search_read([], ['id', 'name'], order='id')}

    def _action_posts_search(self, agent, payload):
        blog_id = _int(payload.get('blog_id'), 'blog_id')
        if blog_id not in (agent.read_blog_ids | agent.write_blog_ids).ids:
            raise GatewayError(f'Blog {blog_id} is not readable for this agent', 403)
        domain = [('blog_id', '=', blog_id)]
        if payload.get('published') is not None:
            domain.append(('is_published', '=', bool(payload['published'])))
        if payload.get('name_like'):
            domain.append(('name', '=like', _text(payload['name_like'], 'name_like', 200)))
        if payload.get('created_after'):
            try:
                after = fields.Datetime.to_datetime(payload['created_after'])
            except (TypeError, ValueError):
                raise GatewayError('"created_after" must look like "YYYY-MM-DD HH:MM:SS" (UTC)')
            domain.append(('create_date', '>=', after))
        field_names = payload.get('fields') or ['id', 'name', 'subtitle', 'create_date', 'is_published']
        if not isinstance(field_names, list) or set(field_names) - POST_READ_FIELDS:
            raise GatewayError(f'"fields" may only contain: {sorted(POST_READ_FIELDS)}')
        order = payload.get('order') or 'create_date desc'
        if order not in ORDERS:
            raise GatewayError(f'"order" must be one of {sorted(ORDERS)}')
        limit = payload.get('limit') or 10
        if _int(limit, 'limit') < 1 or limit > MAX_SEARCH_LIMIT:
            raise GatewayError(f'"limit" must be between 1 and {MAX_SEARCH_LIMIT}')
        posts = self.env['blog.post'].search(domain, order=order, limit=limit)
        return {'posts': self._post_data(posts, ['id'] + field_names)}

    def _action_posts_create(self, agent, payload):
        payload = dict(payload)
        blog_id = _int(payload.pop('blog_id', None), 'blog_id')
        if blog_id not in agent.write_blog_ids.ids:
            raise GatewayError(f'This agent may not create posts in blog {blog_id}', 403)
        for required in ('name', 'content'):
            if required not in payload:
                raise GatewayError(f'"{required}" is required')
        vals = self._post_vals(payload, POST_WRITE_FIELDS)
        vals.update({
            'blog_id': blog_id,
            'is_published': False,
            'ai_agent_id': agent.id,
            'author_id': agent.author_id.id or self.env.ref('base.partner_root').id,
        })
        post = self.env['blog.post'].create(vals)
        return {'id': post.id, 'website_url': post.website_url, 'is_published': post.is_published}

    def _action_posts_write(self, agent, payload):
        payload = dict(payload)
        post = self._own_draft(agent, payload.pop('id', None))
        vals = self._post_vals(payload.get('vals') or {}, POST_WRITE_FIELDS)
        if not vals:
            raise GatewayError('"vals" is empty')
        post.write(vals)
        return {'id': post.id, 'written': sorted(vals), 'is_published': post.is_published}

    def _action_attachments_list(self, agent, payload):
        post = self._own_draft(agent, payload.get('post_id'))
        attachments = self.env['ir.attachment'].search_read(
            [('res_model', '=', 'blog.post'), ('res_id', '=', post.id)],
            ['id', 'name', 'mimetype', 'public', 'create_date'], order='id')
        return {'attachments': [_json_safe(a) for a in attachments]}

    def _action_attachments_read(self, agent, payload):
        att_id = _int(payload.get('id'), 'id')
        attachment = self.env['ir.attachment'].browse(att_id).exists()
        allowed = attachment in agent.attachment_ids
        if attachment and not allowed and attachment.res_model == 'blog.post':
            post = self.env['blog.post'].browse(attachment.res_id).exists()
            allowed = bool(post and post.ai_agent_id == agent and not post.is_published)
        if not attachment or not allowed:
            raise GatewayError(f'Attachment {att_id} is not readable for this agent', 403)
        return {'id': attachment.id, 'name': attachment.name,
                'mimetype': attachment.mimetype, 'datas': (attachment.datas or b'').decode()}

    def _action_attachments_create(self, agent, payload):
        post = self._own_draft(agent, payload.get('post_id'))
        name = payload.get('name')
        if not isinstance(name, str) or not ATTACHMENT_NAME.match(name):
            raise GatewayError('"name" may only contain letters, digits, "." "-" "_" (max 120)')
        mimetype = payload.get('mimetype')
        if mimetype not in ATTACHMENT_TYPES:
            raise GatewayError(f'"mimetype" must be one of {sorted(ATTACHMENT_TYPES)}')
        magic, may_be_public = ATTACHMENT_TYPES[mimetype]
        try:
            raw = base64.b64decode(payload.get('datas') or '', validate=True)
        except (binascii.Error, ValueError, TypeError):
            raise GatewayError('"datas" must be base64')
        if not raw or len(raw) > ATTACHMENT_LIMIT:
            raise GatewayError(f'File is empty or larger than {ATTACHMENT_LIMIT // (1024 * 1024)} MB')
        if magic and not raw.startswith(magic):
            raise GatewayError(f'File content is not {mimetype}')
        public = bool(payload.get('public'))
        if public and not may_be_public:
            raise GatewayError(f'{mimetype} attachments cannot be public')
        attachment = self.env['ir.attachment'].create({
            'name': name, 'mimetype': mimetype, 'public': public,
            'res_model': 'blog.post', 'res_id': post.id, 'raw': raw,
        })
        return {'id': attachment.id, 'name': attachment.name, 'public': attachment.public}

    def _action_mail_send(self, agent, payload):
        subject = _text(payload.get('subject'), 'subject', 250)
        body = _clean_html(payload.get('body_html'), 200_000)
        if not agent.mail_from or not agent.mail_to:
            raise GatewayError('Mail is not configured for this agent', 503)
        if agent._mails_sent_today() >= agent.mail_limit_per_day:
            raise GatewayError(f'Mail limit reached ({agent.mail_limit_per_day} per 24 hours)', 429)
        mail = self.env['mail.mail'].create({
            'subject': subject, 'body_html': body, 'auto_delete': False,
            'email_from': agent.mail_from, 'email_to': agent.mail_to,
        })
        mail.send()
        return {'id': mail.id, 'state': mail.state, 'to': agent.mail_to}
