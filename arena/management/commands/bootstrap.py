import secrets
from pathlib import Path
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from arena.models import Tournament


class Command(BaseCommand):
    help = 'Create the draft tournament and local organizer account without demo players.'

    def handle(self, *args, **options):
        tournament, created = Tournament.objects.get_or_create(pk=1, defaults={'description': 'Командный турнир сообщества CQC по Mobile Legends: Bang Bang. Капитан регистрирует состав, каждый участник подтверждает участие и оплачивает свой взнос. Допуск команды — после проверки организатором.'})
        user_model = get_user_model()
        if not user_model.objects.filter(is_superuser=True).exists():
            username = 'organizer'
            if user_model.objects.filter(username=username).exists():
                username = 'organizer-' + secrets.token_hex(3)
            password = secrets.token_urlsafe(24)
            user_model.objects.create_superuser(username=username, password=password, first_name='Организатор')
            target = Path(settings.BASE_DIR) / '.local' / 'admin-access.txt'
            target.write_text('Локальный кабинет организатора: '+settings.PUBLIC_BASE_URL+'/organizer/\nЛогин: '+username+'\nПароль: '+password+'\n\nНе публикуйте этот файл. Измените пароль перед публикацией через python manage.py changepassword '+username+'\n', encoding='utf-8')
            self.stdout.write('Organizer account created. Credentials: .local/admin-access.txt')
        self.stdout.write('Draft tournament ready. No demo teams or payments created.')
