# Code review — October 16, 2026

## Scope and status

Six specialist reviewers and a coordinating reviewer examined the current working tree, including uncommitted and untracked changes. The review covered the async agent runtime, authorization rules, admin API and security boundaries, shared Python integrations, knowledge and storage packages, and web/framework runtime contracts.

The Go code ingestion project was excluded: `apps/forge-codegraph-worker` and `packages/go` were not reviewed. Python codegraph readers and the Python MCP service were included.

**17 actionable findings: seven P1 and ten P2. All are remediated in the working tree.** Work began with all seven P1 findings. Each finding's remediation entry records its implementation and regression validation. Locations below refer to the original review and may move as code changes.

**Remediation progress: 17 of 17 findings fixed and tested; all seven P1 findings complete.** Existing user changes were preserved. The Go ingestion project remains excluded and unchanged.

- **P1:** High-priority security or operational defect to address promptly.
- **P2:** Actionable correctness, reliability, isolation, or data-loss defect.

## Findings

### 1. [P1] Cached agent dependencies can bypass current permissions

**Remediation — Fixed and tested.** Resolution captures a deep-copied transitive dependency graph, includes dependency contents/resource stamps in the cache key, and preserves that exact graph through SSE/A2A execution. Authorization and chat pages check current permissions on the captured dependency IDs and reject missing dependencies. Validation: runtime suite **221 passed**; admin authorization suite **108 passed**, including publication races, captured graph/page checks, current permission changes, and deleted dependencies; changed-file Ruff/formatting and executor/A2A typing passed.

**Location:** [source.py:159](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/apps/forge-admin-api/src/forge_admin/chat_agents/source.py:159)

The hosted agent cache key includes the parent agent's version and direct skill stamp, but not its saved-agent dependency versions. Authorization walks the latest published dependencies, while a cached runner retains previously compiled dependencies.

If a parent was built with child version 1 containing a restricted grandchild, and child version 2 removes that grandchild, authorization can admit a caller who cannot use the restricted agent while the cached parent still contains it.

**Evidence:** Reproduced with the real `MongoAgentSource`, `AgentExecutor`, and `authorize_use`, backed by an isolated versioned store. After publishing the child update, an anonymous caller was authorized, the same parent runner was reused, and the restricted descendant remained in its executable graph. A second reviewer independently confirmed the missing enforcement path.

**Recommended fix:** Include transitive dependency versions in cache invalidation and authorize the dependency graph actually executed. A permission check against a different graph is insufficient.

### 2. [P1] Conversation audits expose another organization's state

**Remediation — Fixed and tested.** Scoped session traces now reconstruct snapshots only from authorized event deltas; unknown initial state and hidden turns are excluded, including empty and truncated results. Removed the audit route's ineffective snapshot substitution. Validation: trace regressions **10 passed**; admin conversation tests **15 passed**; changed-file Ruff checks passed.

**Location:** [audit.py:366](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/apps/forge-admin-api/src/forge_admin/conversations/audit.py:366)

Organization-scoped audits filter events, but the trace helper builds `state_before` from earlier events belonging to other organizations. `state_after` inherits those values, and replacing `state` with `state_after` does not sanitize the response. When no allowed events exist, the helper returns the current session state.

**Evidence:** Reproduced with SQLite and the real ADK session service. A preceding excluded turn supplied a confidential title and private page context. The response contained only the authorized organization's event, but all three state snapshots exposed the excluded values.

**Recommended fix:** Reconstruct partial audit state solely from authorized data, or omit snapshots whose provenance cannot be established. Cover state isolation as well as event filtering.

### 3. [P1] HTTP redirects leak configured credentials to other hosts

**Remediation — Fixed and tested.** Origin-changing redirects strip static headers except a small representation-header allowlist; changes of hostname, scheme, or port are covered. The equivalent workflow HTTP path is protected too. Validation for findings 3/4/6: runtime suite **221 passed**, workflow HTTP/support/graph/GitHub tests **135 passed**; changed-source Ruff and typing checks passed.

**Location:** [tools.py:278](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/agent-runtime/src/forge_agent_runtime/tools.py:278)

