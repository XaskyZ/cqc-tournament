from .models import GameAccount, TelegramIdentity, Tournament
from django.conf import settings
from .services import registration_errors

def tournament_context(request):
    from .telegram_auth import auth_available
    tournament = Tournament.objects.order_by('pk').first()
    telegram_account = TelegramIdentity.objects.filter(user=request.user).first() if request.user.is_authenticated else None
    game_account = GameAccount.objects.filter(user=request.user).first() if request.user.is_authenticated else None
    from .rules import has_accepted
    rules_accepted = bool(tournament and has_accepted(tournament, request.user))
    return {'tournament': tournament, 'rules_accepted': rules_accepted, 'demo_mode': settings.DEMO_MODE, 'registration_ready': bool(tournament and not registration_errors(tournament)), 'telegram_auth_available': auth_available(), 'telegram_account': telegram_account, 'telegram_linked': bool(telegram_account), 'game_account': game_account}
