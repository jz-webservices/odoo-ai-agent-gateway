import base64
import json

from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from ..models.gateway import GatewayError

JPEG = b'\xff\xd8\xff\xe0' + b'0' * 100
COVER = json.dumps({
    'background-image': 'url(/web/image/1/cover.webp)',
    'background_color_class': 'o_cc5 o_cc', 'background_color_style': '',
    'opacity': '0.2', 'resize_class': 'o_half_screen_height o_record_has_cover',
    'text_align_class': '',
})
ALL_RIGHTS = {'can_read_posts': True, 'can_write_drafts': True,
              'can_use_attachments': True, 'can_send_mail': True}


class GatewayCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Gateway = cls.env['ai.agent.gateway']
        cls.news = cls.env['blog.blog'].create({'name': 'News'})
        cls.readonly_blog = cls.env['blog.blog'].create({'name': 'Odoo'})
        cls.other_blog = cls.env['blog.blog'].create({'name': 'Private'})
        cls.tag = cls.env['blog.tag'].create({'name': 'AI'})
        cls.author = cls.env['res.partner'].create({'name': 'Editor'})
        cls.kit = cls.env['ir.attachment'].create({'name': 'kit.zip', 'raw': b'PK kit'})
        cls.secret = cls.env['ir.attachment'].create({'name': 'secret.pdf', 'raw': b'secret'})
        cls.agent = cls.env['ai.agent'].create({
            'name': 'Writer', **ALL_RIGHTS,
            'read_blog_ids': [(6, 0, cls.readonly_blog.ids)],
            'write_blog_ids': [(6, 0, cls.news.ids)],
            'attachment_ids': [(6, 0, cls.kit.ids)],
            'author_id': cls.author.id,
            'mail_from': 'Agent <agent@example.com>',
            'mail_to': 'editor@example.com',
        })
        cls.token = cls.agent._generate_token()

    def call(self, action, token=None, **payload):
        return self.Gateway.dispatch(action, token or self.token, payload)

    def create_draft(self, token=None, **vals):
        payload = {'blog_id': self.news.id, 'name': 'Weekly Tech News',
                   'content': '<p>Hello</p>', **vals}
        return self.call('posts.create', token, **payload)['id']

    def assertRejected(self, status, action, token=None, **payload):
        with self.assertRaises(GatewayError) as e:
            self.call(action, token, **payload)
        self.assertEqual(e.exception.status, status, str(e.exception))
        return e.exception


class TestToken(GatewayCase):

    def test_wrong_or_missing_token(self):
        for token in (None, '', 'aag_wrong', 'not-a-gateway-token'):
            with self.assertRaises(GatewayError) as e:
                self.Gateway.dispatch('me', token, {})
            self.assertEqual(e.exception.status, 401)

    def test_new_token_replaces_old(self):
        old = self.token
        new = self.agent._generate_token()
        self.assertTrue(self.call('me', new))
        self.assertRejected(401, 'me', old)

    def test_only_hash_is_stored(self):
        self.assertNotIn(self.token, self.agent.sudo().token_hash)
        self.assertEqual(len(self.agent.sudo().token_hash), 64)
        self.assertEqual(self.agent.token_hint, '…' + self.token[-4:])

    def test_revoke_and_archive(self):
        self.agent.action_revoke_token()
        self.assertRejected(401, 'me')
        token = self.agent._generate_token()
        self.agent.active = False
        self.assertRejected(401, 'me', token)

    def test_token_management_needs_admin(self):
        user = new_test_user(self.env, 'blogger', groups='base.group_user')
        with self.assertRaises(AccessError):
            self.agent.with_user(user)._generate_token()

    def test_unknown_action(self):
        self.assertRejected(404, 'res.users.write')


class TestCapabilities(GatewayCase):

    def test_me_lists_scope(self):
        me = self.call('me')
        self.assertEqual(me['name'], 'Writer')
        self.assertIn('posts.create', me['actions'])
        self.assertEqual({b['id'] for b in me['readable_blogs']}, {self.news.id, self.readonly_blog.id})
        self.assertEqual(me['mails_left_today'], 3)

    def test_missing_capability_is_forbidden(self):
        reader = self.env['ai.agent'].create({
            'name': 'Reader', 'can_read_posts': True,
            'read_blog_ids': [(6, 0, self.news.ids)]})
        token = reader._generate_token()
        self.assertTrue(self.call('posts.search', token, blog_id=self.news.id) is not None)
        self.assertRejected(403, 'posts.create', token, blog_id=self.news.id, name='x', content='<p>x</p>')
        self.assertRejected(403, 'mail.send', token, subject='x', body_html='<p>x</p>')
        self.assertEqual(self.call('me', token)['actions'], ['me', 'posts.search', 'tags'])

    def test_agents_cannot_touch_each_others_drafts(self):
        other = self.env['ai.agent'].create({
            'name': 'Other', **ALL_RIGHTS, 'write_blog_ids': [(6, 0, self.news.ids)]})
        other_token = other._generate_token()
        post_id = self.create_draft()
        self.assertRejected(404, 'posts.write', other_token, id=post_id, vals={'subtitle': 'x'})
        self.assertRejected(404, 'attachments.list', other_token, post_id=post_id)

    def test_request_limit(self):
        self.agent.request_limit_per_hour = 2
        self.call('me')
        self.call('me')
        self.assertRejected(429, 'me')

    def test_successful_calls_are_logged(self):
        post_id = self.create_draft()
        log = self.env['ai.agent.log'].search([('agent_id', '=', self.agent.id)], limit=1)
        self.assertEqual(log.action, 'posts.create')
        self.assertTrue(log.success)
        self.assertIn(str(post_id), log.summary)
        self.assertNotIn('Hello', log.summary)   # content is never logged
        self.assertTrue(self.agent.last_call)


