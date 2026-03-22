# Mesilat Zion Farm Website

A full-stack Django web application for [Mesilat Zion Horse Farm](https://en.wikipedia.org/wiki/Mesilat_Ziyyon), a horse riding farm near Jerusalem, Israel. The system manages activity bookings, scheduling, payments, customer reviews, and staff administration — fully localized in Hebrew with RTL support.

---

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Getting Started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Local Development](#local-development)
  - [Docker](#docker)
- [Configuration](#configuration)
- [Database Models](#database-models)
- [API Endpoints](#api-endpoints)
- [Admin Interface](#admin-interface)
- [Deployment](#deployment)

---

## Features

### Customer-Facing
- **Activity Pages** — Dedicated pages for 9+ riding activities: lessons, night riding, sunrise riding, couple rides (including picnic variants), group rides, carriage trips, children's activities, and photography sessions
- **Real-Time Booking** — Interactive appointment picker with 15-minute slot granularity and live availability checking
- **Slot Hold Mechanism** — Temporarily locks slots during checkout to prevent double-booking
- **Payment Processing** — Hosted payment flow with webhook-based confirmation and status tracking
- **Consent Management** — Phone-based terms & privacy consent tracking (no cookies required), with versioning support
- **Reviews System** — 5-star ratings with paginated display and honeypot spam protection
- **Cancellation Requests** — Multi-channel cancellation support (web, phone, WhatsApp) with status tracking
- **Policy Pages** — Terms of service, privacy policy, and cancellation policy

### Admin / Staff
- **Appointment Calendar** — Visual calendar with drag-and-drop scheduling
- **Business Hours Editor** — Season-aware schedule configuration with automatic DST detection (Israel timezone)
- **Hebrew Calendar Support** — Holiday and recurring special-date rules using the Hebrew calendar
- **Batch Operations** — Bulk email/SMS sending, payment processing, and booking management
- **Automated Notifications** — Booking confirmations, SMS feedback requests, and payment alerts via ntfy.sh
- **Instructor Management** — Staff profiles with color-coded calendar views
- **Therapeutic Riding Sessions** — Dedicated tracking for treatment sessions
- **Monthly Summaries & Revenue Reports**

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Django 5.2.4 (Python 3.13) |
| Database | PostgreSQL (production), SQLite (development) |
| Application Server | Gunicorn 25.1.0 |
| Static Files | WhiteNoise (compression + serving) |
| Containerization | Docker (python:3.13.2-slim) |
| Email | Gmail SMTP |
| SMS / Push Notifications | ntfy.sh webhooks |
| Hebrew Calendar | `convertdate` library |
| Frontend | HTML5, CSS3, Vanilla JavaScript (RTL) |

---

## Project Structure

```
mesilat_zion_farm_website/
├── mesilat_zion_farm_website/   # Django project configuration
│   ├── settings.py              # App settings (DB, email, logging, policies)
│   ├── urls.py                  # Root URL routing
│   ├── wsgi.py
│   └── asgi.py
├── homePage/                    # Main application
│   ├── models.py                # 13 data models (Activity, Booking, Appointment, ...)
│   ├── admin.py                 # Heavily customized Django admin
│   ├── forms.py                 # Booking, review, and cancellation forms
│   ├── urls.py                  # 38 URL patterns
│   ├── utils.py
│   ├── views/
│   │   ├── pages.py             # Static activity & gallery pages
│   │   ├── booking.py           # Full booking workflow
│   │   ├── payment.py           # Payment initiation, return, and webhooks
│   │   ├── consent.py           # Terms/privacy consent API
│   │   └── reviews.py           # Reviews and cancellation requests
│   ├── services/
│   │   ├── booking_service.py   # Slot availability, business hours logic
│   │   ├── ntfy_gateway.py      # SMS/email notification gateway
│   │   ├── slot_hold.py         # Appointment hold/release (token-based)
│   │   └── payments/            # Payment service modules
│   ├── management/commands/
│   │   └── send_feedback_requests.py  # Scheduled post-booking feedback SMS
│   ├── templates/homePage/      # 23 HTML templates
│   ├── static/homePage/         # CSS, JavaScript, and image assets
│   ├── templatetags/            # Custom Django template tags
│   └── migrations/              # Database migrations
├── manage.py
├── requirements.txt
├── Dockerfile
└── .env                         # Environment variables (not committed)
```

---

## Getting Started

### Prerequisites

- Python 3.13+
- PostgreSQL (optional for local; SQLite works out of the box)
- Docker (optional)

### Local Development

1. **Clone the repository**

   ```bash
   git clone <repository-url>
   cd mesilat_zion_farm_website
   ```

2. **Create and activate a virtual environment**

   ```bash
   python -m venv venv
   source venv/bin/activate   # Windows: venv\Scripts\activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables**

   Copy `.env.example` to `.env` (or create `.env`) and fill in the required values. See [Configuration](#configuration) for details.

5. **Apply migrations**

   ```bash
   python manage.py migrate
   ```

6. **Create a superuser**

   ```bash
   python manage.py createsuperuser
   ```

7. **Run the development server**

   ```bash
   python manage.py runserver
   ```

   The application will be available at `http://127.0.0.1:8000`.

### Docker

```bash
# Build the image
docker build -t mesilat-zion:latest .

# Run the container
docker run -p 8000:8000 --env-file .env mesilat-zion:latest
```

> **Note:** The Dockerfile uses Django's development server. For production, update the `CMD` to use Gunicorn (see [Deployment](#deployment)).

---

## Configuration

All configuration is loaded from a `.env` file in the project root. Key variables:

| Variable | Description | Default |
|---|---|---|
| `SECRET_KEY` | Django secret key | — |
| `DEBUG` | Enable debug mode | `False` |
| `DATABASE_URL` | PostgreSQL connection string | SQLite fallback |
| `EMAIL_HOST_USER` | Gmail SMTP username | — |
| `EMAIL_HOST_PASSWORD` | Gmail app password | — |
| `DEFAULT_FROM_EMAIL` | Sender display name | — |
| `SEND_SMS` | Enable SMS notifications | `False` |
| `SEND_EMAIL` | Enable email notifications | `False` |
| `NTFY_URL` | ntfy.sh base URL | `https://ntfy.sh` |
| `NTFY_TOPIC` | ntfy.sh topic for notifications | — |
| `NTFY_PRIORITY` | Notification priority (1–5) | `5` |
| `FEEDBACK_URL` | Base URL for feedback links in SMS | — |
| `GOOGLE_PLACE_ID` | Google Places ID (optional) | — |
| `GOOGLE_PLACES_API_KEY` | Google Places API key (optional) | — |

**Policy versions** (configured in `settings.py`):

| Setting | Value |
|---|---|
| `TERMS_VERSION` | `1.3` |
| `PRIVACY_VERSION` | `1.2` |
| `MARKETING_VERSION` | `1.3` |

---

## Database Models

| Model | Description |
|---|---|
| `Activity` | Riding activity types with pricing |
| `Appointment` | 15-minute bookable time slots with hold/release mechanism |
| `Booking` | Customer reservations linked to appointments and payments |
| `Payment` | Payment transaction records (created → pending → succeeded/failed/refunded) |
| `BusinessHours` | Operating hours by season (DST-aware, summer/winter) |
| `Weekday` | Day-of-week configuration |
| `CustomSchedule` | Special date rules supporting both Gregorian and Hebrew calendar dates |
| `ActivityRule` | Activity availability rules per day and time |
| `SiteReview` | Customer star ratings and comments |
| `CancellationRequest` | Cancellation requests with multi-channel tracking |
| `TermsConsent` | Versioned terms/privacy consent by phone number |
| `MarketingConsent` | Per-channel marketing opt-in tracking (SMS, Email, WhatsApp) |
| `Instructor` | Staff profiles with color-coded calendar display |
| `TreatmentSession` | Therapeutic riding session records |

---

## API Endpoints

### Appointment Management
| Method | URL | Description |
|---|---|---|
| `GET` | `/available-appointment/<activity_id>/` | Fetch available time slots |
| `POST` | `/appointments/hold/` | Hold a slot during checkout |
| `POST` | `/appointments/release/` | Release a held slot |
| `POST` | `/appointments/renew/` | Extend hold expiration |
| `GET` | `/appointments/snapshot/` | Current appointment availability snapshot |

### Booking & Payment
| Method | URL | Description |
|---|---|---|
| `GET/POST` | `/booking-form/` | Display and submit the booking form |
| `POST` | `/confirm-booking/` | Validate consent and prepare for checkout |
| `GET/POST` | `/pay/start/` | Initiate a payment session |
| `GET` | `/pay/return/` | Payment provider success callback |
| `GET` | `/pay/mock-checkout/<payment_id>/` | Mock checkout page (testing) |
| `POST` | `/pay/webhook/` | Payment status webhook receiver |

### Content & Policies
| Method | URL | Description |
|---|---|---|
| `GET/POST` | `/reviews/` | View and submit customer reviews |
| `GET/POST` | `/cancel-request/` | Submit a cancellation request |
| `GET` | `/cancel-policy/` | Cancellation policy page |
| `GET` | `/terms/` | Terms of service |
| `GET` | `/privacy/` | Privacy policy |
| `GET` | `/api/consent-status/` | Check whether phone number requires consent |

---

## Admin Interface

The Django admin at `/admin/` is extensively customized for farm staff:

- **Schedule Board** — Visual appointment calendar with drag-and-drop support
- **Business Hours** — Edit seasonal operating hours with DST awareness
- **Custom Schedules** — Block or adjust availability for holidays and special dates (supports Hebrew calendar)
- **Bookings** — View, filter, and manage all reservations; process payments and refunds
- **Monthly Summaries** — Revenue and booking statistics per month
- **Consent Dashboard** — Track which customers have accepted which policy versions
- **Notifications** — Send batch SMS/email messages to selected customers
- **Instructors & Treatment Sessions** — Manage staff and therapeutic riding records

---

## Deployment

The application is designed for deployment on [Render.com](https://render.com) with a managed PostgreSQL database.

**Production checklist:**

1. Set `DEBUG=False` in `.env`
2. Set a strong `SECRET_KEY`
3. Set `DATABASE_URL` to your PostgreSQL connection string
4. Configure email credentials
5. Collect static files: `python manage.py collectstatic`
6. Use Gunicorn as the WSGI server:
   ```bash
   gunicorn mesilat_zion_farm_website.wsgi:application --bind 0.0.0.0:8000
   ```
7. Schedule `send_feedback_requests` management command via cron or Render's cron job feature

---

## Management Commands

```bash
# Send SMS feedback requests to customers with completed bookings
python manage.py send_feedback_requests
```

This command is intended to run on a scheduled basis (e.g., daily) to prompt recent customers for reviews.
