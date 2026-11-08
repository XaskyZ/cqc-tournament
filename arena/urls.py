from django.contrib.auth import views as auth_views
from django.urls import path
from . import views
from . import match_views, notification_views
from . import rules
from . import telegram_auth
from .forms import PasswordLoginForm
from . import team_views
from . import account_disputes, roster_recovery
from .telegram_webhook import telegram_webhook

urlpatterns = [
    path('notifications/', notification_views.inbox, name='notifications'),
    path('notifications/<int:notification_id>/read/', notification_views.mark_read, name='notification_read'),
    path('matches/<int:match_id>/', match_views.detail, name='match_detail'),
    path('matches/<int:match_id>/action/', match_views.action, name='match_action'),
    path('organizer/match-reports/', match_views.report_queue, name='match_report_queue'),
    path('organizer/match-report/<int:report_id>/', match_views.report_review, name='match_report_review'),
    path('rules/', rules.rules_page, name='rules'),
    path('telegram/webhook/', telegram_webhook, name='telegram_webhook'),
    path('account/game/disputes/', account_disputes.create_dispute, name='account_disputes'),
    path('organizer/account-disputes/', account_disputes.dispute_queue, name='account_dispute_queue'),
    path('organizer/account-disputes/<int:dispute_id>/', account_disputes.review_dispute_view, name='account_dispute_review'),
    path('organizer/participant/<int:participant_id>/withdraw/', roster_recovery.withdraw_player_view, name='withdraw_player'),
    path('team/<int:team_id>/revise/', team_views.team_return_to_draft, name='team_return_to_draft'),
    path('team/<int:team_id>/details/', team_views.team_update_details, name='team_update_details'),
    path('team/<int:team_id>/role/<int:participant_id>/', team_views.team_change_role, name='team_change_role'),
    path('team/<int:team_id>/rotate-invite/', team_views.team_rotate_invite, name='team_rotate_invite'),
    path('organizer/team/<int:team_id>/reopen/', views.team_reopen, name='team_reopen'),
    path('team/join/<uuid:invite_token>/', team_views.team_join, name='team_join'),
    path('team/<int:team_id>/add-player/', team_views.team_add_player, name='team_add_player'),
    path('team/<int:team_id>/submit/', team_views.team_submit, name='team_submit'),
    path('team/<int:team_id>/remove/<int:participant_id>/', team_views.team_remove_player, name='team_remove_player'),
    path('auth/telegram/', telegram_auth.auth_page, name='telegram_auth'),
    path('auth/telegram/begin/', telegram_auth.auth_begin, name='telegram_auth_begin'),
    path('auth/telegram/status/', telegram_auth.auth_status, name='telegram_auth_status'),
    path('auth/telegram/finish/', telegram_auth.auth_finish, name='telegram_auth_finish'),
    path('auth/telegram/cancel/', telegram_auth.auth_cancel, name='telegram_auth_cancel'),
    path('', views.home, name='home'), path('teams/', views.teams, name='teams'), path('schedule/', views.schedule, name='schedule'), path('bracket/', views.bracket, name='bracket'),
    path('signup/', views.signup, name='signup'), path('login/', auth_views.LoginView.as_view(authentication_form=PasswordLoginForm), name='login'), path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('register/', views.register, name='register'), path('captain/', views.captain, name='captain'),
    path('captain/state/', views.captain_state, name='captain_state'), path('p/<uuid:token>/state/', views.participant_state, name='participant_state'),
    path('p/<uuid:token>/', views.participant, name='participant'), path('p/<uuid:token>/confirm/', views.participant_confirm, name='participant_confirm'), path('p/<uuid:token>/paid/', views.payment_submit, name='payment_submit'),
    path('organizer/', views.organizer, name='organizer'), path('organizer/payment/<int:payment_id>/', views.review_payment, name='review_payment'),
    path('organizer/team/<int:team_id>/roster/', views.roster_check, name='roster_check'), path('organizer/team/<int:team_id>/confirm/', views.team_confirm, name='team_confirm'), path('organizer/team/<int:team_id>/reject/', views.team_reject, name='team_reject'),
    path('organizer/bracket/', views.generate_bracket, name='generate_bracket'), path('organizer/match/<int:match_id>/', views.match_edit, name='match_edit'),
]
