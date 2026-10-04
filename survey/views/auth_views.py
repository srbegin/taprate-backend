import os

from django.contrib.auth import authenticate
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from ..serializers import RegisterSerializer, UserSerializer


# ── Throttle classes ───────────────────────────────────────────────────────────

class LoginRateThrottle(AnonRateThrottle):
    """5 attempts per minute per IP on the login endpoint."""
    scope = 'login'


class PasswordResetRateThrottle(AnonRateThrottle):
    """3 requests per minute per IP on the password reset endpoint."""
    scope = 'password_reset'


# ── Helpers ────────────────────────────────────────────────────────────────────

def _token_pair(user):
    """Return access + refresh token dict for a user."""
    refresh = RefreshToken.for_user(user)
    return {
        'access': str(refresh.access_token),
        'refresh': str(refresh),
    }


# ── Auth views ─────────────────────────────────────────────────────────────────

class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = serializer.save()

        from ..tasks import send_welcome_email
        send_welcome_email.delay(str(user.id))

        return Response(
            {
                'user': UserSerializer(user).data,
                'tokens': _token_pair(user),
            },
            status=status.HTTP_201_CREATED,
        )


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes   = [LoginRateThrottle]

    def post(self, request):
        email    = request.data.get('email', '').lower()
        password = request.data.get('password', '')

        user = authenticate(request, username=email, password=password)
        if not user:
            return Response(
                {'detail': 'Invalid email or password.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        return Response({
            'user': UserSerializer(user).data,
            'tokens': _token_pair(user),
        })


class LogoutView(APIView):
    """
    POST { refresh } → blacklists the refresh token so it can't mint new
    access tokens. The client should also clear its local session.

    Requires authentication so anonymous callers can't burn arbitrary tokens.
    Returns 200 even if the token is already blacklisted — idempotent by design.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response(
                {'detail': 'Refresh token required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except TokenError:
            # Already blacklisted or invalid — treat as a successful logout
            pass

        return Response({'detail': 'Logged out successfully.'})


class ChangePasswordView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        current = request.data.get('current_password', '')
        new_pw  = request.data.get('new_password', '')

        if not current or not new_pw:
            return Response(
                {'detail': 'current_password and new_password are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(new_pw) < 8:
            return Response(
                {'detail': 'New password must be at least 8 characters.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = authenticate(request, username=request.user.username, password=current)
        if not user:
            return Response(
                {'detail': 'Current password is incorrect.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(new_pw)
        user.save()
        return Response({'detail': 'Password updated successfully.'})


class TokenRefreshView(APIView):
    """Thin wrapper so the frontend hits a consistent /api/auth/ prefix."""
    permission_classes = [AllowAny]

    def post(self, request):
        refresh_token = request.data.get('refresh')
        if not refresh_token:
            return Response(
                {'detail': 'Refresh token required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            refresh = RefreshToken(refresh_token)
            return Response({'access': str(refresh.access_token)})
        except TokenError as e:
            return Response({'detail': str(e)}, status=status.HTTP_401_UNAUTHORIZED)


class PasswordResetRequestView(APIView):
    """
    POST { email } → always returns 200 to prevent email enumeration.
    If the email matches an account, a reset link is sent via Resend.
    The link is valid for PASSWORD_RESET_TIMEOUT seconds (Django default: 3 days).
    """
    permission_classes = [AllowAny]
    throttle_classes   = [PasswordResetRateThrottle]

    def post(self, request):
        email = request.data.get('email', '').lower().strip()

        if email:
            from django.contrib.auth import get_user_model
            from ..tasks import send_password_reset_email

            User = get_user_model()
            try:
                user      = User.objects.get(email=email)
                uid       = urlsafe_base64_encode(force_bytes(user.pk))
                token     = PasswordResetTokenGenerator().make_token(user)
                frontend  = os.environ.get('FRONTEND_URL', 'https://taprate.app')
                reset_url = f"{frontend}/auth/reset-password?uid={uid}&token={token}"
                send_password_reset_email.delay(str(user.id), reset_url)
            except User.DoesNotExist:
                pass  # Silent — never reveal whether the email exists

        return Response({
            'detail': "If an account exists with that email, we've sent a reset link."
        })


class PasswordResetConfirmView(APIView):
    """
    POST { uid, token, new_password } → validates token and sets new password.
    Uses Django's PasswordResetTokenGenerator — token is single-use (invalidated
    once the password changes) and time-limited.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        uid          = request.data.get('uid', '')
        token        = request.data.get('token', '')
        new_password = request.data.get('new_password', '')

        if not all([uid, token, new_password]):
            return Response(
                {'detail': 'uid, token, and new_password are required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(new_password) < 8:
            return Response(
                {'detail': 'Password must be at least 8 characters.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        from django.contrib.auth import get_user_model
        User = get_user_model()

        try:
            user_pk = force_str(urlsafe_base64_decode(uid))
            user    = User.objects.get(pk=user_pk)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            return Response(
                {'detail': 'This reset link is invalid or has expired.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not PasswordResetTokenGenerator().check_token(user, token):
            return Response(
                {'detail': 'This reset link is invalid or has already been used.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user.set_password(new_password)
        user.save()
        return Response({'detail': 'Password reset successfully. You can now sign in.'})