Every redirect reuses the HTTP tool's static headers. Credential-provider scoping protects provider-generated credentials, but configured headers such as `Authorization` and `X-API-Key` are forwarded to a different destination.

**Evidence:** A mock HTTP transport returned a redirect from an API endpoint to another host. Both configured credential headers arrived unchanged at the second host.

**Recommended fix:** Bind sensitive headers to their intended origin and strip them when the destination changes. Apply credential handling consistently to configured headers and provider-generated credentials.

### 4. [P1] DNS rebinding bypasses private-network protection

**Remediation — Fixed and tested.** HTTP egress resolves and validates addresses at TCP connection time and connects only to those numeric addresses, preserving the original Host/TLS identity. Connection pools are separated by network policy. Tools, specification loading, OpenAPI, and workflow requests use the guard. Regression coverage exercises DNS changes, mixed/private/mapped addresses, Host/SNI, and policy isolation. Proxy routes and injected unguarded network transports now fail closed; the compatibility requirement is documented in the runtime README. Validation: the **221 runtime + 135 workflow** tests reported above passed; dependency lock validation passed offline.

**Location:** [tools.py:143](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/agent-runtime/src/forge_agent_runtime/tools.py:143)

`check_url()` validates one DNS resolution but returns the original hostname. The HTTP transport resolves that hostname again when connecting. A hostname that changes from a public address to a private address between those resolutions can bypass the private-network restriction.

**Evidence:** A controlled resolver and HTTP transport backend returned a public address during validation and loopback during connection resolution. The guarded request completed without rejecting the private destination. This was an isolated transport reproduction, not a live attack against a private service; the execution sandbox blocked a separate local-listener test.

**Recommended fix:** Bind connections to validated addresses while preserving the intended host and TLS identity, and apply the same policy to every redirect and connection attempt.

### 5. [P1] Updating an MCP endpoint can send replacement credentials to the old server

**Remediation — Fixed and tested.** MCP credential callbacks validate the pinned endpoint, timeout, and organization under lock before decrypting or refreshing credentials. Endpoint/timeout cache stamps rebuild hosted toolsets after connection changes while allowing credential rotation at the same endpoint. Validation: MCP package **55 passed** and admin MCP tests **35 passed**, including stale-endpoint, ownership-change, and cache-invalidation regressions.

**Location:** [toolset.py:67](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/mcp-servers/src/forge_mcp_servers/toolset.py:67)

An MCP toolset captures the server URL when built, while its header callback reads current credentials. Updating an MCP server's URL and secret leaves existing cached agents connecting to the old server with the replacement server's secret. The update path does not invalidate those runners.

**Evidence:** Constructed a real `McpToolset`, changed its stored endpoint and sealed bearer credential, and observed the original connection URL paired with the replacement credential. No network request was needed.

**Recommended fix:** Reject credentials when the current endpoint differs from the pinned endpoint, and rebuild the complete toolset when connection settings change.

### 6. [P1] Template regex evaluation can freeze the runtime

**Remediation — Fixed and tested.** Instructions and HTTP templates use bounded regular expressions and evaluate on a dedicated thread pool. Regressions bound pathological matches in a subprocess and verify concurrent event-loop progress. Validation: included in the **221 runtime + 135 workflow** passing tests above; changed-source typing and Ruff checks passed.

**Location:** [templating.py:76](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/agent-runtime/src/forge_agent_runtime/templating.py:76)

Chat templates instantiate JSONata with its default Python regex engine, bypassing the bounded regex engine used by workflow expressions. Evaluator timeouts cannot interrupt a regex operation already running. Instructions render synchronously, so pathological matching can block other requests in the process.

**Evidence:** An isolated template evaluation using `$match(state.text, /^(a+)+$/)` on a nonmatching repeated-character input exceeded a 25 ms evaluation budget and remained running after three seconds. The subprocess was terminated to bound the reproduction.

**Recommended fix:** Use a bounded regex engine for chat instruction and HTTP template expressions, and avoid blocking the event loop with expression evaluation. Moving unbounded Python regex work to a thread alone does not remove its interpreter-lock risk.

### 7. [P1] Healthy document jobs can be aborted after two minutes

