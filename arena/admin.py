from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from .forms import TournamentAdminForm
from .models import GameAccount, Match, Outbox, Participant, Payment, PaymentAudit, Team, TelegramIdentity, Tournament
from .rule_models import RulesAcceptance

admin.site.site_header = 'CQC · Настройки турнира'
admin.site.site_title = 'Организатор CQC'
admin.site.index_title = 'Управление турниром'


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(RulesAcceptance)
class RulesAcceptanceAdmin(ReadOnlyAdmin):
    list_display = ('user', 'tournament', 'version', 'accepted_at')
    search_fields = ('user__username', 'version')


@admin.register(Tournament)
class TournamentAdmin(admin.ModelAdmin):
    form = TournamentAdminForm
    list_display = ('title', 'event_date', 'registration_open', 'terms_confirmed')
    readonly_fields = ('created_at',)
    fieldsets = (
        ('Общие сведения', {'fields': ('title', 'game', 'description', 'rules')}),
        ('Сроки и часовой пояс', {'fields': ('timezone_name', 'event_date', 'starts_at', 'end_registration')}),
        ('Состав и взносы', {'fields': ('max_teams', 'roster_size', 'reserve_limit', 'reserves_pay', 'kaspi_url'), 'description': 'Взнос фиксирован: 2 200 ₸ с каждого оплачивающего участника. Сначала согласуйте условия запасных.'}),
        ('Сетка и призы', {'fields': ('best_of', 'final_best_of', 'grand_final_reset', 'prize_pool', 'prize_note'), 'description': 'Двойная олимпийская система. BO3 означает до двух побед. Пары первого раунда идут по порядку регистрации допущенных команд.'}),
        ('Запуск регистрации', {'fields': ('terms_confirmed', 'registration_open', 'created_at')}),
    )

    def has_add_permission(self, request):
        return not Tournament.objects.exists() and super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Team)
class TeamAdmin(ReadOnlyAdmin):
    list_display = ('name', 'captain', 'status', 'roster_checked_at')
    list_filter = ('status',)
    search_fields = ('name', 'captain__username')


@admin.register(Participant)
class ParticipantAdmin(ReadOnlyAdmin):
    list_display = ('nickname', 'team', 'account', 'is_reserve', 'registration_confirmed')
    list_filter = ('registration_confirmed', 'is_reserve')
    search_fields = ('nickname', 'team__name', 'game_id')


@admin.register(Payment)
class PaymentAdmin(ReadOnlyAdmin):
    list_display = ('participant', 'amount', 'status', 'reviewed_at', 'reviewed_by', 'reviewed_by_telegram', 'review_link')
    list_filter = ('status', 'required')
    search_fields = ('participant__nickname', 'participant__team__name', 'bank_reference')

    @admin.display(description='Проверка')
    def review_link(self, obj):
        return format_html('<a href="{}">Проверить взнос</a>', reverse('review_payment', args=[obj.pk])) if obj.status == 'review' else '—'


@admin.register(PaymentAudit)
class AuditAdmin(ReadOnlyAdmin):
    list_display = ('payment', 'from_status', 'to_status', 'actor_user', 'actor_telegram', 'created_at')
    list_filter = ('to_status',)


@admin.register(Match)
class MatchAdmin(ReadOnlyAdmin):
    list_display = ('key', 'bracket', 'team_a', 'team_b', 'scheduled_at', 'winner', 'edit_link')
    list_filter = ('bracket',)

    @admin.display(description='Управление')
    def edit_link(self, obj):
        return format_html('<a href="{}">Расписание и результат</a>', reverse('match_edit', args=[obj.pk]))


@admin.register(Outbox)
class OutboxAdmin(ReadOnlyAdmin):
    list_display = ('payment', 'kind', 'created_at', 'sent_at', 'attempts', 'last_error')


@admin.register(TelegramIdentity)
class TelegramIdentityAdmin(ReadOnlyAdmin):
    list_display = ('user', 'telegram_id', 'username', 'linked_at')
    search_fields = ('user__username', 'username', 'telegram_id')


@admin.register(GameAccount)
class GameAccountAdmin(ReadOnlyAdmin):
    list_display = ('user', 'nickname', 'game_id', 'server_id', 'lookup_provider', 'lookup_checked_at')
    search_fields = ('nickname', 'game_id', 'server_id')
