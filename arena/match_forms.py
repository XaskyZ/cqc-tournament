"""Forms for the match workspace; authorization lives in views and services."""
from urllib.parse import urlsplit

from django import forms


def date_field(label):
    return forms.DateTimeField(label=label, required=False,
        widget=forms.DateTimeInput(format='%Y-%m-%dT%H:%M', attrs={'type': 'datetime-local'}),
        input_formats=['%Y-%m-%dT%H:%M'])


class MatchSettingsForm(forms.Form):
    scheduled_at = date_field('Начало матча')
    ready_deadline = date_field('Подтвердить готовность до')
    lobby_details = forms.CharField(label='Данные лобби и инструкция для команд', required=False,
        max_length=2000, widget=forms.Textarea(attrs={'rows': 3}),
        help_text='Видны только организатору, капитанам и игрокам этого матча.')


class ResultForm(forms.Form):
    score_a = forms.IntegerField(label='Победы команды A', min_value=0, max_value=99)
    score_b = forms.IntegerField(label='Победы команды B', min_value=0, max_value=99)

    def __init__(self, *args, match, **kwargs):
        super().__init__(*args, **kwargs)
        for side in ('a', 'b'):
            team = getattr(match, f'team_{side}')
            self.fields[f'score_{side}'].label = f'Победы: {team.name if team else side.upper()}'


class CorrectionForm(ResultForm):
    reason = forms.CharField(label='Причина исправления', max_length=1000,
        widget=forms.Textarea(attrs={'rows': 2}))


class ForfeitForm(forms.Form):
    winner_team_id = forms.TypedChoiceField(label='Победа присуждается', coerce=int)
    reason = forms.CharField(label='Причина технического поражения', max_length=1000,
        widget=forms.Textarea(attrs={'rows': 2}), help_text='Причина будет опубликована в карточке матча. Не указывайте личные данные.')

    def __init__(self, *args, match, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['winner_team_id'].choices = [(team.pk, team.name) for team in (match.team_a, match.team_b) if team]


class ReportForm(forms.Form):
    kind = forms.ChoiceField(label='Тип обращения', choices=[('result', 'Сообщить результат'), ('dispute', 'Обратиться к судье')])
    reported_score_a = forms.IntegerField(label='Победы команды A', required=False, min_value=0, max_value=99)
    reported_score_b = forms.IntegerField(label='Победы команды B', required=False, min_value=0, max_value=99)
    message = forms.CharField(label='Комментарий', max_length=2000, widget=forms.Textarea(attrs={'rows': 3}))
    evidence_url = forms.URLField(label='Ссылка на скриншот или видео', required=False, max_length=500,
        help_text='HTTPS-ссылка, доступная судье. Не публикуйте личные данные и платёжные чеки.')

    def __init__(self, *args, match, **kwargs):
        super().__init__(*args, **kwargs)
        for side in ('a', 'b'):
            team = getattr(match, f'team_{side}')
            self.fields[f'reported_score_{side}'].label = f'Победы: {team.name if team else side.upper()}'

    def clean_evidence_url(self):
        value = self.cleaned_data['evidence_url']
        if value:
            url = urlsplit(value)
            if url.scheme != 'https' or not url.hostname or url.username or url.password:
                raise forms.ValidationError('Укажите HTTPS-ссылку без логина и пароля.')
        return value

    def clean(self):
        data = super().clean()
        a, b = data.get('reported_score_a'), data.get('reported_score_b')
        if (a is None) != (b is None) or (data.get('kind') == 'result' and (a is None or b is None)):
            raise forms.ValidationError('Для результата заполните счёт обеих команд. В обращении к судье можно оставить оба поля пустыми.')
        if data.get('kind') == 'dispute' and (a is not None or b is not None):
            raise forms.ValidationError('Для обращения к судье оставьте поля счёта пустыми; опишите ситуацию в комментарии.')
        return data


class ReportDecisionForm(forms.Form):
    status = forms.ChoiceField(label='Решение', choices=[('resolved', 'Разобрано'), ('rejected', 'Отклонено')])
    reason = forms.CharField(label='Обоснование решения', max_length=1000, widget=forms.Textarea(attrs={'rows': 2}))