**Remediation — Fixed and tested.** Document jobs disable the unmaintained SAQ heartbeat and retain their 1,800-second execution timeout and retries. A regression checks the real `Job.stuck` predicate at 121 and 1,801 seconds. Validation: async-worker suite **90 passed, 8 service-dependent skips**.

**Location:** [queue.py:58](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/apps/forge-async-worker/src/forge_async_worker/queue.py:58)

Document jobs have a 120-second SAQ heartbeat and a 1,800-second execution timeout, but `run_job` never refreshes the SAQ job heartbeat. SAQ can classify a healthy ingestion as stuck and abort or retry it well before its configured execution timeout.

**Evidence:** Invoked the actual job handler with an actual `saq.Job` and an isolated runner. Advancing the clock to 121 seconds produced `stuck=True` with an unchanged last-touch timestamp. The installed SAQ sweeper confirms that stale jobs are aborted or retried.

**Recommended fix:** Maintain a heartbeat task around document execution, with reliable cleanup, or disable this heartbeat and rely on the execution timeout.

### 8. [P2] Deleting one conversation deletes shared user artifacts

**Remediation — Fixed and tested.** Both deletion routes exclude `user:` artifacts from session cleanup. Regression coverage verifies shared artifacts and other sessions' files survive deletion, including deletion of a nonexistent session. Validation: reusable runtime selection **3 passed**; assistant deletion regression **1 passed**; changed-file Ruff checks passed.

**Locations:** [server.py:272](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/agent-runtime/src/forge_agent_runtime/server.py:272), [assistant.py:386](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/apps/forge-admin-api/src/forge_admin/api/routes/assistant.py:386)

Artifact listing includes `user:` files shared across conversations. The deletion handlers delete every returned artifact, removing those shared files when a single conversation is deleted.

**Evidence:** Saved a shared user artifact, then called the runtime's delete endpoint for a nonexistent session. The endpoint returned 204 and the shared artifact disappeared from another conversation's view.

**Recommended fix:** Delete only artifacts belonging to the selected session. Apply the correction to both the reusable runtime and assistant routes.

### 9. [P2] An ingestion already loading its source can resurrect a deleted document

**Remediation — Fixed and tested.** Ingestion captures a source-read fence and uses compare-and-set checks before starting or marking load failure. Deletes create tombstones even for unknown IDs, and explicit re-registration advances the fence. CLI ingestion can explicitly recreate a deleted path while preserving the unchanged-document optimization for existing records. Regressions cover deletion before first registration, deletion during loading, re-registration, and failure from an older loader. Validation for findings 9/10/11: full documents suite **210 passed, 1 skipped**; package Ruff and formatting passed **85 files**; source mypy passed **61 files**.

**Location:** [store.py:115](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/documents/src/forge_task_documents/storage/mongo/store.py:115)

The ingestion job loads its source before establishing its run fence. If deletion happens during that read, `begin_run()` unconditionally changes the tombstone back to processing. The older submission can then complete and expose deleted passages until cleanup executes. Memory and Spanner stores have the same ordering problem.

**Evidence:** Paused a document loader, deleted the document, and resumed loading. Ingestion completed successfully, the document became READY, and knowledge search returned the deleted text again.

**Recommended fix:** Establish the ingestion fence before loading or require an expected registration/version when beginning the run. Old submissions must not revive tombstones, including when deletion precedes the first ingestion record.

### 10. [P2] Document publication exposes mixed or failed versions

**Remediation — Fixed and tested.** Memory, Mongo, and Spanner store immutable chunk generations; successful completion atomically selects the published generation. Cleanup failures cannot change visibility or fail an already committed publication. Hybrid search legs share a generation snapshot, and section expansion stays bound to each hit's generation. An explicit zero pointer prevents deleted/recreated legacy records from exposing retained chunks. Tests cover same-ID metadata staging, partial writes, cleanup failures, publication between search legs, expansion races, legacy migration, and cleanup after recreation. Validation: included in **210 document tests passed, 1 skipped**, including **9 Spanner transaction/SQL regression cases**; shared embeddings **31 passed**; typing/lint checks passed. Required rollout/index changes are documented below.

**Location:** [store.py:229](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/documents/src/forge_task_documents/storage/mongo/store.py:229)

