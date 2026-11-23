web: gunicorn config.wsgi --bind 0.0.0.0:${PORT:-8080} --workers 2 --timeout 60
worker: python manage.py poll_mailbox --loop