class TestPosts(GatewayCase):

    def test_create_is_always_unpublished_draft(self):
        post_id = self.create_draft(subtitle='Sub', tag_ids=[self.tag.id], cover_properties=COVER)
        post = self.env['blog.post'].browse(post_id)
        self.assertFalse(post.is_published)
        self.assertEqual(post.ai_agent_id, self.agent)
        self.assertEqual(post.author_id, self.author)
        self.assertEqual(post.tag_ids, self.tag)

    def test_cannot_set_publish_or_other_fields(self):
        for field in ('is_published', 'website_published', 'author_id', 'website_id',
                      'active', 'ai_agent_id'):
            with self.assertRaises(GatewayError):
                self.create_draft(**{field: True})

    def test_create_only_in_write_blogs(self):
        for blog in (self.readonly_blog, self.other_blog):
            self.assertRejected(403, 'posts.create', blog_id=blog.id, name='x', content='<p>x</p>')

    def test_html_is_sanitized(self):
        post_id = self.create_draft(content=(
            '<section class="s_text_block"><p onclick="x()">Text</p>'
            '<script>alert(1)</script><a href="javascript:alert(2)">j</a>'
            '<img src="/web/image/1/a.png" onerror="alert(3)"></section>'))
        content = self.env['blog.post'].browse(post_id).content
        for bad in ('<script', 'onclick', 'onerror', 'javascript:'):
            self.assertNotIn(bad, content)
        self.assertIn('s_text_block', content)
        self.assertIn('/web/image/1/a.png', content)

    def test_cover_properties_validated(self):
        bad_covers = [
            'not json',
            json.dumps({'background-image': 'url(https://evil.example/x.png)'}),
            json.dumps({'background-image': 'url(/web/image/1/x.jpg);color:red'}),
            json.dumps({'onclick': 'x'}),
            json.dumps({'opacity': '0.2" onmouseover="x'}),
        ]
        for cover in bad_covers:
            with self.assertRaises(GatewayError):
                self.create_draft(cover_properties=cover)

    def test_unknown_tags_rejected(self):
        with self.assertRaises(GatewayError):
            self.create_draft(tag_ids=[999999])

    def test_write_own_draft(self):
        post_id = self.create_draft()
        self.call('posts.write', id=post_id, vals={'subtitle': 'Fixed', 'content': '<p>New</p>'})
        post = self.env['blog.post'].browse(post_id)
        self.assertEqual(post.subtitle, 'Fixed')
        self.assertIn('New', post.content)

    def test_cannot_write_foreign_or_published_posts(self):
        manual = self.env['blog.post'].create({'name': 'Manual', 'blog_id': self.news.id})
        self.assertRejected(404, 'posts.write', id=manual.id, vals={'subtitle': 'x'})
        post_id = self.create_draft()
        self.env['blog.post'].browse(post_id).is_published = True
        self.assertRejected(403, 'posts.write', id=post_id, vals={'subtitle': 'x'})

    def test_cannot_publish_via_write(self):
        post_id = self.create_draft()
        with self.assertRaises(GatewayError):
            self.call('posts.write', id=post_id, vals={'is_published': True})

    def test_search_only_readable_blogs_and_fields(self):
        self.create_draft()
        result = self.call('posts.search', blog_id=self.news.id, published=False,
                           name_like='Weekly%', fields=['name', 'content', 'website_url'])
        self.assertEqual(len(result['posts']), 1)
        self.assertIn('website_url', result['posts'][0])
        self.assertTrue(self.call('posts.search', blog_id=self.readonly_blog.id) is not None)
        self.assertRejected(403, 'posts.search', blog_id=self.other_blog.id)
        with self.assertRaises(GatewayError):
            self.call('posts.search', blog_id=self.news.id, fields=['create_uid', 'message_ids'])
        with self.assertRaises(GatewayError):
            self.call('posts.search', blog_id=self.news.id, limit=1000)


