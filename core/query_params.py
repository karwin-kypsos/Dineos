from datetime import date

from rest_framework.exceptions import ValidationError


def parse_query_date(value, field):
    """A YYYY-MM-DD query parameter as a date, or a 400 naming the field.

    2026-10-05: several list endpoints parsed ?date= with strptime or
    parse_date and nothing caught the error, so ?date=2026-13-45 (or any
    typo) answered 500 instead of telling the caller what was wrong.
    parse_date in particular returns None for a bad shape but RAISES for a
    good shape with impossible values, so neither path was safe on its own.
    """
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError({field: ["Expected format YYYY-MM-DD."]})
