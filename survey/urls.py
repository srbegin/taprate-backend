from django.urls import path
from .views import (
    RegisterView, LoginView, MeView, TokenRefreshView, ChangePasswordView,
    LocationListView, LocationDetailView,
    SurveySetListView, SurveySetDetailView,
    QuestionListView, QuestionDetailView,
    # IncentiveView,
    AlertListView, AlertDetailView,
    InsightsView, CommentFeedView, OrganizationView,
    QRCodeView,
    PublicSurveyDetailView, SurveyResponseView,
    NfcTagView,
    AdminOverviewView,
    AdminOrganizationListView,
    AdminTagListView, AdminTagDetailView, AdminRecentSignupsView,
    CheckoutView, PortalView, WebhookView,
    IncentiveListCreateView, IncentiveDetailView, IncentiveAssignView, 
    RedeemValidateView, RedeemUseView, 
)

urlpatterns = [
    # Auth
    path('auth/register/', RegisterView.as_view()),
    path('auth/login/', LoginView.as_view()),
    path('auth/token/refresh/', TokenRefreshView.as_view()),
    path('auth/me/', MeView.as_view()),
    path('auth/change-password/', ChangePasswordView.as_view()),

    # Dashboard — locations
    path('dashboard/locations/', LocationListView.as_view()),
    path('dashboard/locations/<uuid:pk>/', LocationDetailView.as_view()),
    path('dashboard/locations/<uuid:pk>/qr/', QRCodeView.as_view()),

    # Dashboard — survey sets
    path('dashboard/survey-sets/', SurveySetListView.as_view()),
    path('dashboard/survey-sets/<uuid:pk>/', SurveySetDetailView.as_view()),

    # Dashboard — questions (nested under survey set)
    path('dashboard/survey-sets/<uuid:set_pk>/questions/', QuestionListView.as_view()),
    path('dashboard/survey-sets/<uuid:set_pk>/questions/<uuid:pk>/', QuestionDetailView.as_view()),

    # Dashboard — incentive (nested under question)
    # path('dashboard/survey-sets/<uuid:set_pk>/questions/<uuid:survey_pk>/incentive/', IncentiveView.as_view()),

    # Dashboard — alerts
    path('dashboard/alerts/', AlertListView.as_view()),
    path('dashboard/alerts/<uuid:pk>/', AlertDetailView.as_view()),

    # Dashboard — insights
    path('dashboard/insights/', InsightsView.as_view()),

    # Dashboard — comments
    path('dashboard/comments/', CommentFeedView.as_view()),

    # Dashboard — organization detail
    path('dashboard/organization/', OrganizationView.as_view()),

    # Incentives
    path('dashboard/incentives/',               IncentiveListCreateView.as_view()),
    path('dashboard/incentives/<uuid:pk>/',     IncentiveDetailView.as_view()),
    path('dashboard/incentives/<uuid:pk>/assign/', IncentiveAssignView.as_view()),
    path('dashboard/redeem/',                   RedeemValidateView.as_view()),
    path('dashboard/redeem/<str:code>/use/',    RedeemUseView.as_view()),

    # Public survey PWA
    path('survey/<uuid:location_uuid>/', PublicSurveyDetailView.as_view()),
    path('survey/<uuid:location_uuid>/response/', SurveyResponseView.as_view()),

    # NFC tag claim
    path('tags/<uuid:tag_id>/', NfcTagView.as_view()),

    # Admin Views
    path('admin/overview/',       AdminOverviewView.as_view()),
    path('admin/organizations/',  AdminOrganizationListView.as_view()),
    path('admin/tags/',           AdminTagListView.as_view()),
    path('admin/signups/',        AdminRecentSignupsView.as_view()),
    path('admin/tags/<uuid:tag_id>/release/', AdminTagDetailView.as_view()),

    # Billing
    path('billing/checkout/', CheckoutView.as_view()),
    path('billing/portal/',   PortalView.as_view()),
    path('billing/webhook/',  WebhookView.as_view()),
]