# Google Cloud setup for Connect Google

This guide is for whoever owns Wisp's Google sign-in: the developer who ships the tester builds, or
anyone who wants to use their own Google Cloud project instead.

A tester build can carry a built-in OAuth client, supplied at build time with `WISP_GOOGLE_OAUTH_CLIENT`
(see [build-release.md](build-release.md)). That client belongs to a Google Cloud project in **Testing**
mode, so only the Google accounts on its **Test users** list can sign in (sections 3 to 5 below). Testers
see an "unverified app" warning and choose Continue, and their sign-in expires every 7 days. A build with
no built-in client, or someone who prefers their own project, creates a client once (sections 1 to 8) and
pastes it into **Settings ▸ Google ▸ Advanced ▸ Use your own OAuth client**. Wisp always uses a client you
paste in before the built-in one. Allow about 15 minutes.

Wisp makes one sign-in per Google account and asks for these read-only scopes in that one consent
(`service/gmail_source.py` `_consent_scopes`, `service/google_workspace.py` `WORKSPACE_SCOPES`):

| Scope (after `https://www.googleapis.com/auth/`) | Google API to enable | Needed |
| --- | --- | --- |
| `gmail.readonly` | Gmail API | Required. Google calls it a **restricted** scope. |
| `calendar.readonly` | Google Calendar API | Optional. Brief and Day read the next 7 days. |
| `drive.readonly` | Google Drive API | Optional. Google calls it a **restricted** scope. |
| `documents.readonly` | Google Docs API | Optional. |
| `spreadsheets.readonly` | Google Sheets API | Optional. |
| `presentations.readonly` | Google Slides API | Optional. |
| `chat.messages.readonly`, `chat.spaces.readonly` | Google Chat API | Optional. |
| `tasks.readonly` | Google Tasks API | Optional. |
| `contacts.readonly` | People API | Optional. |

Nothing here can send, edit or delete anything in Google. The person signing in can untick any scope
except Gmail on Google's consent screen: Wisp refuses a sign-in without Gmail, and otherwise connects
with whatever was granted (Settings then offers **Reconnect** to add the rest).

Google's console moves things around often. The names below match the "Google Auth Platform" layout
(Overview / Branding / Audience / Clients / Data Access / Verification Center). Where I'm unsure of a
label it says *(unverified)*.

---

## 1. Create a project

1. Open <https://console.cloud.google.com/> and sign in with the account that will own the app (yours).
2. Click the project picker at the top left, next to "Google Cloud", then **New project**.
3. Name it `Wisp`. Leave Organization as is, or "No organization" for a personal account. Click **Create**.
4. Make sure the picker now shows **Wisp** before you go on.

## 2. Enable the APIs

The project needs one API for each scope in the table above. For the built-in tester client, enable all
nine:

**Gmail API, Google Calendar API, Google Drive API, Google Docs API, Google Sheets API, Google Slides API,
Google Chat API, Google Tasks API and People API.**

1. Go to **☰ ▸ APIs & Services ▸ Library**, or open <https://console.cloud.google.com/apis/library>.
2. For each name above, search it, open it, and click **Enable**.

Skipping the optional ones does not stop the others. If you skip the Calendar API, mail still works and
Wisp shows "Turn on the Google Calendar API in your Google Cloud project…" for each account until you
enable it. For the Workspace APIs (Drive through People), the Google tool for that product fails until its
API is on *(unverified: Wisp has no message of its own for these)*. A new API can take a minute to take
effect.

The People API is the one behind the `contacts.readonly` scope. That mapping comes from Google's scope
documentation, not from Wisp's code.

## 3. Google Auth Platform: branding, audience, contact

1. Go to **☰ ▸ Google Auth Platform ▸ Branding**. **APIs & Services ▸ OAuth consent screen** now
   redirects there.
2. If you see "Google Auth Platform not configured yet", click **Get started**:
   - **App Information**: App name `Wisp`. User support email: your email. Click **Next**.
   - **Audience**: choose **External**. Click **Next**.
     - "Internal" only works for accounts in your own Workspace organization, so testers' Gmail accounts can't use it.
   - **Contact Information**: your email. Click **Next**.
   - **Finish**: tick the box agreeing to the Google API Services User Data Policy. Click **Continue**, then **Create**.
