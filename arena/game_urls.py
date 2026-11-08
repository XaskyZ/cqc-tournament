from django.urls import path
from . import game_accounts

urlpatterns = [
    path('account/game/', game_accounts.game_account, name='game_account'),
    path('p/<uuid:token>/claim/', game_accounts.participant_claim, name='participant_claim'),
]
