# Instagram + Facebook setup: Almanca Dil Atölyesi

Goal: the PC posts each lesson's Shorts as Reels on **Instagram and the Facebook Page**, **one a day after 20:00
(Istanbul)**, each with its own cover and caption. You set it up once (steps 1–5). After that nothing is needed per video.

Everything prepared is in Drive: `SINEM-YouTube/_channel/instagram/` (profile picture, this guide) and, per lesson,
`Output/cover-short-N.jpg` + `Output/instagram.md` (captions, also handy for posting by hand).

Done so far (2026-11-02): Instagram `@almancadilatolyesi`, Facebook Page "Almanca Dil Atölyesi", Meta app
"Almanca Dil Atölyesi" with the Instagram and Page use cases.

---

## 1. Create the Instagram account (phone, Instagram app) ~5 min

Use a new email address or phone number for the account. Then fill in:

| Field | Value |
|---|---|
| Username | `almancadilatolyesi` |
| Name (searchable!) | `Almanca Dil Atölyesi • Almanca Öğren` |
| Profile picture | `_channel/instagram/profile-picture.png` (TR/DE logo, same as YouTube) |
| Bio | see below |
| Link | `https://www.youtube.com/@sinembb` (Edit profile → Links → Add external link, title "YouTube dersleri") |

Bio (copy as is, 4 lines):

```
🇹🇷→🇩🇪 Türkçe anlatımla Almanca
🎯 A1–B2 · Goethe sınav hazırlık
🎬 Her gün 1 kısa ders · Sinem
👇 Dersin tamamı YouTube'da
```

## 2. Professional account + link it to the Facebook Page (phone / PC) ~5 min

- Instagram: Profile → ☰ → **Account type and tools** → **Switch to professional account** → **Creator** → category
  **Education** → keep contact info off. The account must be **public**.
- Link it to the Page: on Facebook, the Page → **Settings → Linked accounts → Instagram → Connect account** → log in as
  `almancadilatolyesi` (or in Instagram: Edit profile → **Page** → Connect).

**Use it by hand for 2–3 days before automating:** follow ~20 German-learning and Turkish accounts, like a few posts,
post the first Reel yourself (Short 1, caption from `instagram.md`). Brand-new accounts that start posting through an
API right away are more likely to be flagged as bots.

## 3. Meta developer app (PC, Chrome) ~10 min

1. **developers.facebook.com** → **My Apps** → the app → **Use cases**.
2. **Manage messaging & content on Instagram → Customize → API setup with Facebook login**. The content permissions
   (`instagram_basic`, `instagram_content_publishing`, `pages_read_engagement`, `business_management`,
   `pages_show_list`) are listed there; if there is an **Add** button for them, click it. Skip the messaging ones.
   (Meta calls this setup "legacy" and recommends "Unified login"; legacy still works, and Unified login needs a
   business portfolio and isn't documented yet.)
3. **Use cases → Manage everything on your Page → Customize** → **Add** `pages_manage_posts` (and
   `pages_read_engagement`, `pages_show_list` if not there yet).
4. Leave the app **Unpublished** (Development mode): it can post to accounts that have a role in the app (yours). No
   App Review, no business verification.

## 4. Get the token and give it to the PC ~5 min

1. **developers.facebook.com/tools/explorer** (Graph API Explorer).
2. Right side: **Meta App** → `Almanca Dil Atölyesi`; **User or Page** → **Get User Access Token**.
3. **Permissions** → add: `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`,
   `instagram_content_publish`, `business_management`.
4. **Generate Access Token** → Facebook asks which Pages / Instagram accounts: pick **Almanca Dil Atölyesi** and
   **almancadilatolyesi** → Continue / Save.
5. Copy the token (copy icon in the token field). Don't paste it into a chat, email or Drive.
6. In the app: **App settings → Basic**: you need the **App ID** and the **App secret** (Show → Facebook password).
7. On the PC, open a terminal and run:

   ```
   D:\YT\work\venv\Scripts\python D:\code\YT\pipeline\instagram.py fb-token
   ```

   It asks the App ID, then the App secret and the token (neither is shown while you paste). It prints the Page,
   `token never expires`, the permissions, and the Instagram + Page follower counts. The secret and the short token are
   not stored; only the Page token is (`%USERPROFILE%\.shorts-pipeline\instagram.json`).

If it says "No Instagram account linked", do the linking in step 2 and make a new token (4.1–4.5).

## 5. Automatic posting (already on)

- **How the video gets there:** the PC uploads it straight to Meta. The Instagram cover goes up as an unpublished
  (invisible) Page photo for a minute and is deleted after posting. No temporary link is needed in this mode.
- **The watcher:** Task Scheduler task "YT Easy mode watcher" runs every 10 minutes (also does YouTube and new
  lessons). After 20:00, if nothing went out in the last 20 hours, it posts the next Reel: first Instagram, then the
  same Reel on the Page (with the clickable YouTube link of the full lesson). Oldest lesson first, in Short order.
  If the Page part fails, it retries the next day before moving on. Nothing is posted until step 4 is done.

Check any time: `instagram.py check` (accounts, posts used of the 24 h limit), `instagram.md` in the lesson's Output
shows which Reels went out with their links, and `D:\YT\easy-work\instagram.log` has every post or problem.

(Other route, not used: "API setup with Instagram login" + `instagram.py token`, where Meta downloads the video
through a temporary Cloudflare link. New apps can no longer pick it.)

---

### Good to know

- **Cadence:** one Reel a day keeps followers happy; posting several at once gets fewer views each.
- **Hashtags:** at most 5 per caption (the captions do this). More looks like spam.
- **Covers:** the profile grid shows the middle 3:4 of the cover; the covers keep the text inside it.
- **Music:** the Reels carry our own background music from YouTube's Audio Library. That's fine on Instagram as
  well; just never add Instagram's music on top by hand to a Reel that was posted through the API.
- **Long video:** 16:9 and 6+ minutes doesn't work on Instagram; the Reels point to YouTube instead ("link profilde").
- **Not possible by API:** Instagram's own music, collaborators via invite, editing the cover after posting.
