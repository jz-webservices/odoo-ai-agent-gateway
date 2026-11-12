from odoo import fields, models


class BlogPost(models.Model):
    _inherit = 'blog.post'

    ai_agent_id = fields.Many2one(
        'ai.agent', string='Created by AI agent', readonly=True, copy=False,
        index='btree_not_null', ondelete='set null',
        help='Draft created through the AI Agent Gateway. The agent may only edit '
             'its own drafts and only while they are unpublished.')
