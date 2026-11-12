from django.urls import path

from . import api

urlpatterns = [
    path('session', api.session),
    path('song-requests/quota', api.song_quota),
    path('song-requests/check', api.song_check),
    path('song-requests', api.song_requests),
    path('song-requests/<uuid:pk>', api.song_request_detail),
    path('admin/song-items', api.admin_songs),
    path('admin/song-items/<uuid:pk>/review', api.admin_song_review),
    path('admin/tasks', api.admin_tasks),
    path('admin/tasks/<uuid:pk>/finalize', api.admin_task_finalize),
    path('admin/tasks/<uuid:pk>/publish', api.admin_task_publish),
    path('admin/tasks/<uuid:pk>/recover', api.admin_task_recover),
    path('worker/tasks', api.worker_tasks),
    path('worker/tasks/<uuid:pk>/claim', api.worker_claim),
    path('worker/results', api.worker_receive),
]
