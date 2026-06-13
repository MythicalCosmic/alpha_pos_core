from django.http import JsonResponse


def json_response(result_tuple):
    data, status = result_tuple
    return JsonResponse(data, status=status)


class ServiceResponse:
    @staticmethod
    def success(data=None, message="Success"):
        body = {"success": True, "message": message}
        if data is not None:
            body["data"] = data
        return body, 200

    @staticmethod
    def created(data=None, message="Created"):
        body = {"success": True, "message": message}
        if data is not None:
            body["data"] = data
        return body, 201

    @staticmethod
    def error(message="Bad request", errors=None):
        body = {"success": False, "message": message}
        if errors:
            body["errors"] = errors
        return body, 400

    @staticmethod
    def unauthorized(message="Unauthorized"):
        return {"success": False, "message": message}, 401

    @staticmethod
    def forbidden(message="Forbidden"):
        return {"success": False, "message": message}, 403

    @staticmethod
    def not_found(message="Not found"):
        return {"success": False, "message": message}, 404

    @staticmethod
    def validation_error(errors, message="Validation failed"):
        return {"success": False, "message": message, "errors": errors}, 422

