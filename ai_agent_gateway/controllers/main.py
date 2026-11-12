import logging
import re

from odoo import http
from odoo.http import request

from ..models.gateway import GatewayError

_logger = logging.getLogger(__name__)

BEARER = re.compile(r'^bearer\s+(\S+)$', re.IGNORECASE)


class AiAgentGatewayController(http.Controller):

    @http.route('/ai-agent-gateway/v1/<string:action>', type='http', auth='public',
                methods=['POST'], csrf=False, save_session=False)
    def gateway(self, action, **kwargs):
        """POST JSON to /ai-agent-gateway/v1/<action> with "Authorization: Bearer <token>".

        Answers {"result": ...} or {"error": "..."} with a matching HTTP status.
        """
        match = BEARER.match(request.httprequest.headers.get('Authorization', ''))
        remote_addr = request.httprequest.remote_addr
        try:
            try:
                payload = request.get_json_data()
            except ValueError:
                raise GatewayError('Request body must be JSON')
            result = request.env['ai.agent.gateway'].dispatch(
                action, match and match.group(1), payload, remote_addr)
        except GatewayError as e:
            request.env.cr.rollback()
            if e.status == 401:
                _logger.warning('AI Agent Gateway: rejected token from %s', remote_addr)
            elif e.agent:
                # rejected calls are logged too (after the rollback, so the log stays)
                request.env['ai.agent.log']._record(e.agent, action, e.status, '', str(e), remote_addr)
            return request.make_json_response({'error': str(e)}, status=e.status)
        return request.make_json_response({'result': result})
