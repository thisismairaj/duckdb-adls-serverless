"""
DuckDB-over-ADLS serverless pattern: a single HTTP-triggered Azure Function that
queries a Parquet/Delta file sitting in Azure Data Lake Storage Gen2, using DuckDB
as the query engine and the official Azure SDKs (not DuckDB's own `azure` extension)
for the actual storage connection.

Why not DuckDB's `azure` extension directly: in testing, it failed inside a Linux
Consumption Function container with a misleading "Problem with the SSL CA cert"
error. That error is not about a missing or invalid certificate (checked directly)
and not about permissions (disproven directly - the official SDK, using the exact
same managed identity, succeeds immediately). The root cause is internal to that
extension's own HTTP/TLS stack on this container type. See README.md for the full
debugging story. The fix here: let `azure-identity` + `azure-storage-blob` do the
one network call that matters (download the blob), and keep DuckDB scoped to a
local file it reads after that - never talking to the cloud directly.

Configuration (environment variables / local.settings.json, see .env.example):
    STORAGE_ACCOUNT_NAME   - the ADLS Gen2 storage account, e.g. "mydata01"
    CONTAINER_NAME          - the blob container holding the data file
    BLOB_PATH               - path to the file inside the container, e.g. "sales.parquet"

Auth: DefaultAzureCredential - resolves to the Function App's managed identity when
deployed to Azure, or your own `az login` session when run locally via `func start`.
No connection string or key is ever read, stored, or logged.
"""

import json
import logging
import os
import tempfile
import time
import traceback

import azure.functions as func
import duckdb
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

STORAGE_ACCOUNT_NAME = os.environ["STORAGE_ACCOUNT_NAME"]
ACCOUNT_URL = f"https://{STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
CONTAINER_NAME = os.environ["CONTAINER_NAME"]
BLOB_PATH = os.environ["BLOB_PATH"]
LOCAL_CACHE_PATH = os.path.join(tempfile.gettempdir(), "duckdb_adls_cache.parquet")

# Module-level: created once per cold start, reused across warm invocations of the
# same worker process - this is the entire mechanism behind "warm" reuse here.
# Nothing special needed beyond ordinary module-level state.
_credential = None
_con = None


def _get_credential():
    global _credential
    if _credential is None:
        _credential = DefaultAzureCredential()
    return _credential


def _get_duckdb_connection():
    global _con
    if _con is None:
        _con = duckdb.connect()
    return _con


def _ensure_local_copy() -> tuple[str, bool]:
    """Download the blob to local disk once via the Azure SDK; reuse it on warm
    invocations. Returns (path, was_cached)."""
    if os.path.exists(LOCAL_CACHE_PATH):
        return LOCAL_CACHE_PATH, True
    blob_service_client = BlobServiceClient(account_url=ACCOUNT_URL, credential=_get_credential())
    blob_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=BLOB_PATH)
    with open(LOCAL_CACHE_PATH, "wb") as f:
        download_stream = blob_client.download_blob()
        f.write(download_stream.readall())
    return LOCAL_CACHE_PATH, False


@app.route(route="query", methods=["GET"])
def query(req: func.HttpRequest) -> func.HttpResponse:
    """Runs a fixed demo query against the configured file. Replace the SQL below
    with whatever your own data actually needs - this endpoint is a template,
    not a general-purpose query API (it does not accept arbitrary SQL from the
    request, which would be a SQL-injection-shaped problem for a public endpoint)."""
    logging.info("duckdb-adls-serverless: query invoked")
    t_start = time.perf_counter()

    try:
        local_path, was_cached = _ensure_local_copy()
        t_after_cache = time.perf_counter()

        con = _get_duckdb_connection()
        result = con.execute(
            f"SELECT COUNT(*) AS row_count FROM read_parquet('{local_path}')"
        ).fetchall()
        t_after_query = time.perf_counter()

        body = {
            "cache_hit": was_cached,
            "download_step_seconds": round(t_after_cache - t_start, 4),
            "query_seconds": round(t_after_query - t_after_cache, 4),
            "total_seconds": round(t_after_query - t_start, 4),
            "rows": [dict(zip(["row_count"], r)) for r in result],
        }
        return func.HttpResponse(
            json.dumps(body, indent=2),
            mimetype="application/json",
            status_code=200,
        )
    except Exception as e:
        logging.exception("duckdb-adls-serverless: query failed")
        error_body = {
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc(),
        }
        return func.HttpResponse(
            json.dumps(error_body, indent=2),
            mimetype="application/json",
            status_code=500,
        )