class TestAttachments(GatewayCase):

    def test_create_image_on_own_draft(self):
        post_id = self.create_draft()
        result = self.call('attachments.create', post_id=post_id, name=f'card-{post_id}.jpg',
                           mimetype='image/jpeg', public=True, datas=base64.b64encode(JPEG).decode())
        attachment = self.env['ir.attachment'].browse(result['id'])
        self.assertEqual((attachment.res_model, attachment.res_id), ('blog.post', post_id))
        self.assertTrue(attachment.public)
        listed = self.call('attachments.list', post_id=post_id)['attachments']
        self.assertEqual([a['id'] for a in listed], [attachment.id])

    def test_html_backup_stays_private(self):
        post_id = self.create_draft()
        datas = base64.b64encode(b'<p>original</p>').decode()
        with self.assertRaises(GatewayError):
            self.call('attachments.create', post_id=post_id, name='backup.html',
                      mimetype='text/html', public=True, datas=datas)
        result = self.call('attachments.create', post_id=post_id, name='backup.html',
                           mimetype='text/html', datas=datas)
        self.assertFalse(self.env['ir.attachment'].browse(result['id']).public)

    def test_attachment_checks(self):
        post_id = self.create_draft()
        ok = {'post_id': post_id, 'name': 'card.jpg', 'mimetype': 'image/jpeg',
              'datas': base64.b64encode(JPEG).decode()}
        for bad in ({'name': '../evil.jpg'}, {'mimetype': 'application/x-sh'},
                    {'mimetype': 'image/svg+xml'}, {'datas': 'not base64!'},
                    {'datas': base64.b64encode(b'<svg onload=x>').decode()}):
            with self.assertRaises(GatewayError):
                self.call('attachments.create', **{**ok, **bad})
        manual = self.env['blog.post'].create({'name': 'Manual', 'blog_id': self.news.id})
        with self.assertRaises(GatewayError):
            self.call('attachments.create', **{**ok, 'post_id': manual.id})

    def test_read_only_shared_files(self):
        self.assertEqual(base64.b64decode(self.call('attachments.read', id=self.kit.id)['datas']), b'PK kit')
        self.assertRejected(403, 'attachments.read', id=self.secret.id)


class TestMail(GatewayCase):

    def test_mail_goes_only_to_configured_address(self):
        result = self.call('mail.send', subject='Review', body_html='<p>Hello</p><script>x</script>',
                           email_to='someone@evil.example')
        mail = self.env['mail.mail'].browse(result['id'])
        self.assertEqual(mail.email_to, 'editor@example.com')
        self.assertNotIn('<script', str(mail.body_html))

    def test_mail_limit(self):
        self.agent.mail_limit_per_day = 2
        self.call('mail.send', subject='1', body_html='<p>1</p>')
        self.call('mail.send', subject='2', body_html='<p>2</p>')
        self.assertRejected(429, 'mail.send', subject='3', body_html='<p>3</p>')

    def test_mail_not_configured(self):
        self.agent.mail_to = False
        self.assertRejected(503, 'mail.send', subject='x', body_html='<p>x</p>')


@tagged('post_install', '-at_install')
class TestController(HttpCase):

    def setUp(self):
        super().setUp()
        self.blog = self.env['blog.blog'].create({'name': 'News'})
        self.agent = self.env['ai.agent'].create({
            'name': 'HTTP agent', 'can_read_posts': True,
            'read_blog_ids': [(6, 0, self.blog.ids)]})
        self.token = self.agent._generate_token()

    def post(self, action, payload, token=None):
        headers = {'Content-Type': 'application/json', 'X-Odoo-Database': self.env.cr.dbname}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        return self.url_open(f'/ai-agent-gateway/v1/{action}', data=json.dumps(payload), headers=headers)

    def test_http_auth_and_result(self):
        self.assertEqual(self.post('me', {}).status_code, 401)
        self.assertEqual(self.post('me', {}, 'aag_wrong').status_code, 401)
        response = self.post('tags', {}, self.token)
        self.assertEqual(response.status_code, 200)
        self.assertIn('tags', response.json()['result'])

    def test_http_error_is_json_and_logged(self):
        response = self.post('posts.search', {'blog_id': 999999}, self.token)
        self.assertEqual(response.status_code, 403)
        self.assertIn('error', response.json())
        response = self.post('posts.create', {}, self.token)
        self.assertEqual(response.status_code, 403)
        logs = self.env['ai.agent.log'].search([('agent_id', '=', self.agent.id)])
        self.assertEqual(set(logs.mapped('status')), {403})
        self.assertFalse(any(logs.mapped('success')))
