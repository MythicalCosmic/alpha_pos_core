"""Globally disable CSRF enforcement (trusted-LAN appliance mode).

When the desktop exposes the POS to the whole network, browser/form POSTs
arrive from arbitrary device IPs that CSRF_TRUSTED_ORIGINS can't wildcard.
Setting request._dont_enforce_csrf_checks (the same flag csrf_exempt sets)
before CsrfViewMiddleware runs makes it skip the Origin/Referer check for
every request. Only enabled when OPEN_LAN is on; auth + licensing still apply.
"""


class DisableCSRFMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request._dont_enforce_csrf_checks = True
        return self.get_response(request)
