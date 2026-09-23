# Project Instructions

## Text-to-Speech Notifications

Use `~/.codex/notify-user.ts` to notify the user when their attention is helpful.

Always run notification commands asynchronously in fire-and-forget style so the agent does not wait for audio playback:

```sh
~/.codex/notify-user.ts --text "message to speak" >/tmp/codex-notify-user.log 2>&1 &
```

Keep the message short, mention the project by name, and write it like a developer updating a manager.
