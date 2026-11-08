from django import forms
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from .models import Tournament


class PasswordLoginForm(AuthenticationForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].widget.attrs.pop('autofocus', None)


class SignupForm(UserCreationForm):
    first_name = forms.CharField(label='Имя капитана', max_length=150)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['username'].widget.attrs.pop('autofocus', None)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'first_name', 'password1', 'password2')


class TeamRegistrationForm(forms.Form):
    name = forms.CharField(label='Название команды', max_length=100)
    telegram_contact = forms.CharField(label='Telegram капитана', max_length=70, help_text='Имя пользователя @username или ссылка t.me/username.')
    discord_contact = forms.CharField(label='Discord капитана', max_length=80, help_text='Имя пользователя в Discord, не отображаемое имя.')
    accept_rules = forms.BooleanField(label='Подтверждаю создание команды и согласен на обработку данных для проведения турнира.')


class JoinTeamForm(forms.Form):
    role = forms.ChoiceField(label='Место в составе', choices=[('main', 'Основной игрок'), ('reserve', 'Запасной')])
    accept_rules = forms.BooleanField(label='Подтверждаю вступление в команду с моим игровым аккаунтом.')


class ManualPlayerForm(forms.Form):
    game_id = forms.RegexField(label='Game ID', regex=r'^[0-9]{1,40}$', max_length=40, widget=forms.TextInput(attrs={'inputmode': 'numeric'}))
    server_id = forms.RegexField(label='Server ID', regex=r'^[0-9]{1,40}$', max_length=40, widget=forms.TextInput(attrs={'inputmode': 'numeric'}))
    role = forms.ChoiceField(label='Место в составе', choices=[('main', 'Основной игрок'), ('reserve', 'Запасной')])


class PlayerForm(forms.Form):
    nickname = forms.CharField(label='Игровой никнейм', max_length=100)
    game_id = forms.RegexField(label='Game ID', regex=r'^[0-9]{1,40}$', max_length=40, widget=forms.TextInput(attrs={'inputmode': 'numeric'}), error_messages={'invalid': 'Укажите числовой игровой ID.'})
    server_id = forms.RegexField(label='Server ID', regex=r'^[0-9]{1,40}$', max_length=40, widget=forms.TextInput(attrs={'inputmode': 'numeric'}), error_messages={'invalid': 'Укажите числовой ID сервера.'})
    is_reserve = forms.BooleanField(required=False, widget=forms.HiddenInput)

    def clean(self):
        data = super().clean()
        for field in ('game_id', 'server_id'):
            if field in data:
                if int(data[field]) <= 0:
                    self.add_error(field, 'ID должен быть положительным числом.')
                else:
                    data[field] = str(int(data[field]))
        return data


def player_formset(tournament, data=None):
    size = tournament.roster_size + (tournament.reserve_limit or 0)
    factory = forms.formset_factory(PlayerForm, extra=size, max_num=size, absolute_max=size, validate_max=True)
    result = factory(data=data, prefix='players')
    for index, form in enumerate(result.forms):
        form.initial['is_reserve'] = index >= tournament.roster_size
        form.empty_permitted = index >= tournament.roster_size
    return result


class PaymentReviewForm(forms.Form):
    decision = forms.ChoiceField(label='Решение', choices=[('approve', 'Подтвердить оплату'), ('reject', 'Отклонить')])
    bank_reference = forms.CharField(label='Уникальный номер операции в Kaspi Pay', max_length=160, required=False, help_text='Для подтверждения: номер фактической операции. Один платёж нельзя зачесть дважды.')
    reason = forms.CharField(label='Причина отклонения / комментарий', max_length=1000, required=False, widget=forms.Textarea(attrs={'rows': 3}))
    verified = forms.BooleanField(label='Я сверил поступление, сумму и участника в Kaspi Pay.', required=False)
    submission = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if settings.DEMO_MODE:
            self.fields['bank_reference'].label = 'Тестовый номер операции'
            self.fields['bank_reference'].help_text = 'Например TEST-001. Банковский перевод не нужен; номер должен быть уникальным.'
            self.fields['verified'].label = 'Подтверждаю тестовую проверку взноса.'

    def clean(self):
        data = super().clean()
        if data.get('decision') == 'approve':
            if not data.get('verified'):
                self.add_error('verified', 'Подтвердите тестовую проверку.' if settings.DEMO_MODE else 'Для подтверждения необходимо сверить платёж в Kaspi Pay.')
            if not data.get('bank_reference'):
                self.add_error('bank_reference', 'Укажите номер операции.')
        elif not data.get('reason'):
            self.add_error('reason', 'Объясните причину отклонения.')
        return data


class MatchEditForm(forms.Form):
    scheduled_at = forms.DateTimeField(label='Время матча (часовой пояс турнира)', required=False, widget=forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}), input_formats=['%Y-%m-%dT%H:%M'])
    score_a = forms.IntegerField(label='Победы команды A', required=False, min_value=0, max_value=99)
    score_b = forms.IntegerField(label='Победы команды B', required=False, min_value=0, max_value=99)

    def clean(self):
        data = super().clean()
        if (data.get('score_a') is None) != (data.get('score_b') is None):
            raise ValidationError('Для результата заполните оба значения счёта. Для расписания оставьте оба пустыми.')
        return data


class TournamentAdminForm(forms.ModelForm):
    class Meta:
        model = Tournament
        fields = '__all__'
        labels = {
            'title': 'Название турнира', 'game': 'Игра', 'description': 'Описание', 'rules': 'Правила и условия участия',
            'prize_pool': 'Призовой фонд, ₸', 'prize_note': 'Распределение и условия призов', 'event_date': 'Дата турнира',
            'starts_at': 'Начало турнира', 'end_registration': 'Окончание регистрации', 'timezone_name': 'Часовой пояс IANA',
            'max_teams': 'Количество команд', 'roster_size': 'Основных игроков', 'reserve_limit': 'Максимум запасных',
            'reserves_pay': 'Запасные платят по 2 200 ₸', 'best_of': 'Матчи BO', 'final_best_of': 'Гранд-финал BO',
            'grand_final_reset': 'Повторный гранд-финал при победе нижней сетки', 'kaspi_url': 'Ссылка Kaspi Pay',
            'registration_open': 'Открыть регистрацию', 'terms_confirmed': 'Условия подтверждены организатором',
        }
        help_texts = {'timezone_name': 'Например Asia/Qyzylorda. Сначала сохраните часовой пояс, затем задайте время в нём. Пока пояс не выбран, время вводится в UTC.', 'terms_confirmed': 'Подтвердите только после согласования правил, сроков, состава и призов.', 'prize_note': 'Укажите распределение призового фонда; предварительная сумма взята из плана совещания.'}
