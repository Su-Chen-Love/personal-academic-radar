# Security policy

Do not report private research profiles, paper feedback, local paths, database
contents, or credentials in a public issue. Use a private GitHub security
advisory for vulnerabilities.

The repository is designed to be public; the state directory is not. Never
commit `config.toml`, SQLite files, agent queues or results, digests, logs,
backups, `.env` files, or credentials.

The web application binds to loopback by default and has no public-user
authentication. Treat `--allow-remote` as an expert-only escape hatch, not as a
deployment recipe.

