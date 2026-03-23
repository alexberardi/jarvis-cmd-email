# jarvis-cmd-email

Email management for [Jarvis](https://github.com/alexberardi/jarvis-node-setup). Supports Gmail (OAuth) and IMAP/SMTP.

## Install

```bash
python scripts/command_store.py install --url https://github.com/alexberardi/jarvis-cmd-email
```

## Voice Commands

- "Check my email"
- "Read email 3"
- "Search for receipts"
- "Send an email to john@example.com saying I'll be late"
- "Reply to the first email saying thanks"
- "Archive email 2"
- "Delete the third email"
- "Star the first email"

## Components

| Component | Type | Description |
|-----------|------|-------------|
| email | command | Full email management |
| email_alerts | agent | VIP alerts, urgent detection, daily digest |

## Providers

| Provider | Auth | Setup |
|----------|------|-------|
| Gmail | OAuth2 | Authenticate via mobile app |
| IMAP | Username/Password | Set IMAP_* secrets |

## Structure

```
commands/email/command.py            # Voice command
agents/email_alerts/agent.py         # Background alert agent
lib/google_gmail_service.py          # Gmail API client
lib/imap_email_service.py            # IMAP/SMTP client
lib/email_service_factory.py         # Provider factory
lib/email_message.py                 # EmailMessage dataclass
```