Publication makes new chunks searchable before removing old chunks in a separate operation. A partial failure leaves both generations visible. The document has already been marked READY, so the failure path cannot mark that completed run failed. Spanner also publishes in separate transactions.

Additionally, stable-ID chunks are overwritten while retaining their published flag. A replacement run can change visible metadata before it completes, even if it later fails.

**Evidence:** Injecting a cleanup failure after publication left contradictory old and new policy passages searchable together. A failed same-text re-ingestion also changed the visible tags of an already-published chunk.

**Recommended fix:** Store immutable generations and atomically switch an authoritative published-generation pointer. Searches should select that generation. A transaction around final cleanup alone does not fix earlier overwrites of published chunks.

### 11. [P2] Mongo write-concern failures are silently treated as success

**Remediation — Fixed and tested.** Mongo bulk-write handling propagates write-concern failures, unexpected empty error details, and nonduplicate write errors. Only explicit duplicate-only errors remain eligible for suppression. Validation: **2 targeted regressions passed**, including write-concern failure combined with duplicate-key errors, and the full documents suite passed **210 tests, 1 skipped**.

**Location:** [store.py:221](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/documents/src/forge_task_documents/storage/mongo/store.py:221)

The bulk-write exception handler checks `writeErrors` but ignores `writeConcernErrors`. A replication-acknowledgement failure with no per-operation errors is therefore treated as success, allowing publication despite uncertain durability.

**Evidence:** Injected a real `BulkWriteError` containing a write-concern timeout and no per-operation errors. The isolated ingestion returned READY with a positive chunk count even though its fake store retained no chunks. This demonstrates suppression of an uncertain outcome, not a live replication failure.

**Recommended fix:** Propagate or classify write-concern failures. Suppress only the deliberately tolerated duplicate-key errors when no other error category is present.

### 12. [P2] Concurrent runner builds can fail during cache eviction

**Remediation — Fixed and tested.** Runner leases retain active runners through completion/cancellation; cache mutations finish before asynchronous cleanup, and shutdown waits for leases. Five dedicated regressions cover slow eviction/concurrent builds, active-turn retention, cancellation, same-key waiter cancellation, and failed-build lock cleanup. Validation: runtime suite **221 passed**; executor/A2A typing and changed-file Ruff/formatting passed.

**Location:** [executor.py:338](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/agent-runtime/src/forge_agent_runtime/executor.py:338)

Eviction awaits runner cleanup while another build can modify the cache and evict the first build's newly inserted runner. When the first build resumes, `move_to_end` raises `KeyError`. Eviction also lacks protection for runners that are still in use.

**Evidence:** Reproduced concurrent builds with a cache size of one and cleanup paused by an ADK plugin. Building a third agent evicted the second while its build was suspended; the second request then failed with `KeyError`.

**Recommended fix:** Synchronize cache mutations, separate cleanup from unsafe cache-state transitions, and retain active runners until their users finish.

### 13. [P2] Child workflows lose their skill snapshots

**Remediation — Fixed and tested.** Child workflow payloads inherit the parent's skill snapshots. The regression validates the actual child payload and executes its inline Skills tool to verify the expected instructions load. Validation: agent-tools tests **32 passed**; changed-file Ruff, formatting, and source typing checks passed.

**Location:** [agent_tools.py:717](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/adk-workflows/src/forge_task_adk_workflows/graph/agent_tools.py:717)

Child workflow payload construction copies saved workflows, chat agents, and knowledge bases but omits `skills`. A child with an inline Skills tool consequently cannot resolve a skill that the parent already carries.

**Evidence:** Invoked the actual child-workflow creation path with a parent carrying a skill. Validating the captured child payload showed an empty skill dictionary. Existing skill execution tests confirm that missing snapshots fail the run.

**Recommended fix:** Include skill snapshots in inherited child resources and cover nested workflow execution with inline Skills tools.

### 14. [P2] Sandbox output can exhaust the host runtime's memory

**Remediation — Fixed and tested.** OpenShell retains at most 1 MiB of combined stdout/stderr by default and closes the response stream immediately on overflow, also preventing the SDK's internal buffer from continuing to grow. Sandbox deletion remains in the existing cleanup path. Validation: OpenShell suite **13 passed**, covering shared stream budget, endless output, exact-limit completion, stream closure, and deletion; Ruff and typing checks passed.

