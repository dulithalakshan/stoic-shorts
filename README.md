# Stoic Shorts bot

Makes one 20–30 s stoic/discipline YouTube Short every day and uploads it.

Gemini (script) → Edge TTS (voice + word timings) → Pexels (portrait footage) → FFmpeg (1080×1920, captions) → YouTube Data API.

Privacy policy: [PRIVACY.md](PRIVACY.md)

## Secrets (Settings → Secrets and variables → Actions)
| Secret | Where from |
|---|---|
| `GEMINI_API_KEY` | aistudio.google.com/apikey |
| `PEXELS_API_KEY` | pexels.com/api |
| `YT_CLIENT_ID`, `YT_CLIENT_SECRET` | Google Cloud → Google Auth Platform → Clients (Web app) |
| `YT_REFRESH_TOKEN` | OAuth Playground, scope `https://www.googleapis.com/auth/youtube.upload` |

Optional repository variable `YT_PRIVACY` = `public` / `unlisted` / `private`.

## Notes
- Change the posting time in `.github/workflows/daily-short.yml` (cron is UTC).
- Add a `music/` folder with royalty-free `.mp3` files for background music; otherwise a quiet low drone is used.
- Until the Google Cloud project passes YouTube's API audit, API uploads are forced to **private**.
- Keep the OAuth consent screen **In production** (not Testing), or the refresh token expires after 7 days.
