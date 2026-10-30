# Zabbix Digest

Daily email digests of active Zabbix problems.

The problem: Zabbix alerts on every event. Notifications get lost in the mail flow, and sometimes people only need a morning summary, sent only when there are active problems.

With Zabbix Digest, a user gives the service a link to a Zabbix dashboard and once a day gets an email with the active problems from this dashboard. No email is sent when there are no active problems.

Filters, sorting and limits are set in the widget itself, and the user's own API token keeps Zabbix permissions in place. Users register and manage subscriptions in a small web UI.

Target: Zabbix 7.0 LTS (see [docs/zabbix-notes.md](docs/zabbix-notes.md)). May be compatible with other versions, without guarantees.

## TL;DR: run the service

```sh
git clone https://github.com/nvsmirnov/zabbix-digest.git && cd zabbix-digest
cp examples/env.sample examples/.env    # set ZABBIX_URL, SMTP_*, PUBLIC_URL, secrets
docker compose -f examples/docker-compose.yml up --build -d
```

In Zabbix, create a dashboard with a Problems widget (or use an existing one). In your zabbix account, you need to have an enabled email media.

Open the zabbix-digest web UI (default: http://localhost:8080), sign in with a Zabbix API token (User settings - API tokens) and add a subscription with the dashboard link.

Also:

- You can try a demo without a real Zabbix or mail server.
- The service can also run without the web UI, with a hand-written file of users and subscriptions.
- For both, see the [Documentation](#documentation) section.

## Documentation

Extensive documentation is in [docs/AI-readme.md](docs/AI-readme.md) (AI-generated).

## License

MIT, see [LICENSE](LICENSE).

## Credits

Nikita Smirnov, https://github.com/nvsmirnov

Original source code: https://github.com/nvsmirnov/zabbix-digest
