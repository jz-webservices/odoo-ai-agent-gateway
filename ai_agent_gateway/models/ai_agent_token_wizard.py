from odoo import fields, models


class AiAgentTokenWizard(models.TransientModel):
    """Shows a new token exactly once. Transient rows are vacuumed by Odoo."""

    _name = 'ai.agent.token.wizard'
    _description = 'New AI Agent Token'

    agent_id = fields.Many2one('ai.agent', required=True, ondelete='cascade')
    token = fields.Char(readonly=True)

    def action_close(self):
        self.write({'token': False})
        return {'type': 'ir.actions.act_window_close'}
