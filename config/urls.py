from django.contrib import admin
from django.urls import include, path
from arena import views

urlpatterns = [path('admin/', admin.site.urls), path('', include('arena.urls')), path('', include('arena.game_urls'))]
handler403 = views.forbidden
handler404 = views.not_found
handler500 = views.server_error