3. Optional, under **Branding**: add a logo and an app home page.
   - Leave the logo empty for now. Uploading one can push the app into brand verification.

## 4. Data Access: add the scopes

1. Go to **Google Auth Platform ▸ Data Access**.
2. Click **Add or remove scopes**.
3. In the panel, filter for and tick the scopes from the table at the top:
   - `.../auth/gmail.readonly` (Gmail API, "View your email messages and settings"), listed under
     **restricted** scopes.
   - `.../auth/calendar.readonly` (Google Calendar API, "See and download any calendar you can access…"),
     listed under **sensitive** scopes.
   - `.../auth/drive.readonly` (Google Drive API), listed under **restricted** scopes.
   - `.../auth/documents.readonly`, `.../auth/spreadsheets.readonly`, `.../auth/presentations.readonly`,
     `.../auth/chat.messages.readonly`, `.../auth/chat.spaces.readonly`, `.../auth/tasks.readonly` and
     `.../auth/contacts.readonly`, listed under **sensitive** scopes *(unverified: Google's console
     groups them and the grouping changes)*.
   - If a scope doesn't appear, use **Manually add scopes** at the bottom of the panel. Paste the URLs
     from the table, one per line, then click **Add to table**.
4. Click **Update**, then **Save** on the Data Access page.

You don't need to submit for verification for testing. Section 6 covers what that means.

## 5. Audience: test users (the tester whitelist)

1. Go to **Google Auth Platform ▸ Audience**. Publishing status should read **Testing**.
2. Under **Test users**, click **+ Add users**. Enter your own Gmail address and each tester's Google address
   (comma separated). Click **Save**.
3. Testers you add can sign in. Google still shows its "Google hasn't verified this app" warning: they click
   **Continue** (older screens: **Advanced**, then **Go to Wisp**). Anyone not on the list gets
   **"Access blocked: Wisp has not completed the Google verification process" (Error 403: access_denied)**,
   and Wisp says their account isn't on the tester list yet.

Testing-mode limits:

- **Test users are capped at 100** per project. Removing a user may not give the slot back *(unverified)*.
- **Tokens last 7 days.** Google issues refresh tokens that expire after 7 days when an External app is in
  Testing and asks for anything beyond name/email/profile, which Wisp does. Wisp then shows "Google sign-in
  expired; reconnect in Settings ▸ Google" and posts one "Google sign-in expired" notification per expiry. Tell testers to reconnect about once a week.
- Tokens issued while the app was in Testing keep that 7-day expiry even after you publish *(unverified)*.
  Reconnect after you change the status.

## 6. Fallback if you can't collect testers' emails in advance: publish unverified

1. Go to **Google Auth Platform ▸ Audience** and click **Publish app**. Confirm.
   - The status changes to **In production**.
   - The Verification Center shows the app needs verification. Don't submit it for verification yet.
2. What a tester sees when they tap Connect Google:
   1. The Google account chooser.
   2. **"Google hasn't verified this app"**: the app is asking for sensitive info.
   3. The tester clicks **Advanced**, then **Go to Wisp (unsafe)**.
   4. The consent screen, with a checkbox for each scope in the table. Gmail is the one they must tick.
      - If they untick Calendar, Wisp still connects mail, and Brief and Day show no Google events.
      - If they untick some Workspace scopes (Drive, Docs, Chat…), the account connects and Settings
        shows "Some Google tools (Drive, Docs, Chat…) weren't shared. Reconnect to add them."
      - If they untick Gmail, Wisp refuses the sign-in and explains why.
   5. "Google connected. You can close this tab and return to Wisp."
3. Caveats:
   - An unverified app in production has a lifetime cap of **100 new users** for sensitive or restricted
     scopes. That's plenty for a small tester group.
   - Google may restrict an unverified app that asks for **restricted** scopes such as `gmail.readonly`. If the
     "Go to Wisp (unsafe)" link is missing for an account, go back to the test-user path *(unverified;
     behavior has varied)*.
   - Production tokens don't have the 7-day expiry.
   - Prefer personal `@gmail.com` accounts. A school or company Google Workspace admin can block unverified
     third-party apps entirely. The tester then sees an admin-policy error, and nothing in Wisp can fix it.

