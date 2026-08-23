# 📖 StoryForge

Turn any idea into an illustrated AI micro-story — powered by [Pollinations](https://pollinations.ai).

**Live app:** https://storyforge.wiradifit-makmur-sejahtera.duckdns.org

## How it works (BYOP — Bring Your Own Pollen)

1. Click **Sign in with Pollinations** — you authorize StoryForge via OAuth (PKCE) with a budget *you* set.
2. Describe your idea, pick a genre and length.
3. StoryForge writes an original micro-story **and** generates matching cover art.
4. Generation is paid from **your own Pollinations pollen** — StoryForge never touches your balance beyond the budget you approved.

StoryForge is a zero-dependency Python stdlib app (`http.server` + SQLite) behind Caddy TLS.

## Quests this app fulfills
- `app_active` — first user connects via the authorize flow
- `app_listed` — submitted to the community app directory
- Developer earnings: every user generation pays the developer +25% over base rates (`earningsEnabled: true`)

## Run your own

```bash
python3 app.py            # serves 127.0.0.1:3095
```

Register an App Key at [enter.pollinations.ai/keys](https://enter.pollinations.ai/keys) with:
- type: publishable, earningsEnabled: true
- redirect URI: `https://yourdomain/auth/callback`

Then drop the key JSON into `../state/appkey.json`.

## API usage

- Text: `POST https://gen.pollinations.ai/v1/chat/completions` (OpenAI-compatible)
- Image: `GET https://gen.pollinations.ai/image/{prompt}?model=flux`
- Auth docs: [BRING_YOUR_OWN_POLLEN.md](https://github.com/pollinations/pollinations/blob/main/BRING_YOUR_OWN_POLLEN.md)

---
Built by WIRADIFIT SOFTWARE LABS · pollination-quests department
