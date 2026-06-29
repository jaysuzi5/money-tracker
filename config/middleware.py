import logging
import time

logger = logging.getLogger('page')


class PageLoggingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        start = time.monotonic()
        response = self.get_response(request)
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            'request',
            extra={
                'ctx_method': request.method,
                'ctx_path': request.path,
                'ctx_status': response.status_code,
                'ctx_duration_ms': duration_ms,
                'ctx_user': getattr(request.user, 'username', 'anon'),
            },
        )
        return response
