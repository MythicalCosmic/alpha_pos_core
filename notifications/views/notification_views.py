from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST
from base.helpers.request import parse_json_body, safe_page, safe_per_page
from base.helpers.response import json_response, ServiceResponse
from base.security.permissions import admin_required
from notifications.services.config_service import ConfigService
from notifications.services.sender_service import SenderService
from notifications.services.queue_service import QueueService
from notifications.services.safe_format import validate_template_text as _validate_template_text
from notifications.models import NotificationTemplate, NotificationLog


@csrf_exempt
@require_http_methods(["GET", "PUT"])
@admin_required
def settings_view(request):
    if request.method == "GET":
        return json_response(ConfigService.get_settings())

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    return json_response(ConfigService.update_settings(**data))


@csrf_exempt
@require_POST
@admin_required
def settings_test(request):
    settings = ConfigService.load()
    brand = settings.brand_name if settings else 'Alpha POS'
    # send_raw() enqueues the message and returns None, so wrap the outcome in a
    # ServiceResponse ourselves rather than feeding None into json_response.
    SenderService.send_raw(f"{brand} - test notification")
    return json_response(ServiceResponse.success(message='Test notification queued'))


@require_GET
@admin_required
def settings_status(request):
    return json_response(ConfigService.get_status())


@require_GET
@admin_required
def notification_types(request):
    templates = NotificationTemplate.objects.all()
    data = [
        {
            "id": t.id,
            "notification_type": t.notification_type,
            "name": t.name,
            "is_enabled": t.is_enabled,
        }
        for t in templates
    ]
    return JsonResponse({"success": True, "data": data}, status=200)


@csrf_exempt
@require_http_methods(["PUT"])
@admin_required
def notification_type_detail(request, type_slug):
    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    try:
        template = NotificationTemplate.objects.get(notification_type=type_slug)
    except NotificationTemplate.DoesNotExist:
        return JsonResponse(
            {"success": False, "message": "Notification type not found"}, status=404
        )

    if "is_enabled" in data:
        template.is_enabled = data["is_enabled"]
    if "template_text" in data:
        err = _validate_template_text(data["template_text"])
        if err:
            return JsonResponse(
                {"success": False, "message": err, "errors": {"template_text": err}},
                status=422,
            )
        template.template_text = data["template_text"]
    template.save()

    return JsonResponse(
        {
            "success": True,
            "message": "Updated",
            "data": {
                "id": template.id,
                "notification_type": template.notification_type,
                "name": template.name,
                "template_text": template.template_text,
                "is_enabled": template.is_enabled,
            },
        },
        status=200,
    )


def _serialize_template(t):
    return {
        "id": t.id,
        "notification_type": t.notification_type,
        "name": t.name,
        "template_text": t.template_text,
        "description": t.description,
        "is_enabled": t.is_enabled,
        "language": t.language,
    }


@csrf_exempt
@require_http_methods(["GET", "POST"])
@admin_required
def templates_list(request):
    if request.method == "GET":
        qs = NotificationTemplate.objects.all()
        # Lightweight filters so admins can scope the list when building a
        # dashboard.
        ntype = request.GET.get('notification_type')
        if ntype:
            qs = qs.filter(notification_type=ntype)
        language = request.GET.get('language')
        if language:
            qs = qs.filter(language=language)
        if 'is_enabled' in request.GET:
            qs = qs.filter(is_enabled=request.GET['is_enabled'].lower() in ('true', '1', 'yes'))
        return JsonResponse(
            {"success": True, "data": [_serialize_template(t) for t in qs]},
            status=200,
        )

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    notification_type = (data.get('notification_type') or '').strip()
    name = (data.get('name') or '').strip()
    template_text = data.get('template_text', '')

    missing = []
    if not notification_type:
        missing.append('notification_type')
    if not name:
        missing.append('name')
    if not template_text:
        missing.append('template_text')
    if missing:
        return JsonResponse(
            {"success": False, "message": "Missing required fields",
             "errors": {f: f"{f} is required" for f in missing}},
            status=422,
        )

    err = _validate_template_text(template_text)
    if err:
        return JsonResponse(
            {"success": False, "message": err, "errors": {"template_text": err}},
            status=422,
        )

    if NotificationTemplate.objects.filter(notification_type=notification_type).exists():
        return JsonResponse(
            {"success": False, "message": "Template for this type already exists",
             "errors": {"notification_type": "must be unique"}},
            status=409,
        )

    template = NotificationTemplate.objects.create(
        notification_type=notification_type,
        name=name,
        template_text=template_text,
        description=data.get('description', ''),
        is_enabled=data.get('is_enabled', True),
        language=data.get('language', 'uz'),
    )
    return JsonResponse(
        {"success": True, "message": "Created", "data": _serialize_template(template)},
        status=201,
    )


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE"])
@admin_required
def template_detail(request, template_id):
    try:
        template = NotificationTemplate.objects.get(id=template_id)
    except NotificationTemplate.DoesNotExist:
        return JsonResponse(
            {"success": False, "message": "Template not found"}, status=404
        )

    if request.method == "GET":
        return JsonResponse(
            {"success": True, "data": _serialize_template(template)},
            status=200,
        )

    if request.method == "DELETE":
        template.delete()
        return JsonResponse(
            {"success": True, "message": "Deleted"},
            status=200,
        )

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    if "template_text" in data:
        err = _validate_template_text(data["template_text"])
        if err:
            return JsonResponse(
                {"success": False, "message": err, "errors": {"template_text": err}},
                status=422,
            )
        template.template_text = data["template_text"]
    if "name" in data:
        template.name = data["name"]
    if "description" in data:
        template.description = data["description"]
    if "is_enabled" in data:
        template.is_enabled = data["is_enabled"]
    if "language" in data:
        template.language = data["language"]
    template.save()

    return JsonResponse(
        {"success": True, "message": "Updated", "data": _serialize_template(template)},
        status=200,
    )


