web: daphne -b 0.0.0.0 -p $PORT dineos.asgi:application
worker: celery -A dineos worker --loglevel=info --concurrency 2
