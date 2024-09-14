import os
import signal
import subprocess
import time
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = 'Control the Django application (start, stop, restart)'

    def add_arguments(self, parser):
        parser.add_argument('action', type=str, help='Action to perform: start, stop, or restart')

    def handle(self, *args, **options):
        action = options['action']
        
        if action == 'start':
            self.start_app()
        elif action == 'stop':
            self.stop_app()
        elif action == 'restart':
            self.restart_app()
        else:
            self.stdout.write(self.style.ERROR(f'Invalid action: {action}'))

    def start_app(self):
        pid = self.get_running_pid()
        if pid:
            self.stdout.write(self.style.WARNING('Application is already running'))
            return

        command = "python manage.py runserver"
        process = subprocess.Popen(command, shell=True)
        
        with open('app.pid', 'w') as f:
            f.write(str(process.pid))
        
        self.stdout.write(self.style.SUCCESS('Application started'))

    def stop_app(self):
        pid = self.get_running_pid()
        if not pid:
            self.stdout.write(self.style.WARNING('Application is not running'))
            return

        os.kill(pid, signal.SIGTERM)
        os.remove('app.pid')
        self.stdout.write(self.style.SUCCESS('Application stopped'))

    def restart_app(self):
        self.stop_app()
        time.sleep(2)  # Wait for the app to fully stop
        self.start_app()

    def get_running_pid(self):
        if os.path.exists('app.pid'):
            with open('app.pid', 'r') as f:
                return int(f.read().strip())
        return None