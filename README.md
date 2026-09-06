# SwasthyaAI

A multilingual health-awareness chatbot. Ask health questions in any of 12+
Indian languages and get clear, non-diagnostic guidance powered by **Groq**.
Includes email/password and Google sign-in, with saved chat history for
signed-in users.

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate        # Windows: .\venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure secrets
cp .env.example .env            # then fill in real values in .env

# 4. Run
python app.py
```

Open http://localhost:5000

## Environment variables

See `.env.example`. You need at minimum a `SECRET_KEY` and a `GEMINI_API_KEY`.
Google sign-in also needs `GOOGLE_OAUTH_CLIENT_ID` and
`GOOGLE_OAUTH_CLIENT_SECRET`. For local OAuth over http, set
`OAUTHLIB_INSECURE_TRANSPORT=1` (never in production).

## Project structure

```
app.py                  Flask app: pages, auth, chat API, history API
chatbot.py              Gemini wrapper (prompt + call)
models.py               SQLAlchemy models (User, Message)
templates/index.html    Landing page + guest chat + auth modal
templates/dashboard.html  Signed-in chat with saved history
requirements.txt        Python dependencies
.env.example            Config template (copy to .env)
```

## Notes

- Guests can chat without an account; those messages are **not** saved.
- Signed-in users get saved history, and recent turns are fed back to the model
  for context.
- This app is for health **awareness** only. It does not diagnose and is not a
  substitute for professional medical care.

## Security

Never commit your real `.env`. If an API key or OAuth secret has ever been
shared or committed, rotate it immediately.
