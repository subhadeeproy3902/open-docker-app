# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 1.x | ✅ |

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's
[private vulnerability reporting](https://github.com/subhadeeproy3902/open-docker-app/security/advisories/new) or e-mail
**subha9.5roy350@gmail.com** with the details and steps to reproduce. You will get an answer within a few days.

## Security model in short

- Berth talks only to the Docker / Podman socket you point it at. Access to that socket is equivalent to root on the
  machine, which is a property of Docker itself; Berth adds no network listener of its own.
- No telemetry, no account. Network access happens only for your engine, Docker Hub search and pulls, registry sign-in
  (handled by the `docker` CLI and its credential store) and the Trivy vulnerability database.
- Engine start/stop and `daemon.json` edits go through polkit (`pkexec`), so your password is never seen by Berth.
- Container environment variables are hidden in the UI until you choose to reveal them.
