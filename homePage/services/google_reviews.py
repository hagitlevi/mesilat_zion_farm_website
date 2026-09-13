import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
ACCOUNTS_URL = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
LOCATIONS_URL_TMPL = "https://mybusinessbusinessinformation.googleapis.com/v1/accounts/{account_id}/locations"
REVIEWS_URL_TMPL = "https://mybusiness.googleapis.com/v4/accounts/{account_id}/locations/{location_id}/reviews"

STAR_RATING_MAP = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


def star_rating_to_int(value: str) -> int:
    """ממפה את ה-enum של גוגל (ONE..FIVE) למספר; ברירת מחדל 5 אם הערך לא מוכר."""
    return STAR_RATING_MAP.get(value, 5)


def exchange_code_for_tokens(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def list_accounts(access_token: str) -> list:
    resp = requests.get(
        ACCOUNTS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("accounts", [])


def list_locations(access_token: str, account_id: str) -> list:
    url = LOCATIONS_URL_TMPL.format(account_id=account_id)
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"readMask": "name,title"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("locations", [])


def fetch_all_reviews(access_token: str, account_id: str, location_id: str) -> list:
    url = REVIEWS_URL_TMPL.format(account_id=account_id, location_id=location_id)
    headers = {"Authorization": f"Bearer {access_token}"}
    reviews = []
    page_token = None
    while True:
        params = {"pageToken": page_token} if page_token else {}
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        reviews.extend(data.get("reviews", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return reviews
