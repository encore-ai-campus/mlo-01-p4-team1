from django.urls import path
from . import api, passkeys

urlpatterns = [
    path('api/accounts/policy', api.Policy.as_view()),
    path('api/accounts/availability', api.Availability.as_view()),
    path('api/accounts/signup', api.Signup.as_view()),
    path('api/accounts/recovery', api.Recovery.as_view()),
    path('api/accounts/recovery/complete', api.RecoveryComplete.as_view()),
    path('api/me/profile', api.Profile.as_view()),
    path('api/me/email', api.Email.as_view()),
    path('api/me/email/verify', api.EmailVerify.as_view()),
    path('api/me/password', api.Password.as_view()),
    path('api/me/withdraw', api.Withdraw.as_view()),
    path('api/me/playlists', api.Playlists.as_view()),
    path('api/me/playlists/<int:pk>', api.PlaylistDetail.as_view()),
    path('api/admin/passkeys/options', passkeys.Options.as_view()),
    path('api/admin/passkeys/verify', passkeys.Verify.as_view()),
]
