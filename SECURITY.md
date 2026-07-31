# Security Policy

## Reporting a vulnerability

Please use GitHub's private vulnerability-reporting feature. Do not publish suspected
vulnerabilities or attach a real memory database to an issue.

Include the affected version, impact, attacker prerequisites, a synthetic reproduction,
and any suggested mitigation. Do not test systems or data you do not own or have
permission to assess.

## Local data

Failure Memory stores incident and lesson text locally with owner-only permissions.
Agents are instructed not to submit raw prompts, transcripts, secrets, personal data, or
unnecessary paths. Recognized credentials are redacted as defense in depth, not as a
substitute for minimizing input.

The event log is append-only at the database layer. Retrieval indexes and optional model
files are derived adapters and may be rebuilt.

## Supported versions

Security fixes are applied to the latest release until multiple maintained release lines
are announced.
