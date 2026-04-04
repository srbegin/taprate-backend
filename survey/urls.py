from django.urls import path
from .views import (
    RegisterView, LoginView, MeView, TokenRefreshView,
    LocationListView, LocationDetailView,
    SurveyListView, SurveyDetailView,
    IncentiveView,
    AlertListView, AlertDetailView,
    InsightsView,
    PublicSurveyDetailView, SurveyResponseView,
    NfcTagView,
)

urlpatterns = [
    # Auth
    path('auth/register/', RegisterView.as_view()),
    path('auth/login/', LoginView.as_view()),
    path('auth/token/refresh/', TokenRefreshView.as_view()),
    path('auth/me/', MeView.as_view()),

    # Dashboard — locations
    path('dashboard/locations/', LocationListView.as_view()),
    path('dashboard/locations/<uuid:pk>/', LocationDetailView.as_view()),

    # Dashboard — surveys
    path('dashboard/surveys/', SurveyListView.as_view()),
    path('dashboard/surveys/<uuid:pk>/', SurveyDetailView.as_view()),
    path('dashboard/surveys/<uuid:survey_pk>/incentive/', IncentiveView.as_view()),

    # Dashboard — alerts
    path('dashboard/alerts/', AlertListView.as_view()),
    path('dashboard/alerts/<uuid:pk>/', AlertDetailView.as_view()),

    # Dashboard — insights
    path('dashboard/insights/', InsightsView.as_view()),

    # Public survey PWA
    path('survey/<uuid:location_uuid>/', PublicSurveyDetailView.as_view()),
    path('survey/<uuid:location_uuid>/response/', SurveyResponseView.as_view()),

    # NFC tag claim
    path('tags/<uuid:tag_id>/', NfcTagView.as_view()),
]