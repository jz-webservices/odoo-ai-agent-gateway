from datetime import timedelta

from odoo import api, fields, models

DEFAULT_RETENTION_DAYS = 90


class AiAgentLog(models.Model):
    """One row per authenticated call - successful or rejected."""

    _name = 'ai.agent.log'
    _description = 'AI Agent Call'
    _order = 'id desc'
    _rec_name = 'action'

    agent_id = fields.Many2one('ai.agent', required=True, ondelete='cascade', index=True, readonly=True)
    action = fields.Char(required=True, readonly=True)
    status = fields.Integer(string='HTTP status', readonly=True)
    success = fields.Boolean(readonly=True, index=True)
    summary = fields.Char(readonly=True, help='Ids and names from the request, never the content.')
    error = fields.Char(readonly=True)
    remote_addr = fields.Char(string='IP address', readonly=True)

    @api.model
    def _record(self, agent, action, status, summary='', error='', remote_addr=None):
        log = self.sudo().create({
            'agent_id': agent.id,
            'action': (action or '')[:64],
            'status': status,
            'success': status < 400,
            'summary': (summary or '')[:500],
            'error': (error or '')[:500],
            'remote_addr': remote_addr,
        })
        agent.sudo().last_call = log.create_date
        return log

    @api.autovacuum
    def _gc_old_calls(self):
        days = int(self.env['ir.config_parameter'].sudo().get_param(
            'ai_agent_gateway.log_retention_days', DEFAULT_RETENTION_DAYS) or DEFAULT_RETENTION_DAYS)
        self.sudo().search([('create_date', '<', fields.Datetime.now() - timedelta(days=days))]).unlink()
