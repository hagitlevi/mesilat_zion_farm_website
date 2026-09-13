import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from homePage.services.google_reviews import (
    exchange_code_for_tokens,
    list_accounts,
    list_locations,
)

REDIRECT_URI = "http://localhost:8765/callback"
SCOPE = "https://www.googleapis.com/auth/business.manage"
AUTH_BASE_URL = "https://accounts.google.com/o/oauth2/v2/auth"


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        self.server.auth_code = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            "<html><body>ההרשאה התקבלה, אפשר לסגור את החלון ולחזור לטרמינל.</body></html>".encode("utf-8")
        )

    def log_message(self, format, *args):
        pass  # לא להציף את הטרמינל בלוגים של השרת הזמני


class Command(BaseCommand):
    help = "הרשאה חד-פעמית מול Google (OAuth) לקבלת refresh token + מזהי חשבון/מיקום עסקי"

    def handle(self, *args, **options):
        client_id = getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
        client_secret = getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise CommandError(
                "יש להגדיר GOOGLE_OAUTH_CLIENT_ID ו-GOOGLE_OAUTH_CLIENT_SECRET ב-.env לפני הרצת הפקודה."
            )

        params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
        }
        auth_url = f"{AUTH_BASE_URL}?{urllib.parse.urlencode(params)}"

        self.stdout.write("פתחי את הכתובת הבאה בדפדפן והתחברי עם החשבון שמנהל את הפרופיל העסקי:")
        self.stdout.write(auth_url)
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

        server = HTTPServer(("localhost", 8765), _CallbackHandler)
        server.auth_code = None
        self.stdout.write("ממתינה לאישור בדפדפן...")
        while server.auth_code is None:
            server.handle_request()

        code = server.auth_code
        tokens = exchange_code_for_tokens(client_id, client_secret, code, REDIRECT_URI)
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise CommandError(
                "גוגל לא החזירה refresh_token. נסי שוב - ואם זה חוזר על עצמו, בטלי הרשאה קודמת "
                "ב-https://myaccount.google.com/permissions ונסי שוב."
            )

        access_token = tokens["access_token"]

        accounts = list_accounts(access_token)
        if not accounts:
            raise CommandError("לא נמצא אף חשבון עסקי מחובר למשתמש הזה.")
        if len(accounts) > 1:
            self.stdout.write("נמצאו כמה חשבונות - הפקודה בחרה את הראשון; אם זה לא הנכון, עדכני ידנית:")
            for acc in accounts:
                self.stdout.write(f"  {acc['name']} — {acc.get('accountName', '')}")
        account_id = accounts[0]["name"].split("/")[-1]

        locations = list_locations(access_token, account_id)
        if not locations:
            raise CommandError("לא נמצא אף מיקום עסקי תחת החשבון הזה.")
        if len(locations) > 1:
            self.stdout.write("נמצאו כמה מיקומים - הפקודה בחרה את הראשון; אם זה לא הנכון, עדכני ידנית:")
            for loc in locations:
                self.stdout.write(f"  {loc['name']} — {loc.get('title', '')}")
        location_id = locations[0]["name"].split("/")[-1]

        self.stdout.write("")
        self.stdout.write("הוסיפי את השורות הבאות לקובץ .env:")
        self.stdout.write(f"GOOGLE_OAUTH_REFRESH_TOKEN={refresh_token}")
        self.stdout.write(f"GOOGLE_BUSINESS_ACCOUNT_ID={account_id}")
        self.stdout.write(f"GOOGLE_BUSINESS_LOCATION_ID={location_id}")
