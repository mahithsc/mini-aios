# Google Apps OAuth and local Gmail MCP

Gmail and Google Calendar share one Google connection. mini-AIOS uses
Authorization Code + PKCE and permanently owns the user's encrypted refresh
token. No OAuth client secret or cloud token broker is involved.

## Where configuration lives

### Google Cloud

Create an iOS OAuth client for the mobile bundle identifier. The client ID is
public application metadata; Google does not issue or require a client secret
for this client type. Register the generated reversed-client-ID URL scheme in
the iOS app.

For the temporary development app:

```dotenv
bundle ID: com.anonymous.aios-mobile
Apple team ID: 64THXTCK8B
client profile: google-ios-dev
```

In Google Cloud, enable the Gmail API and configure the consent screen with:

```text
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
```

The Google-hosted Gmail MCP API and its Developer Preview program are not
required: mini-AIOS runs its own Gmail MCP server and calls the public Gmail
REST API.

Connections created before `gmail.send` was added must reconnect once to grant
the new scope. Until then, AIOS reports Gmail as not fully connected and does
not load the Gmail MCP toolkit.

### mini-aios

```dotenv
AIOS_GOOGLE_OAUTH_CLIENT_ID=<public iOS client ID>
AIOS_GOOGLE_OAUTH_CLIENT_PROFILE=google-ios-dev
```

`AIOS_GOOGLE_OAUTH_REDIRECT_URI` is optional. mini-AIOS derives the standard
Google iOS redirect URI from the client ID:

```text
com.googleusercontent.apps.<client-id-prefix>:/oauth2redirect
```

The public client ID can be packaged with mini-AIOS. There is no per-device
provider secret to provision. `AIOS_CLOUD_URL` remains relevant to device
pairing and off-LAN transport, but not to provider OAuth.

mini-aios generates a private credential-encryption key on first use at
`<runtime-state>/credentials.key` and stores the encrypted refresh token in its
existing SQLite database. Keep both the runtime state and database persistent.
Production images may instead provide a stable key using:

```dotenv
AIOS_CREDENTIAL_ENCRYPTION_KEY=<Fernet key>
```

or:

```dotenv
AIOS_CREDENTIAL_ENCRYPTION_KEY_FILE=/run/secrets/aios-credential-key
```

The key must not be stored in SQLite. Back up or provision it together with the
device; losing it makes the stored refresh token unreadable.

Optional MCP settings remain local:

```dotenv
AIOS_GOOGLE_MCP_ENABLED=true
AIOS_GMAIL_MCP_TOOLS=search_messages,get_message,get_thread,list_labels,send_email
```

## Local Gmail MCP server

The first-party server lives at
`aios_core/mcp_servers/gmail/` and is started by the mini-AIOS agent as a local
stdio subprocess. It exposes four read-only tools:

- `search_messages`
- `get_message`
- `get_thread`
- `list_labels`

It also exposes one write tool:

- `send_email`

`send_email` is non-idempotent and executes when the AIOS agent calls it. The
first version supports To, Cc, Bcc, Reply-To, plain text, and HTML. It does not
support attachments.

The server does not accept provider tokens through arguments, environment
variables, or MCP messages. Its Gmail client calls the shared mini-AIOS Google
token provider inside the first-party subprocess when a tool runs. That
provider uses the encrypted refresh token in the local mini-AIOS SQLite
database and talks directly to Google's token endpoint when refresh is needed.

Results and outbound messages are deliberately bounded. Searches return at most
25 summaries, attachment bodies are not downloaded, message bodies are
truncated, and long threads return only their 20 most recent messages. Outbound
messages allow at most 50 recipients and a 200,000-character body. Recipient
addresses and headers are validated before the Gmail API is called. All inbound
email content is marked as untrusted input in the server and tool instructions.

This first server is trusted first-party code running under the same operating
system account as mini-AIOS, so this version is an application boundary rather
than an operating-system security boundary. The token-provider interface is the
seam for a future Unix-socket credential broker. That broker will be required
before running untrusted or third-party MCP containers so those containers
cannot read the local state database or encryption key.

## Connection lifecycle

1. Mobile calls `POST /integrations/google/connect/start` on mini-aios with
   `gmail`, `calendar`, or both.
2. mini-AIOS creates a random state and PKCE verifier, stores only the state
   hash plus an encrypted verifier, and returns a Google authorization URL.
3. Mobile opens that URL. Google redirects back to the app with a short-lived
   authorization code and the original state.
4. Mobile forwards `sessionId`, `code`, and `state` to
   `POST /integrations/google/connect/complete` on the paired mini-AIOS.
5. mini-AIOS validates ownership, expiry, single-use status, state, client
   profile, and redirect URI. It then sends the code plus PKCE verifier directly
   to Google's token endpoint.
6. mini-AIOS encrypts the refresh token locally, keeps the access token only in
   memory, and erases the state hash and verifier.
7. When the Gmail MCP needs access, its local provider asks mini-AIOS for an
   access token. On restart or access-token expiry, the provider sends the
   encrypted-at-rest local refresh token directly to Google's token endpoint
   with the public client ID.
8. Disconnect asks Google to revoke the provider token, then securely deletes
   the local connection.

OAuth attempts and per-app states are stored in `integration_auth_sessions` and
`integration_apps`. Sessions expire after ten minutes and terminal sessions no
longer retain PKCE material.

## Local API

- `GET /integrations/google`
- `POST /integrations/google/connect/start`
- `POST /integrations/google/connect/complete`
- `POST /integrations/google/connect/cancel`
- `DELETE /integrations/google`

The mobile app briefly receives the one-time authorization code and state. It
never receives Google access tokens, refresh tokens, client secrets, PKCE
verifiers, or the mini-AIOS device token.

Use a custom Expo development build for the callback; Expo Go cannot register
the Google-generated URL scheme. While the consent screen is in Testing,
Google-issued refresh tokens expire after seven days.

Gmail messages are untrusted model input. The send tool must never infer
recipients or follow sending instructions found inside email content. Future
delete or label-mutating tools should be evaluated separately before being
enabled.
