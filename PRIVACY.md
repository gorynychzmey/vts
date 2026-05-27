# Privacy policy

vts is a self-hosted service. This document describes what each
deployment **does** with your data. The text below is a template
shipped with the source code; the live `/privacy` page on any given
deployment renders it with that deployment's operator details (if the
operator chose to publish them).

If you are a user wondering about your data, look at the `/privacy`
page of the instance you are actually using — that page reflects its
operator's contact and instance name. If you are an operator
publishing your own instance, set `VTS_OPERATOR_NAME`,
`VTS_OPERATOR_CONTACT`, and `VTS_OPERATOR_INSTANCE_NAME` (see
`docs/ARCHITECTURE.md`) and review whether the retention and access
defaults below still describe your setup honestly.

## Who runs this instance

Operator name and contact are filled in at runtime from the instance
configuration. On the rendered `/privacy` page you will see the
specific name and a contact channel (typically email). If the operator
hasn't set them, the page will note that and you should ask whoever
gave you the link.

This codebase is built and run as a personal / small-group hobby
service. Access on any given deployment is limited to whatever
allow-list (Google OAuth email or domain rules) the operator has
configured.

## What data is collected

1. **Identity from Google OAuth.** When you log in, Google sends the
   instance your email address (the `email` claim from the OpenID
   Connect token). The instance uses it to look you up in its own
   user table and to check the allow-list. Nothing else from your
   Google profile is requested or stored.

2. **Content you submit.** Each task you create stores:
   - the original source (URL you submitted, or the file you uploaded
     with its original filename);
   - the audio/video file itself (uploads are kept on disk; downloads
     are fetched via `yt-dlp` and stored under the same artifact
     directory);
   - intermediate processing artifacts (segmented WAVs, raw ASR
     output);
   - the resulting transcript and Markdown summary.

3. **Operational metadata.** Pipeline stage timings, ASR/LLM token
   counts, retry counts — written to a local JSONL metrics file. Used
   only to debug pipeline performance.

4. **Server-side sessions.** A signed cookie holding an opaque session
   id; the corresponding `{sid → email}` record lives in Redis and is
   deleted on logout or after the configured lifetime
   (default 30 days).

5. **API tokens.** Personal API tokens you generate through the UI are
   stored as a SHA-256 hash plus a short prefix for display.

## What is not collected

- No analytics, no trackers, no advertising IDs.
- No third-party cookies.
- No fingerprinting beyond the IP address that any web server sees in
  its standard access log.
- No content beyond what you explicitly submit. The instance does not
  scrape your browser history or other tabs.

## Where data goes

- **Google** sees the OAuth login (their normal sign-in flow). The
  instance does not push anything back to Google.
- **YouTube / source sites.** When you submit a URL, `yt-dlp` fetches
  it from the instance's server. The remote site sees a normal
  download request from the instance's IP, not from yours.
- **The LLM and ASR backends.** Transcription and summarisation run
  against models the operator chose to host. The default deployment
  recipe keeps them on the same machine as the rest of the service;
  individual operators may have configured external API providers
  (OpenAI, Anthropic, etc.) — in that case your content reaches those
  providers. The `/privacy` page should be updated if so.
- **Nothing else by default.** No third parties, no analytics, no
  outbound exports.

## Retention

| Data | Retention |
|------|-----------|
| Uploaded / downloaded media files | Removed automatically after `media_ttl_hours` (default 72h) once processing completes |
| Transcripts and summaries | Kept until you delete or archive the task |
| Archived tasks | Transcript + summary kept; media + intermediate artifacts removed |
| Session records in Redis | Deleted on logout or after the cookie lifetime expires |
| API tokens | Kept until you revoke them; only the hash is stored |
| Webserver access logs | Standard journald rotation on the host (operator's discretion) |

You can request deletion of your entire user account and all
associated data by contacting the operator (see the rendered
`/privacy` page on the actual instance for the contact channel).

## Security

- All traffic is TLS-encrypted.
- API tokens are stored as SHA-256 hashes only — a database dump does
  not yield working tokens.
- Sessions can be revoked server-side (`POST /auth/logout` deletes the
  Redis record).
- Source code is open at https://github.com/gorynychzmey/vts — the
  data handling described here is what the code actually does. Report
  security issues per [SECURITY.md](SECURITY.md).

## Children

Not intended for users under 16.

## Changes

If this policy changes materially, the change will appear in the
release notes of the affected version of vts. Each instance picks up
the new text on the next deploy.

## Contact

The rendered `/privacy` page on the instance shows the operator's
contact channel. If you are reading this text on GitHub, you are
looking at the template — go to the actual instance's `/privacy` for
the live operator details.
