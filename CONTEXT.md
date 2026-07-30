# Failure Memory

Failure Memory preserves durable lessons from real agent failures and makes them safely
available to every trusted local agent harness.

## Language

**Global Personal Memory**:
The single owner-private failure-memory corpus for one local user on one device.
_Avoid_: Workspace memory, Codex memory, harness-local memory

**Origin Context**:
Provenance describing the harness, workspace, session, and source store that produced an
event; it never limits retrieval visibility.
_Avoid_: Retrieval scope, tenant

**Source Store**:
A prior or external Failure Memory ledger whose immutable records can be copied into Global
Personal Memory.
_Avoid_: Replica, shard

**Derived Index**:
A rebuildable lexical, vector, or clustering projection of the authoritative lessons.
_Avoid_: Memory database, source of truth

**Generalization Proposal**:
A reviewable suggestion that multiple lessons support a broader lesson while preserving
every source incident and lesson.
_Avoid_: Automatic merge, canonical truth

**Generalization Review**:
The required second-tier decision after a real failure is accepted: reuse an exact lesson,
reuse or broaden a reviewed related lesson, create a distinct lesson, or defer recording.
_Avoid_: Duplicate check, automatic deduplication

**Harness Projection**:
A host-specific manifest, skill, MCP, and hook view over the shared core and Global Personal
Memory; it never owns a separate memory store.
_Avoid_: Harness database, Codex store, Copilot store

**Proposed Caution**:
A recalled lesson offered for validation against the current task, not verified policy.
_Avoid_: Rule, mandate