**Location:** [openshell.py:329](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/common/src/forge_common/sandbox/openshell.py:329)

OpenShell stdout and stderr chunks accumulate without a byte limit. Joining and decoding copy the accumulated data again. Sandboxed code can exhaust the shared runtime's memory by printing continuously before its time limit expires; later tool-result truncation cannot prevent that allocation.

**Evidence:** An isolated gateway returning 32 one-megabyte output chunks caused all 33,554,432 bytes to be retained and returned without a limit.

**Recommended fix:** Enforce a combined output budget while streaming. Truncate or terminate safely while preserving sandbox cleanup.

### 15. [P2] Public agents can remain accessible after organization deletion

**Remediation — Fixed and tested.** Public runtime authorization and chat-page resolution verify that the owning SQL organization still exists before permitting access. Tests leave external agent/page records in place after SQL deletion and verify rejection of metadata, A2A cards, session creation, and page runs. Validation: runtime-access and chat-page suites **15 passed**; changed-file Ruff checks passed.

**Location:** [runtime_access.py:107](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/apps/forge-admin-api/src/forge_admin/auth/runtime_access.py:107)

Public runtime authorization returns before checking whether the organization exists. Organization deletion commits SQL first and only logs subsequent Mongo cleanup failures. Remaining Mongo records therefore continue to resolve public agents after deletion reports success.

**Evidence:** With SQLite authorization, the real Mongo agent source, and an isolated store whose cleanup failed, organization deletion returned 204. Subsequent anonymous agent reads returned 200 and conversation creation returned 201.

**Recommended fix:** Verify organization existence before public authorization succeeds. Apply the same invariant to chat-page paths that resolve their records from Mongo.

### 16. [P2] Closing during an in-flight autosave loses newer edits

**Remediation — Fixed and tested.** Pagehide saves use the existing serialized queue and wait for the previous response's revision. Unload protection covers dirty documents and in-flight requests, including edits reverted while a prior save is outstanding. Regression tests delay an already-committed save response and verify that the latest edit is then saved using the returned revision. Validation for findings 16/17: **6 autosave tests passed**; application TypeScript and changed-source ESLint checks passed. Browser unload remains best effort if the user chooses to leave despite the warning.

**Location:** [autosave.ts:198](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/apps/forge-web/src/features/builder/components/autosave.ts:198)

The unload handler bypasses the serialized save queue and submits the latest document with the old local revision. If an earlier save committed but its response has not arrived, the final request receives 409, which is swallowed. Normal pending saves do not trigger the unload warning.

**Evidence:** Executed the actual transpiled hook with a revision-checking stub. The first edit committed revision 1 to 2; the final edit was sent with revision 1; the server retained only the first edit, with no unload prompt.

**Recommended fix:** Protect unload while saves are outstanding, preserve unsaved changes locally where necessary, and avoid competing revision-dependent writes.

### 17. [P2] Refused saves disable the unsaved-changes warning

**Remediation — Fixed and tested.** Dirty state is independent of automatic-retry eligibility. A refused document continues to trigger unload protection without being automatically resubmitted; an explicit retry can save it and clear the warning. Validation: regression cases for **400, 403, and 422** pass within the six autosave tests above.

**Location:** [autosave.ts:207](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/apps/forge-web/src/features/builder/components/autosave.ts:207)

After a 400, 403, or 422 response, `refused` matches the current document. The `pending()` predicate excludes that document, so the unload warning cannot fire for the unsaved edit. The pagehide handler also skips saving it.

**Evidence:** Executed the actual transpiled hook with a 403 save response. It entered the refused state, but closing produced no warning and no further save attempt.

**Recommended fix:** Separate “different from the persisted document” from “eligible for retry.” Use the unsaved/dirty predicate for unload protection.

## Remediation validation

Completed checks during remediation (some selections overlap):

