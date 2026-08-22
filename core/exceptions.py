from rest_framework import status
from rest_framework.exceptions import APIException


class InsufficientPortionsError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Not enough portions remaining for this item."
    default_code = "insufficient_portions"


class SessionNotOpenError(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This table session is not open."
    default_code = "session_not_open"


class InvalidStatusTransitionError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "That status transition is not allowed."
    default_code = "invalid_status_transition"


class MenuItemNotFoundError(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "One or more menu items don't exist, aren't available, or don't belong to this restaurant."
    default_code = "menu_item_not_found"
