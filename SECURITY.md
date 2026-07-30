# Security Policy

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue.

Use the repository host's private vulnerability-reporting feature when available. If the
host does not provide one, contact a maintainer privately through the maintainer profile
before sharing technical details. Include:

- the affected revision and component;
- impact and attacker prerequisites;
- a minimal reproduction using synthetic data;
- whether credentials or personal data may have been exposed;
- any suggested mitigation.

Do not test against systems, accounts, or data you do not own or have explicit permission
to assess.

## Supported versions

Until the first stable release, security fixes are applied to the latest development
revision. A version support table will be added when multiple maintained release lines
exist.

## Sensitive local data

Failure Memory databases and backups may contain incident and lesson text. Keep them
owner-private, do not attach them to issues, and sanitize diagnostics before sharing.
Public CLI and MCP health responses intentionally omit absolute database paths.
