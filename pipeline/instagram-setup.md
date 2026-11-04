# Instagram setup: Almanca Dil Atölyesi

Goal: the PC posts each lesson's Shorts as Reels on **Instagram**, **one a day after 20:00 (Istanbul)**, each with its
own cover and caption. You set it up once (steps 1–5). After that nothing is needed per video.

**Facebook (2026-11-03):** the PC posts nothing on Facebook (`FACEBOOK_PAGE = False`). But **a Facebook Page must stay
linked to the Instagram account**: after the old Page was deleted, Instagram accepted the video but refused the final
publish ("Fatal", subcode 2207085) with every key. A new, empty Page "Almanca Dil Atölyesi" was linked the same day
(Instagram → Edit profile → Facebook → Connect) and posting worked again. Don't delete it; it can stay empty.

## The key in use: a system user's token (Business settings) ~5 min

Verified 2026-11-03 (Short 4 of "new" posted with it). Doesn't expire.

1. **business.facebook.com → Settings** (portfolio "Almanca Dil Atölyesi"). **Instagram profiles**: `@almancadilatolyesi`
   is owned by the portfolio, and under **Connected assets** the Facebook Page is listed.
2. **Users → System users** → the system user (now: **Claude**) → **Assign assets → Instagram profiles →
   almancadilatolyesi → full control**. (The app "Almanca Dil Atölyesi" must be assigned too.)
3. **Generate token** → app **Almanca Dil Atölyesi**, expiry **Never**, permissions `instagram_basic`,
   `instagram_content_publish` (+ `instagram_manage_contents`) → copy it. Don't paste it in a chat, email or Drive.
4. On the PC, in a terminal:

   ```
   D:\YT\workenv\Scripts\python D:\code\YT\pipeline\instagram.py system-token
   ```

   Paste the token (not shown) → it prints the account and the check. "has no Instagram account" = step 2 is missing.

**Go slowly with Meta.** On 2026-11-03 many logins, keys and Page changes within a few hours got the developer account
flagged ("Account confirmation needed", all keys "API access blocked") until it was confirmed on
developers.facebook.com. One key change at a time, and don't switch Facebook profiles in the middle of one: a Graph API
Explorer token dies when you do ("the user logged out").

(The browser routes `unified-token` (Unified login, its configuration and the `ig-login.html` redirect) and `fb-token`
(Graph API Explorer, steps 3–4 below) also still exist. The Unified-login key lost the Instagram account when the new
Page was linked.)

Everything prepared is in Drive: `SINEM-YouTube/_channel/instagram/` (profile picture, this guide) and, per lesson,
`Output/extras/cover-short-N.jpg` + `Output/extras/instagram.md` (captions, also handy for posting by hand).

Done so far (2026-11-02): Instagram `@almancadilatolyesi`, Facebook Page "Almanca Dil Atölyesi", Meta app
"Almanca Dil Atölyesi" with the Instagram and Page use cases.

---

## 1. Create the Instagram account (phone, Instagram app) ~5 min

Use a new email address or phone number for the account. Then fill in:

| Field | Value |
|---|---|
| Username | `almancadilatolyesi` |
| Name (searchable!) | `Almanca Dil Atölyesi • Almanca Öğren` |
| Profile picture | `_channel/instagram/profile-picture.jpg` (TR/DE logo, same as YouTube; Instagram takes JPG) |
| Bio | see below |
| Link | `https://www.youtube.com/@sinembb` (Edit profile → Links → Add external link, title "YouTube dersleri") |

Bio (copy as is, 5 lines; chosen 2026-11-02, 143 of 150 characters):

```
A1'den C1'e Almanca 🇩🇪
👩‍🏫 Almanca öğretmeni Sinem
📚 Gramer · Goethe & telc sınav taktikleri
🏠 Almanya'da hayat
👇 Tüm dersler YouTube'da
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
   `token never expires`, the permissions, and the Instagram + Page follower counts. A `WARNING: missing …` line means
   a permission wasn't ticked in 4.3: tick it, generate a new token and run it again. The secret and the short token are
   not stored; only the Page token is (`%USERPROFILE%\.shorts-pipeline\instagram.json`).

If it says "No Instagram account linked", do the linking in step 2 and make a new token (4.1–4.5).

## 5. Automatic posting (already on)

- **How the video gets there:** Instagram downloads the video + cover from a temporary, random Cloudflare address
  that the PC opens for 2–3 minutes and closes right after (only those two files answer; tested 2026-11-02: link up in
  ~70 s, Instagram done ~40 s later). Its direct upload refuses Page keys.
- **The watcher:** Task Scheduler task "YT Easy mode watcher" runs every 10 minutes (also does YouTube and new
  lessons). After 20:00, if nothing went out in the last 20 hours, it posts the next Reel on Instagram. Oldest lesson
  first, in Short order. (Posting the Reels and the long video on the Page too is built in but switched off:
  `FACEBOOK_PAGE` in `instagram.py`.) Nothing is posted until step 4 is done, and not
  before the start date in `_channel/channel.json` → `"instagram": {"start": "2026-11-05"}` (change or delete it to
  start earlier).
- **Renewal:** the system user's key never expires. If it stops working, generate a new one (section "The key in
  use") and run `system-token`.

Check any time: `instagram.py check` (accounts, posts used of the 24 h limit), `instagram.md` in the lesson's Output/extras
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

- **Profile art:** `_channel/brand/` has the Instagram highlight covers and the YouTube banner (and a Facebook cover).
- **Not possible by API:** Instagram's own music, collaborators via invite, editing the cover after posting.
