from rest_framework.pagination import PageNumberPagination


class DineOSPageNumberPagination(PageNumberPagination):
    """Default page size (20) unchanged; a caller can ask for a bigger page
    via ?page_size=, capped so a single request can't demand the entire
    table. Standard DRF PageNumberPagination doesn't expose the query
    param unless a pagination class opts into it."""

    page_size_query_param = "page_size"
    max_page_size = 100