@csrf_exempt
@require_POST
@admin_required
def template_preview(request, template_id):
    """Render the template against an admin-supplied sample context without
    sending a notification. Lets editors verify changes before they go live.
    """
    try:
        template = NotificationTemplate.objects.get(id=template_id)
    except NotificationTemplate.DoesNotExist:
        return JsonResponse(
            {"success": False, "message": "Template not found"}, status=404
        )

    data, error = parse_json_body(request)
    if error:
        return json_response(error)

    context = data.get('context') or {}
    if not isinstance(context, dict):
        return JsonResponse(
            {"success": False, "message": "context must be a JSON object"},
            status=422,
        )

    # Pull in the brand the same way SenderService does so previews match
    # what an end recipient would see.
    from notifications.models import NotificationSettings
    settings = NotificationSettings.load()
    context.setdefault('brand', settings.brand_name)

    # Use the same escaping + sandboxed-format pipeline as the real send
    # path so preview output matches what an end recipient would see — and
    # so a malicious template's `{x.__class__}` is caught here too.
    from notifications.services.sender_service import _escape_context
    from notifications.services.safe_format import safe_format, _UnsafePlaceholder
    try:
        rendered = safe_format(template.template_text, **_escape_context(context))
    except KeyError as exc:
        missing_key = str(exc).strip("'")
        return JsonResponse(
            {"success": False,
             "message": f"Missing context key: {missing_key}",
             "errors": {"context": f"template references {{{missing_key}}} but context did not provide it"}},
            status=422,
        )
    except _UnsafePlaceholder as exc:
        return JsonResponse(
            {"success": False,
             "message": str(exc),
             "errors": {"template_text": str(exc)}},
            status=422,
        )
    except (IndexError, ValueError) as exc:
        return JsonResponse(
            {"success": False, "message": f"Render error: {exc}"},
            status=422,
        )

    return JsonResponse(
        {"success": True, "data": {"rendered": rendered, "context": context}},
        status=200,
    )


@require_GET
@admin_required
def queue_view(request):
    items = QueueService.get_all()
    return JsonResponse({'success': True, 'data': {'queue': items, 'count': len(items)}})


@csrf_exempt
@require_POST
@admin_required
def queue_process(request):
    sent, failed = QueueService.process()
    return JsonResponse({'success': True, 'data': {'sent': sent, 'failed': failed}})


@csrf_exempt
@require_POST
@admin_required
def queue_clear(request):
    QueueService.clear()
    return JsonResponse({'success': True, 'message': 'Queue cleared'})


@require_GET
@admin_required
def logs_view(request):
    page = safe_page(request)
    per_page = safe_per_page(request, 25)
    notification_type = request.GET.get("notification_type")

    qs = NotificationLog.objects.all()
    if notification_type:
        qs = qs.filter(notification_type=notification_type)

    total = qs.count()
    start = (page - 1) * per_page
    end = start + per_page
    logs = qs[start:end]

    data = [
        {
            "id": log.id,
            "notification_type": log.notification_type,
            "recipient": log.recipient,
            "message_text": log.message_text,
            "status": log.status,
            "error_message": log.error_message,
            "created_at": log.created_at.isoformat(),
        }
        for log in logs
    ]

    return JsonResponse(
        {
            "success": True,
            "data": data,
            "pagination": {
                "page": page,
                "per_page": per_page,
                "total": total,
                "pages": (total + per_page - 1) // per_page,
            },
        },
        status=200,
    )