## 7. Clients: create the Desktop OAuth client

1. Go to **Google Auth Platform ▸ Clients** and click **+ Create client**.
2. Fill in:
   - **Application type**: **Desktop app**.
   - **Name**: `Wisp Mac`.
   - Click **Create**.
   - Desktop clients need no redirect URI. Wisp listens on `http://127.0.0.1:<random port>` for the sign-in
     (loopback with PKCE).
3. A dialog shows the **Client ID** (`…apps.googleusercontent.com`) and the **Client secret**.
   - Copy both now, or click **Download JSON**.
   - Newer consoles may not show the secret again after this dialog *(unverified)*. If you lose it, open the
     client and add a new secret, or delete the client and create another one.
   - For a Desktop app the "secret" isn't really confidential, but don't post it publicly.

## 8. Paste it into Wisp and connect your own account

1. Open Wisp's Settings (**⌘,** or the notch gear ▸ **All settings**; it opens in the Wisp window) ▸ **Google**.
2. Open **Advanced ▸ Use your own OAuth client**:
   - Paste the **OAuth client ID**.
   - Paste the **Client secret**.
3. Click **Connect Google account** (above Advanced).
   - Sign in with **your** Google account in the browser and allow the permissions.
   - This also saves the client to `~/.moe/gmail.json` (mode 0600).
4. Within about a minute your account shows as connected. Your Google events for the next 7 days
   appear in Day and Brief.

From then on the notch **Connect Google** button reuses the saved client. If there is no saved client and
the build has no built-in one, Settings says "This copy of Wisp has no built-in Google sign-in. Add your own
OAuth client under Advanced."

## 9. When a tester connects

1. The tester taps **Connect Google** in the notch, and their browser opens Google sign-in.
2. The notch shows, in order:
   - **Waiting for sign-in in your browser…**
   - **Reading mail & calendar…**: about 50 recent message bodies, recent headers, and the next 7 days of
     events.
   - **Building your to-dos…**
   - **Connected as tester@gmail.com**
3. Their cards and to-dos fill in under Brief as the proactive engine finishes.
4. To remove a tester's data, tap **Disconnect & erase** next to their address, then **Erase**. Wisp then:
   - Revokes the token with Google.
   - Removes the account from `~/.moe/gmail.json`.
   - Deletes the account's cached mail (headers, history, bodies, identity), its Google Calendar events and
     the inbox overviews.
   - Deletes its proactive cards, to-dos and summaries.

   Your own account and your OAuth client stay.
5. Optional extra cleanup: the tester can also remove Wisp at <https://myaccount.google.com/permissions>.

## What "Disconnect & erase" does and does not reach

The notch and Settings report each part separately. They only say "erased" for parts the backend
confirmed. Anything else is listed as "Not confirmed erased".

**Erased:**
- the OAuth token, revoked with Google and removed from `~/.moe/gmail.json`
- the account's saved sign-in for Wisp's Google tools (a file under `~/.moe/google_workspace/credentials`)
- mail headers, history, bodies and identity entries, in memory and in `~/.moe/cache`
- the account's Google Calendar events (`gcal:<email>:…` rows) and their side rows
- schedule versions of those rows
- queued assistant events (notifications, nudges, actions) whose payload names those rows or the
  account's address
- anything the routine learner (behavior.db) recorded from those events
  - Wisp no longer learns from Google Calendar rows at all.
- the inbox overview and summary written from the combined inbox, including a rewrite that was still in
  progress
- the account's proactive cards, to-dos and thread summaries

**Not erased:**
- **Chat history.** If anyone asked Ask about the tester's mail or calendar, those turns stay in the chat
  history. Delete those chats separately.
- **Debug and corpus logs.** Debug Mode recordings, corpus exports and service logs may quote tool output
  from the tester's data. Keep Debug Mode off while testing with real mail.
- **Saved memory facts.** These exist only if the tester's data led Wisp to remember something.
  Check Memory ▸ Facts afterwards.
- **Mail.app.** Its own copy, if the tester's account was ever added there.
- **The Google side.** Wisp's access is revoked. The tester can confirm at
  <https://myaccount.google.com/permissions>.
