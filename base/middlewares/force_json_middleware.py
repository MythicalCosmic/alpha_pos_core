import json
from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin


class JSONOnlyMiddleware(MiddlewareMixin):
    EXEMPT_PREFIXES = ('/admin', '/static', '/media')

    def _is_exempt(self, path):
        return any(path.startswith(p) for p in self.EXEMPT_PREFIXES)

    def process_request(self, request):
        if self._is_exempt(request.path):
            return None
        if 'application/json' not in request.META.get('HTTP_ACCEPT', ''):
            request.META['HTTP_ACCEPT'] = 'application/json'
        return None

    def process_response(self, request, response):
        if self._is_exempt(request.path):
            return response
        if isinstance(response, JsonResponse):
            return response

        status_code = response.status_code

        if 200 <= status_code < 300:
            return response

        if 300 <= status_code < 400:
            return response

        # StreamingHttpResponse and FileResponse don't expose .content; we
        # can't safely re-wrap them, so pass through unchanged.
        if getattr(response, 'streaming', False):
            return response

        status_message = self._reason_phrase(status_code)
        try:
            content = None
            if hasattr(response, 'content') and response.content:
                try:
                    content = json.loads(response.content)
                except (json.JSONDecodeError, ValueError):
                    content = response.content.decode('utf-8', errors='ignore')

            json_data = {
                "status": status_message,
                "status_code": status_code,
                "success": False,
                "data": content,
                "meta": {
                    "path": request.path,
                    "method": request.method,
                    "timestamp": self._get_timestamp(),
                },
            }

            return JsonResponse(
                json_data,
                status=status_code,
                safe=False,
            )

        except Exception as e:
            import logging
            from django.conf import settings as django_settings
            logging.getLogger(__name__).exception(
                'JSONOnlyMiddleware: error wrapping response for %s %s',
                request.method, request.path,
            )
            error_detail = str(e) if getattr(django_settings, 'DEBUG', False) else (
                'An internal server error occurred'
            )
            return JsonResponse({
                "status": "Internal server error",
                "status_code": 500,
                "success": False,
                "error": error_detail,
                "meta": {
                    "path": request.path,
                    "method": request.method,
                    "timestamp": self._get_timestamp(),
                },
            }, status=500)
    
    def process_exception(self, request, exception):
        import logging
        from django.conf import settings as django_settings
        if self._is_exempt(request.path):
            return None
        logging.getLogger(__name__).exception("Unhandled exception in %s %s", request.method, request.path)
        error_detail = {
            "type": exception.__class__.__name__,
            "message": str(exception),
        } if getattr(django_settings, 'DEBUG', False) else {
            "message": "An internal server error occurred",
        }
        return JsonResponse({
            "status": "Internal server error",
            "status_code": 500,
            "success": False,
            "error": error_detail,
            "meta": {
                "path": request.path,
                "method": request.method,
                "timestamp": self._get_timestamp()
            }
        }, status=500)
    
    # Standard HTTP reason phrases — keep this map small and predictable so
    # API consumers can switch on it.
    _REASON_PHRASES = {
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        408: "Request Timeout",
        409: "Conflict",
        410: "Gone",
        422: "Unprocessable Entity",
        429: "Too Many Requests",
        500: "Internal Server Error",
        501: "Not Implemented",
        502: "Bad Gateway",
        503: "Service Unavailable",
        504: "Gateway Timeout",
    }

    def _reason_phrase(self, status_code):
        return self._REASON_PHRASES.get(status_code, f"HTTP {status_code}")
    
    def _get_timestamp(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


