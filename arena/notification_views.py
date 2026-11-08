from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .notification_models import Notification
from .notifications import queue_readiness_reminders


@login_required
@never_cache
def inbox(request):
    # Serverless deployments have no persistent worker. A captain opening the
    # inbox can receive a due reminder without sending external messages.
    queue_readiness_reminders(user_id=request.user.pk)
    items = Notification.objects.filter(user=request.user)
    page = Paginator(items, 30).get_page(request.GET.get("page"))
    return render(request, "arena/notifications.html", {"notification_page": page, "unread_count": items.filter(read_at__isnull=True).count()})


@login_required
@require_POST
def mark_read(request, notification_id):
    item = get_object_or_404(Notification, pk=notification_id, user=request.user)
    Notification.objects.filter(pk=item.pk, user=request.user, read_at__isnull=True).update(read_at=timezone.now())
    return redirect("notifications")
