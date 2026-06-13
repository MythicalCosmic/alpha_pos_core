import json


def validate_request(request, required_fields):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return None, ({"success": False, "message": "Invalid JSON"}, 400)

    if not isinstance(data, dict):
        return None, ({"success": False, "message": "Expected JSON object"}, 400)

    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        return None, (
            {
                "success": False,
                "message": f"Missing required fields: {', '.join(missing)}",
                "errors": {f: f"{f} is required" for f in missing},
            },
            422,
        )

    return data, None
