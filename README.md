# duckdb-adls-serverless

A serverless lakehouse pattern: DuckDB as the query engine, Azure Data Lake Storage Gen2 as the data lake, Azure Functions as the compute layer. Storage and compute scale completely independently, you pay only when a query actually runs, and there are no secrets anywhere in the request path — just a managed identity and one scoped permission.

```
            ┌─────────────────────┐        ┌───────────────────────┐
  request → │   Azure Function     │  read  │   ADLS Gen2 (Parquet) │
            │   (managed identity)  │───────▶│   one scoped role:    │
            │   DuckDB (local file) │        │   Storage Blob Data   │
            └─────────────────────┘        │   Reader               │
                                             └───────────────────────┘
```

On a cold start, the function authenticates, downloads the file once, and caches it on local disk for the lifetime of that worker instance. On a warm invocation, it skips straight to querying the already-cached file — no network call, no re-authentication, just DuckDB reading a local Parquet file.

## Why it's built this way, specifically

**DuckDB never talks to Azure directly.** The obvious way to do this is DuckDB's own `azure` extension, reading `azure://container/file.parquet` straight off blob storage. We tried that first. It fails on Azure Functions (Linux Consumption, Python) with a misleading `"Problem with the SSL CA cert (path? access rights?)"` error — this is a known issue ([duckdb/duckdb-azure#8](https://github.com/duckdb/duckdb-azure/issues/8)) caused by how the extension's statically-linked libcurl resolves CA certificate paths on certain Linux containers. It is **not** a missing certificate and **not** a permissions problem — both were ruled out directly before concluding it was the extension itself (see [`docs/THE_BUG.md`](docs/THE_BUG.md) for the full diagnostic trail).

Two working fixes exist:
1. `SET azure_transport_option_type = 'curl';` before querying — the officially documented fix. Confirmed working on Azure Functions Linux Consumption, Python 3.12, as of this repo.
2. **What this repo actually does**: skip the extension's network path entirely. `azure-identity` + `azure-storage-blob` (official, well-tested Microsoft SDKs) handle the one risky network call — authenticating and downloading the file — and DuckDB only ever touches the resulting local file. Fewer moving parts in the one place that broke.

## Deploy it yourself

Requires: an Azure subscription, the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), and [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) (`npm install -g azure-functions-core-tools@4`).

```bash
# 1. Resource group + storage account (your data lake)
az group create --name rg-duckdb-lakehouse --location eastus
az storage account create --name <your-storage-account> \
  --resource-group rg-duckdb-lakehouse --location eastus \
  --sku Standard_LRS --kind StorageV2 --hns true   # --hns true = ADLS Gen2, not plain blob storage

# 2. A container, and upload whatever Parquet file you want to query
az storage container create --name trial-data \
  --account-name <your-storage-account> --auth-mode login
az storage blob upload --account-name <your-storage-account> \
  --container-name trial-data --name your-data.parquet \
  --file ./your-data.parquet --auth-mode login

# 3. A separate storage account the Function App needs for its own runtime state
az storage account create --name <your-func-storage> \
  --resource-group rg-duckdb-lakehouse --location eastus --sku Standard_LRS

# 4. The Function App itself (Linux Consumption = serverless, pay-per-use)
az functionapp create --resource-group rg-duckdb-lakehouse \
  --consumption-plan-location eastus --runtime python --runtime-version 3.12 \
  --name <your-function-name> --storage-account <your-func-storage> --os-type Linux

# 5. Managed identity + the one scoped permission it actually needs
az functionapp identity assign --name <your-function-name> --resource-group rg-duckdb-lakehouse
# copy the principalId from the output, then:
az role assignment create --assignee <principalId> --role "Storage Blob Data Reader" \
  --scope $(az storage account show --name <your-storage-account> \
    --resource-group rg-duckdb-lakehouse --query id -o tsv)

# 6. Set the app's config and deploy
az functionapp config appsettings set --name <your-function-name> \
  --resource-group rg-duckdb-lakehouse --settings \
  STORAGE_ACCOUNT_NAME=<your-storage-account> CONTAINER_NAME=trial-data BLOB_PATH=your-data.parquet

func azure functionapp publish <your-function-name>
```

Then: `curl https://<your-function-name>.azurewebsites.net/api/query`

### Local development

```bash
cp local.settings.json.example local.settings.json   # fill in your own values
az login   # DefaultAzureCredential picks this up locally
func start
```

## What the numbers actually look like

Measured against a real ADLS Gen2 account, same-region Function App, 200k-row Parquet file:

| Call | Cache hit | Download step | Query step | Total |
|---|---|---|---|---|
| 1st (cold) | No | 0.95s | 0.047s | **1.00s** |
| 2nd (warm) | Yes | 0s | 0.006s | **0.0056s** |

Cold start cost is paid once per worker instance, not per request — how often you actually pay it depends entirely on how your specific hosting plan reuses warm instances, which is worth testing for your own traffic pattern rather than assumed.

## Configuration

| Setting | What it is |
|---|---|
| `STORAGE_ACCOUNT_NAME` | Your ADLS Gen2 storage account name |
| `CONTAINER_NAME` | The blob container holding your data file |
| `BLOB_PATH` | Path to the file inside the container |

No connection string, no key, no secret — authentication is `DefaultAzureCredential`, which resolves to the Function's managed identity in Azure and your own `az login` session locally.

## What this is and isn't

This is a **pattern**, not a production query API — the `/query` endpoint runs one fixed demo query (`SELECT COUNT(*)`) against the configured file. Accepting arbitrary SQL from an HTTP request is a SQL-injection-shaped problem you'd need to solve deliberately (parameterization, a query allowlist, or a guard layer), not something this template does for you. Swap the query in `function_app.py` for whatever your real use case needs.

## License

MIT — see [LICENSE](LICENSE).
