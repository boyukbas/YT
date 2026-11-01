# Instagram setup: Almanca Dil Atölyesi

Goal: the PC posts each lesson's Shorts as Instagram Reels, **one a day after 20:00 (Istanbul)**, each with its own cover
and caption. You set it up once (about 30 minutes, steps 1–5). After that nothing is needed per video.

Everything prepared is in Drive: `SINEM-YouTube/_channel/instagram/` (profile picture, this guide) and, per lesson,
`Output/cover-short-N.jpg` + `Output/instagram.md` (captions, also handy for posting by hand).

---

## 1. Create the account (phone, Instagram app) ~5 min

Use a new email address or phone number for the account. Then fill in:

| Field | Value |
|---|---|
| Username | `almancadilatolyesi` (if taken: `almanca.dil.atolyesi`, `almancadilatolyesi_tr`, `sinem.almanca`) |
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

## 2. Switch to a professional account (phone) ~2 min

The API only works with professional accounts.
Profile → ☰ → **Account type and tools** → **Switch to professional account** → **Creator** → category
**Education** (or "Teacher") → keep contact info off → **skip** connecting a Facebook Page.

**Use it by hand for 2–3 days before automating:** follow ~20 German-learning and Turkish accounts, like a few posts,
post the first Reel yourself (Short 1, caption from `instagram.md`). Brand-new accounts that start posting through an
API right away are more likely to be flagged as bots.

## 3. Meta developer app (PC, Chrome) ~10 min

Needs a Facebook login (as far as I know Meta developer accounts still require one; it can be your personal account,
nothing is posted there).

1. Open **developers.facebook.com** → **Get started** / log in → register as a developer (verify phone/email).
2. **My Apps → Create app**.
   - App name: `Almanca Atolyesi Poster` (Meta rejects some words, e.g. "Insta"; any name is fine)
   - Use case: **Manage messaging & content on Instagram**
   - Business portfolio: **I don't want to connect a business portfolio yet**
   - Create (it asks your Facebook password once).
3. In the app: **Instagram → API setup with Instagram login**.
   - **1. Generate access tokens → Add account** → log in to the **new Instagram account** → allow.
   - If it says the account isn't a tester: left menu **App roles → Roles → Add people → Instagram Tester** → the
     username. Then in the Instagram app: Settings → **Website permissions → Apps and websites → Tester invites** →
     accept, and try "Add account" again.
4. Next to the account: **Generate token** → tick "I understand" → **Copy**.
   Keep this token secret: don't paste it into a chat, email or Drive.

The app stays in **Development mode**. That's fine: it only posts to accounts that have a role in the app (yours).
No App Review, no business verification.

## 4. Give the token to the PC ~1 min

On the PC, open a terminal and run:

```
D:\YT\work\venv\Scripts\python D:\code\YT\pipeline\instagram.py token
```

Paste the token (it isn't shown) → Enter. It prints `Instagram: @almancadilatolyesi (MEDIA_CREATOR), …`.
The PC refreshes the token every week (it's valid 60 days and gets extended each time).

## 5. Turn on automatic posting

- **How the video gets to Instagram:** Meta downloads it from a link. While one Reel is posted, the PC opens a
  temporary, random Cloudflare address to that one video + cover and closes it right after (about 1 minute a day).
  Claude Code needs your OK for this (it blocks opening an inbound tunnel by default).
- **The watcher:** `easy.py watch --once` every 10 minutes in Task Scheduler (also needed for YouTube and new lessons).
  It posts the next Reel after 20:00 if none went out in the last 20 hours, oldest lesson first, in Short order.

Check any time: `instagram.py check` (account, posts used of the 24 h limit), `instagram.md` in the lesson's Output
shows which Reels went out with their links, and `D:\YT\easy-work\instagram.log` has every post or problem.

---

### Good to know

- **Cadence:** one Reel a day keeps followers happy; posting several at once gets fewer views each.
- **Hashtags:** at most 5 per caption (the captions do this). More looks like spam.
- **Covers:** the profile grid shows the middle 3:4 of the cover; the covers keep the text inside it.
- **Music:** the Reels carry our own background music from YouTube's Audio Library. That's fine on Instagram as
  well; just never add Instagram's music on top by hand to a Reel that was posted through the API.
- **Long video:** 16:9 and 6+ minutes doesn't work on Instagram; the Reels point to YouTube instead ("link profilde").
- **Not possible by API:** Instagram's own music, collaborators via invite, editing the cover after posting.
