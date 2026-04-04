from .tag_views import NfcTagView
from .auth_views import RegisterView, LoginView, MeView, TokenRefreshView
from .dashboard_views import (
    LocationListView, LocationDetailView,
    SurveyListView, SurveyDetailView,
    IncentiveView,InsightsView, AlertListView, AlertDetailView
)
from .survey_views import PublicSurveyDetailView, SurveyResponseView