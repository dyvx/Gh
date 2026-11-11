# Deploying to Vercel

One Vercel project serves both parts on a single `*.vercel.app` domain
(configured in `vercel.json`):

- `frontend/` - Vue 3 + Vite, built to static files
- `backend/`  - FastAPI, run as a Vercel Function, reachable under `/api/*`

## 1. Create a Postgres database

Vercel has no built-in disk or database, so the app needs a hosted PostgreSQL.
Neon (free tier) is the simplest: Vercel dashboard -> Storage -> Create Database
-> Neon. It adds `DATABASE_URL` to the project automatically. Any other
Postgres (Supabase, etc.) works too - copy its connection string.

Tables are created automatically on the first request.

## 2. Environment variables

Project -> Settings -> Environment Variables:

| Name | Required | Value |
| --- | --- | --- |
| `DATABASE_URL` | yes | Postgres connection string (set for you if you used the Neon integration) |
| `SECRET_KEY` | yes | Long random string, e.g. output of `openssl rand -hex 32`. Do not change it later: it signs logins and encrypts users' saved API keys |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | recommended | Creates the admin account on first start |
| `DEEPSEEK_API_KEY` | optional | Server-side chat key. Each user can also enter their own key on the profile page |
| `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL` | optional | Default `https://api.deepseek.com`; any OpenAI-compatible provider works |
| `PAINTING_API_KEY`, `PAINTING_BASE_URL` | optional | Image generation (Doubao Seedream). Can also be set later in the admin page |
| `VISION_API_KEY` | optional | Lets the character understand photos you send |

The backend refuses to serve (HTTP 503) until `SECRET_KEY` is set.

## 3. Deploy

Either import the Git repository in the Vercel dashboard (leave Root Directory
as the repository root, do not override build settings), or from this folder:

```bash
npm i -g vercel
vercel link        # choose the project name = your subdomain
vercel --prod
```

The project name becomes the address: project `my-name` -> `https://my-name.vercel.app`.

## 4. Check it

Open `https://<project>.vercel.app/api/health`. Expected:

```json
{"status": "ok", "secret_key": "ok", "database": "ok"}
```

Then open the site, register, add an API key on the profile page, and chat.

## What changed from the original (Docker) version

- Chat uses a streaming HTTP request instead of a WebSocket.
- Redis is no longer needed (captcha is stateless).
- Avatars and admin settings are stored in Postgres instead of on disk.
- Photos are shrunk in the browser before upload (Vercel caps requests at 4.5 MB).

The Docker / self-hosted setup still works with the same code.