| Area | Result |
| --- | --- |
| Reusable agent runtime | 221 passed; full source/test mypy passed (48 files) |
| Admin runtime authorization and chat pages | 108 passed |
| Admin conversations and assistant | 282 passed |
| Admin knowledge integration | 123 passed |
| MCP shared package / admin MCP | 55 passed / 35 passed |
| Async worker | 90 passed, 8 service-dependent skips |
| ADK workflows | 538 passed |
| OpenShell executor | 13 passed; source/test typing passed |
| Document storage, ingestion, retrieval, and CLI | 210 passed, 1 skipped; Ruff/format 85 files and source mypy 61 files clean |
| Shared embeddings | 31 passed |
| Python codegraph readers / Python MCP | 119 passed, 20 service-dependent skips / 45 passed |
| Web frontend | 260 passed, 1 existing layout TODO; TypeScript passed |
| Autosave regressions | 6 passed (included in web total); ESLint and Prettier passed |

Changed Python files passed Ruff and formatting checks. Independent cross-review also rechecked the audit isolation, artifact preservation, orphan-organization, and autosave fixes. Validation uncovered a stale assistant test call to a removed private credential method; the test now exercises the current public method and passes. Credential test annotations were corrected without changing their behavior to allow the complete runtime typing check.

Storage tests include Memory and Mongo array/binary-vector contracts plus nine Spanner SQL/transaction cases using a SQLite-backed adapter. The new CLI regression also caught and corrected a pre-existing duplicate `reason` argument when reporting unchanged ingestions. Independent storage cross-review verified the publication snapshot, legacy tombstones, and generation-bound expansion paths.

## Deployment notes

These are working-tree fixes; no production deployment or database migration has been performed.

- MongoDB Atlas needs `run_seq` and `live` filter fields added to the chunks vector index before deploying generation-aware readers. The normal schema check only warns about existing-index drift. The [documents upgrade guide](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/documents/README.md:77) provides the migration and rollout sequence.
- Drain document ingestion and coordinate updates to Python readers and writers; mixing old and new storage-format implementations is unsupported. Spanner stores the pointer in its existing JSON payload and requires no DDL change. Legacy records remain readable, but previously corrupted generations need re-ingestion from source.
- Outbound HTTP tools now require the guarded transport; proxy routes and injected unguarded network clients fail closed. See the [runtime README](/Users/josephleva/IdeaProjects/ei-tiger-agent-workflow-builder/packages/python/agent-runtime/README.md).
- The Go ingestion project remains excluded and unchanged.

## Original review validation

Existing test runs passed. These findings came from additional targeted reproductions and tracing beyond the existing tests.

| Review area | Reported test results |
| --- | --- |
| Reusable agent runtime | 154 passed |
| Async worker | 89 passed, 8 skipped |
| ADK workflows | 533 passed |
| Task SDK | 17 passed |
| Authentication and API-key policy | 155 passed |
| Additional admin API boundary tests | 82 passed |
| Shared integrations and framework | 179 passed; external-service/real-Pi cases excluded |
| Documents, embeddings, and Python codegraph/MCP | 371 passed, 21 skipped |
| JSONata | 1,713 passed, 1 skipped |
| Web frontend | 233 passed, 1 existing TODO |
| Forge UI | 26 passed |
| TypeScript checks | Both application checks passed with `--noEmit` |

Some test selections overlap; these figures should not be summed as a unique-test count. Experimental-feature warnings and sandbox socket warnings occurred in some suites without causing failures.

## Limits and excluded candidates

- Live MySQL locking, MongoDB Atlas, Redis/Spanner service behavior, external model providers, real OAuth services, real Pi, and a live OpenShell gateway were not exercised. Relevant service-dependent tests were skipped or excluded.
- Mongo visibility filters read generation metadata for the requested tenants/documents; production-scale latency and filter size have not been benchmarked. See the upgrade guide for this implementation detail and rollout requirements.
- Reproductions used isolated stores, fake transports/gateways, local SQLite, and bounded subprocesses. They establish the reported code paths but are not production incident evidence.
- Explicit draft execution and organization-wide external-user IDs are supported by existing documentation and tests, so they were not reported as defects.
- Hosted chat-agent saves strip embedded dependencies and overwrite organization identity, so the considered embedded foreign-dependency injection route was excluded.
- The review is not proof that all remaining code is defect-free. Remediation status and regression validation are recorded per finding above; production verification has not been performed.
