# The bug, and how it was actually diagnosed

This is the debugging trail that led to this repo's design — specifically, why it uses `azure-identity`/`azure-storage-blob` instead of DuckDB's own `azure` extension. Written up because the error message itself is genuinely misleading, and the obvious theories (bad cert, wrong permissions) both turned out to be wrong.

## The symptom

A Python Azure Function (Linux Consumption plan), using DuckDB's `azure` extension to read a Parquet file straight from ADLS Gen2, failed on every single call — cold or warm — with:

```
IO Error: AzureStorageFileSystem could not open file: 'azure://container/file.parquet',
unknown error occurred, this could mean the credentials used were wrong. Original error
message: 'Fail to get a new connection for: https://<account>.blob.core.windows.net.
Problem with the SSL CA cert (path? access rights?)'
```

The error text itself offers two guesses — a cert path problem, or an access-rights problem. Neither turned out to be right.

## Ruling out "it's the permissions"

The Function's managed identity had been granted `Storage Blob Data Reader`, scoped to the exact storage account. Easy to doubt that grant was actually working. Two things ruled it out directly, not by assumption:

1. **The nature of the error itself.** A real permissions failure shows up as a successful TLS connection followed by an HTTP `403 Forbidden`. This error is a connection-establishment failure — it never gets far enough to be rejected by authorization, it fails before that.
2. **A direct control test.** The official `azure-identity` + `azure-storage-blob` SDKs, using the *exact same* managed identity, authenticated and downloaded the file successfully on the first try. Same identity, same role assignment, different code path — one worked, one didn't. That's conclusive: not a permissions problem.

## Ruling out "it's a missing certificate"

The error text says "SSL CA cert," so the obvious next theory: the container is missing a CA bundle. Checked directly:

- `certifi`'s bundled CA file existed and was non-empty.
- `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` environment variables were set to point at it.
- The system path Linux tools typically expect (`/etc/ssl/certs/ca-certificates.crt`) was checked directly — it existed, and was a reasonable size (not empty or truncated).
- None of this changed the error at all.

A second control test confirmed TLS itself was fine at the OS level: plain Python (`urllib`), no DuckDB involved, made a real HTTPS request to the same storage account hostname and got back a real HTTP response (not a connection failure). TLS and networking both worked at the container level. The one thing that didn't work was DuckDB's `azure` extension specifically.

## The actual cause

This turned out to be a known, previously-reported issue: [duckdb/duckdb-azure#8](https://github.com/duckdb/duckdb-azure/issues/8), opened September 2023. The root cause, per the extension's maintainers: libcurl is statically linked into the extension, and on certain Linux environments it looks for CA certificates in paths that don't match where they actually live in that container. It's not that the certs are missing from the system — it's that the statically-linked libcurl inside the extension doesn't know where to find them.

A fix exists and is documented, but is **opt-in, not the default**:

```sql
SET azure_transport_option_type = 'curl';
```

This was tested directly against this exact failure, on Azure Functions Linux Consumption Python 3.12 (an environment not covered in the original issue thread) — and it resolved it completely. See the posted follow-up on that issue for the confirmed before/after.

## Why this repo doesn't just use that fix

It's a real, working fix, and if you want to keep using DuckDB's `azure` extension directly, setting that option is a perfectly reasonable choice. This repo takes a different path on purpose: **DuckDB never makes the risky network call at all.** The official SDKs handle authentication and download — code that's extremely well-tested, used by essentially everyone who talks to Azure Storage from Python — and DuckDB is kept scoped to a plain local file. Fewer places for a dialect- or container-specific bug to hide, at the cost of giving up the extension's lazy/partial-read capability on very large files.

Which approach is right depends on your own file sizes and query patterns. Both are documented here because both are real, valid fixes to the same underlying problem.
