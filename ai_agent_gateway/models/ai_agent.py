import hashlib
import logging
import secrets
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

_logger = logging.getLogger(__name__)

TOKEN_PREFIX = 'aag_'


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


class AiAgent(models.Model):
    """One AI agent = one token + exactly the rights listed on this record."""

    _name = 'ai.agent'
    _description = 'AI Agent'
    _order = 'name, id'

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    description = fields.Text(help='What this agent does, who runs it, where its prompt lives.')

    # Token: only the SHA-256 hash is stored, the token is shown once.
    token_hash = fields.Char(readonly=True, copy=False, index=True, groups='base.group_system')
    token_hint = fields.Char(
        readonly=True, copy=False,
        help='Last characters of the current token, to recognise it.')
    has_token = fields.Boolean(compute='_compute_has_token')

    # Capabilities
    can_read_posts = fields.Boolean(
        string='Read posts', help='Actions: tags, posts.search')
    can_write_drafts = fields.Boolean(
        string='Create and edit drafts', help='Actions: posts.create, posts.write')
    can_use_attachments = fields.Boolean(
        string='Attachments',
        help='Actions: attachments.list, attachments.read, attachments.create')
    can_send_mail = fields.Boolean(string='Send mail', help='Action: mail.send')

    # Scope
    read_blog_ids = fields.Many2many(
        'blog.blog', 'ai_agent_read_blog_rel', 'agent_id', 'blog_id',
        string='Readable blogs',
        help='Blogs the agent may search (blogs it may write to are readable too).')
    write_blog_ids = fields.Many2many(
        'blog.blog', 'ai_agent_write_blog_rel', 'agent_id', 'blog_id',
        string='Blogs for drafts')
    attachment_ids = fields.Many2many(
        'ir.attachment', 'ai_agent_attachment_rel', 'agent_id', 'attachment_id',
        string='Shared files',
        help='Files the agent may download, e.g. a style guide or a fact sheet.')
    edit_agent_ids = fields.Many2many(
        'ai.agent', 'ai_agent_edit_agent_rel', 'agent_id', 'other_agent_id',
        string='May edit drafts of',
        help='Unpublished drafts created by these agents may be edited by this agent too, '
             'e.g. a fact-checking agent that corrects the drafts of a writing agent.')
    author_id = fields.Many2one(
        'res.partner', string='Draft author',
        help='Author set on the drafts this agent creates.')

    # Mail
    mail_to = fields.Char(
        string='Mail recipient',
        help='The only address this agent can write to. The agent cannot change it.')
    mail_from = fields.Char(string='Mail sender')
    mail_limit_per_day = fields.Integer(string='Mails per 24 hours', default=3)

    # Limits and history
    request_limit_per_hour = fields.Integer(
        string='Requests per hour', default=300, help='0 = no limit.')
    post_ids = fields.One2many('blog.post', 'ai_agent_id', string='Drafts')
    post_count = fields.Integer(compute='_compute_counts')
    log_ids = fields.One2many('ai.agent.log', 'agent_id', string='Calls')
    log_count = fields.Integer(compute='_compute_counts')
    last_call = fields.Datetime(readonly=True, copy=False)

    @api.depends('token_hint')
    def _compute_has_token(self):
        for agent in self:
            agent.has_token = bool(agent.token_hint)

    def _compute_counts(self):
        for agent in self:
            agent.post_count = len(agent.post_ids)
            agent.log_count = self.env['ai.agent.log'].search_count([('agent_id', '=', agent.id)])

    @api.constrains('mail_limit_per_day', 'request_limit_per_hour')
    def _check_limits(self):
        for agent in self:
            if agent.mail_limit_per_day < 0 or agent.request_limit_per_hour < 0:
                raise ValidationError(_('Limits cannot be negative.'))

    # ------------------------------------------------------------------
    # Token
    # ------------------------------------------------------------------

    def _check_admin(self):
        if not self.env.user.has_group('base.group_system'):
            raise AccessError(_('Only administrators can manage AI agent tokens.'))

    def _generate_token(self):
        """Create a new token (the old one stops working) and return it."""
        self.ensure_one()
        self._check_admin()
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        self.sudo().write({'token_hash': hash_token(token), 'token_hint': '…' + token[-4:]})
        _logger.info('AI Agent Gateway: new token for agent %s by uid %s', self.id, self.env.uid)
        return token

    def action_generate_token(self):
        token = self._generate_token()
        wizard = self.env['ai.agent.token.wizard'].create({'agent_id': self.id, 'token': token})
        return {
            'type': 'ir.actions.act_window',
            'name': _('New token for %s', self.name),
            'res_model': 'ai.agent.token.wizard',
            'res_id': wizard.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_revoke_token(self):
        self._check_admin()
        self.sudo().write({'token_hash': False, 'token_hint': False})
        _logger.info('AI Agent Gateway: token revoked for agents %s by uid %s', self.ids, self.env.uid)

    # ------------------------------------------------------------------
    # Limits
    # ------------------------------------------------------------------

    def _calls_since(self, since, action=None, success=None):
        domain = [('agent_id', '=', self.id), ('create_date', '>=', since)]
        if action:
            domain.append(('action', '=', action))
        if success is not None:
            domain.append(('success', '=', success))
        return self.env['ai.agent.log'].sudo().search_count(domain)

    def _limit_reached(self):
        if not self.request_limit_per_hour:
            return False
        since = fields.Datetime.now() - timedelta(hours=1)
        return self._calls_since(since) >= self.request_limit_per_hour

    def _mails_sent_today(self):
        since = fields.Datetime.now() - timedelta(days=1)
        return self._calls_since(since, action='mail.send', success=True)

    # ------------------------------------------------------------------
    # Smart buttons
    # ------------------------------------------------------------------

    def action_view_posts(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Drafts of %s', self.name),
            'res_model': 'blog.post',
            'view_mode': 'list,form',
            'domain': [('ai_agent_id', '=', self.id)],
            'context': {'active_test': False},
        }

    def action_view_logs(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Calls of %s', self.name),
            'res_model': 'ai.agent.log',
            'view_mode': 'list,form',
            'domain': [('agent_id', '=', self.id)],
        }
