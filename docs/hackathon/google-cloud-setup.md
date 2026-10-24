# Google Cloud setup for "Connect Google" (hackathon)

Wisp does not ship a Google OAuth client. You create one in your own Google Cloud project, once, and
paste it into Wisp. After that anyone can tap **Connect Google** in the notch and sign in with their own
account. Allow about 15 minutes.

Wisp asks for exactly two read-only scopes:

- `https://www.googleapis.com/auth/gmail.readonly` (required; Google calls it a **restricted** scope)
- `https://www.googleapis.com/auth/calendar.readonly` (optional; a **sensitive** scope)

Google's console moves things around often. The names below match the "Google Auth Platform" layout
(Overview / Branding / Audience / Clients / Data Access / Verification Center). Where I'm unsure of a
label it says *(unverified)*.

---

## 1. Create a project

1. Open <https://console.cloud.google.com/> and sign in with the account that will own the app (yours).
2. Click the project picker at the top left, next to "Google Cloud", then **New project**.
3. Name it `Wisp`. Leave Organization as is, or "No organization" for a personal account. Click **Create**.
4. Make sure the picker now shows **Wisp** before you go on.

## 2. Enable the two APIs

1. Go to **☰ ▸ APIs & Services ▸ Library**, or open <https://console.cloud.google.com/apis/library>.
2. Search **Gmail API**, open it, and click **Enable**.
3. Go back to the Library. Search **Google Calendar API**, open it, and click **Enable**.

If you skip the Calendar API, mail still works. Wisp then shows "Turn on the Google Calendar API in your
Google Cloud project…" for each account until you enable it. It can take a minute to take effect.

## 3. Google Auth Platform: branding, audience, contact

1. Go to **☰ ▸ Google Auth Platform ▸ Branding**. **APIs & Services ▸ OAuth consent screen** now
   redirects there.
2. If you see "Google Auth Platform not configured yet", click **Get started**:
   - **App Information**: App name `Wisp`. User support email: your email. Click **Next**.
   - **Audience**: choose **External**. Click **Next**.
     - "Internal" only works for accounts in your own Workspace organization, so judges' Gmail accounts can't use it.
   - **Contact Information**: your email. Click **Next**.
   - **Finish**: tick the box agreeing to the Google API Services User Data Policy. Click **Continue**, then **Create**.
3. Optional, under **Branding**: add a logo and an app home page.
   - Leave the logo empty for now. Uploading one can push the app into brand verification.

## 4. Data Access: add the scopes

1. Go to **Google Auth Platform ▸ Data Access**.
2. Click **Add or remove scopes**.
3. In the panel, filter for and tick:
   - `.../auth/gmail.readonly` (Gmail API, "View your email messages and settings"), listed under
     **restricted** scopes.
   - `.../auth/calendar.readonly` (Google Calendar API, "See and download any calendar you can access…"),
     listed under **sensitive** scopes.
   - If a scope doesn't appear, use **Manually add scopes** at the bottom of the panel. Paste both URLs
     above, one per line, then click **Add to table**.
4. Click **Update**, then **Save** on the Data Access page.

You don't need to submit for verification for the demo. Section 6 covers what that means.

## 5. Audience: test users (recommended path)

1. Go to **Google Auth Platform ▸ Audience**. Publishing status should read **Testing**.
2. Under **Test users**, click **+ Add users**. Enter your own Gmail address and each judge's Google address
   (comma separated). Click **Save**.
3. Judges you add can sign in with no warning screen. Anyone not on the list gets
   **"Access blocked: Wisp has not completed the Google verification process" (Error 403: access_denied)**.

Testing-mode limits:

- **Test users are capped at 100** per project. Removing a user may not give the slot back *(unverified)*.
- **Tokens last 7 days.** Google issues refresh tokens that expire after 7 days when an External app is in
  Testing and asks for anything beyond name/email/profile, which Wisp does. Wisp then shows "Google sign-in
  expired or was revoked; reconnect this account in Settings ▸ Google". For a one-day demo that's fine.
  Reconnect your own account the morning of the demo so it doesn't lapse mid-demo.
- Tokens issued while the app was in Testing keep that 7-day expiry even after you publish *(unverified)*.
  Reconnect after you change the status.

## 6. Fallback if you can't collect judges' emails in advance: publish unverified

1. Go to **Google Auth Platform ▸ Audience** and click **Publish app**. Confirm.
   - The status changes to **In production**.
   - The Verification Center shows the app needs verification. Don't submit for the demo.
2. What a judge sees when they tap Connect Google:
   1. The Google account chooser.
   2. **"Google hasn't verified this app"**: the app is asking for sensitive info.
   3. The judge clicks **Advanced**, then **Go to Wisp (unsafe)**.
   4. The consent screen, with checkboxes for "View your email messages and settings" and "See … your
      calendars". They should tick both.
      - If they untick Calendar, Wisp still connects mail and shows "Mail only (calendar not shared)".
      - If they untick Gmail, Wisp refuses the sign-in and explains why.
   5. "Google connected. You can close this tab and return to Wisp."
3. Caveats:
   - An unverified app in production has a lifetime cap of **100 new users** for sensitive or restricted
     scopes. That's plenty for a hackathon.
   - Google may restrict an unverified app that asks for **restricted** scopes such as `gmail.readonly`. If the
     "Go to Wisp (unsafe)" link is missing for an account, go back to the test-user path *(unverified;
     behavior has varied)*.
   - Production tokens don't have the 7-day expiry.
   - Prefer personal `@gmail.com` accounts. A school or company Google Workspace admin can block unverified
     third-party apps entirely. The judge then sees an admin-policy error, and nothing in Wisp can fix it.

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

1. Open Wisp's Settings window (**⌘,** or the notch gear ▸ **All settings**) ▸ **Google**.
2. Under **Your Google OAuth client**:
   - Paste the **OAuth client ID**.
   - Paste the **Client secret**.
3. Under **Email and calendar with Mail closed**, click **Connect or reconnect an email account**.
   - Sign in with **your** Google account in the browser and allow both permissions.
   - This also saves the client to `~/.moe/gmail.json` (mode 0600).
4. Within about a minute your account shows "Mail & calendar". Your Google events for the next 7 days
   appear in Day and Brief.

From then on the notch **Connect Google** button reuses the saved client. If no client is saved, the notch
shows "Add your Google OAuth client in Settings ▸ Google first."

## 9. During the demo

1. The judge taps **Connect Google** in the notch, and their browser opens Google sign-in.
2. The notch shows, in order:
   - **Waiting for sign-in in your browser…**
   - **Reading mail & calendar…**: about 50 recent message bodies, recent headers, and the next 7 days of
     events.
   - **Building your to-dos…**
   - **Connected as judge@gmail.com**
3. Their cards and to-dos fill in under Brief as the proactive engine finishes.
4. After the demo, tap **Disconnect & erase** next to their address, then **Erase**. Wisp then:
   - Revokes the token with Google.
   - Removes the account from `~/.moe/gmail.json`.
   - Deletes the account's cached mail (headers, history, bodies, identity), its Google Calendar events and
     the inbox overviews.
   - Deletes its proactive cards, to-dos and summaries.

   Your own account and your OAuth client stay.
5. Optional extra cleanup: the judge can also remove Wisp at <https://myaccount.google.com/permissions>.
