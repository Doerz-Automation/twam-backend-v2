from django.apps import AppConfig
import os


class ApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "api"

    def ready(self):
        import sys

        # Allow explicit opt-out via env var (useful for multi-worker ECS deployments)
        # Set RUN_SCHEDULER=false on containers that should NOT run the scheduler.
        if os.environ.get('RUN_SCHEDULER', '').lower() == 'false':
            return

        # Skip for management commands (migrate, shell, etc.)
        is_management_cmd = len(sys.argv) > 1 and sys.argv[1] in (
            'migrate', 'makemigrations', 'collectstatic', 'shell',
            'createsuperuser', 'test', 'check', 'dbshell',
        )

        # Django runserver uses a reloader — only start in the live child process
        # (RUN_MAIN env var is set by Django's reloader in the actual server process)
        is_reloader_parent = 'runserver' in sys.argv and not os.environ.get('RUN_MAIN')

        if not is_management_cmd and not is_reloader_parent:
            from . import scheduler
            scheduler.start_scheduler()
