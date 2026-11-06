([posted on zabbix forum](https://www.zabbix.com/forum/zabbix-suggestions-and-feedback/518348-zabbix-digest-a-daily-email-that-stays-silent-until-something-is-really-missed#post518348))

Zabbix Digest: a daily email that stays silent until something is really missed

I'd like to share a small open source project: Zabbix Digest.

The problem

Zabbix alerts are great, until one of them gets lost in a busy inbox. A problem that fired once and is still active a day later is easy to miss.

The answer

Zabbix Digest sends you a daily email with the active problems from a Zabbix dashboard (your own or a shared one), but stays silent when there are none.

The idea works best when Zabbix Digest is silent on most days - but if a digest email arrives, then you probably missed something.
Keep the dashboard empty when things are fine: for long-standing problems, open a ticket and acknowledge or suppress the problem in Zabbix.

Features

Per-user access with the user's own Zabbix API token, so everyone sees only what Zabbix allows them to see.

Self-service web UI: log in with an API token or an email link, manage subscriptions, preview the digest, send a test message.

Optional per-subscription filters that narrow the Problems widget filter: minimal severity, hide acknowledged, hide suppressed.

Error notifications if a digest can't be prepared (invalid token, missing widget and so on).

Optional weekly or monthly reminder, sent only when no digest went out for that period, so you know the service is alive.

Docker Compose setup, including a demo profile with a Zabbix API stub and a test mailbox, so you can try it without touching your Zabbix.

Email out of the box; other delivery channels through external adapters.

Built for Zabbix 7.0.

Repository: https://github.com/nvsmirnov/zabbix-digest

(c) 2026 Nikita Smirnov. MIT License.
