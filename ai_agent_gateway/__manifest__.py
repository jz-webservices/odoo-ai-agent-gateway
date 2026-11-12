{
    'name': 'AI Agent Gateway',
    'version': '19.0.1.0.0',
    'category': 'Website/Website',
    'summary': 'Let AI agents work in Odoo through a narrow, token-protected API '
               'instead of a full API key',
    'description': """
AI Agent Gateway
================

AI agents (Claude, n8n, custom scripts, ...) often get a full Odoo API key and
can then do everything the user can do. This module gives every agent its own
token and a small HTTP API with a fixed list of actions:

* read posts and tags of the blogs you allow,
* create **unpublished** drafts in the blogs you allow,
* edit only its own drafts, and only while they are unpublished,
* attach images / HTML backups to its own drafts,
* read the files you explicitly share with it,
* send a limited number of mails to one fixed address.

Publishing stays with a human. HTML is sanitized, every call is logged and
each agent has its own rate limit.
""",
    'author': 'JZ Webservices',
    'website': 'https://www.jzwebservices.com',
    'license': 'LGPL-3',
    'depends': ['website_blog', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/ai_agent_views.xml',
        'views/ai_agent_log_views.xml',
        'views/blog_post_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': False,
}
