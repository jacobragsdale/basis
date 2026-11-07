# Spike 0.8: Data Lake

Step 5.7 builds on `pyarrow.fs.AzureFileSystem` in the locked pyarrow 26.0.1, with no new dependency. It signs in with
an account key, a SAS token, workload identity and the Azure CLI, on Windows, Linux and macOS. With the hierarchical
namespace (HNS) its `move` is one server-side rename. Without HNS, `move` refuses, so a flat account fails loudly
instead of copying. Two things shape the build:

- workload identity and the CLI are reachable only through `FileSystem.from_uri("abfs://…?credential_kind=…")`, not
  through the constructor;
- the bare default credential chain is unsafe on a desktop, because it tries a managed identity before `az login`.

Run on 2026-11-07 against pyarrow's source, its wheels and Azurite. No firm account was touched. The firm's side is
assumed as given: an ADLS Gen2 account with HNS on, workload identity on AKS, `az login` on desktops, and private
endpoints possible. Each question ends with a best guess to build to now, and the [(work)](#work-to-run-at-the-firm)
checklist confirms the assumptions at the port.

## Versions

- pyarrow 26.0.1 from `uv.lock` (Arrow C++ 26.0.1, tag `apache-arrow-26.0.1`), Python 3.14.7, uv 0.13.6.
- The Azure SDK for C++ that the wheels bundle:
  - azure-identity-cpp 1.13.3. The SDK's requests from this host carried the user agent `azsdk-cpp-identity/1.13.3`, and
    `arrow.dll` in the Windows wheel holds the string `1.13.3`.
  - azure-core-cpp 1.16.4, azure-storage-blobs-cpp 12.19.1 and azure-storage-files-datalake-cpp 12.17.0.
  - These are the vcpkg ports at commit `61d7eaeb` (2026.10.24), which [Arrow's
    `.env`](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/.env#L95) pins for its wheels.
- Azurite 3.37.0 (`mcr.microsoft.com/azure-storage/azurite@sha256:830430c1…`), on a Debian 13 host (glibc 2.41).
- Used only by the probe, to list raw blobs and to make a SAS: azure-storage-blob 12.31.1.
- For the fallback: adlfs 2026.8.0, fsspec 2026.9.0, azure-identity 1.27.0 and azure-storage-file-datalake 12.26.1.

## Setup

- A scratch venv with `pyarrow==26.0.1`.
- Arrow's source at the tag: `cpp/src/arrow/filesystem/azurefs.{h,cc}`, `python/pyarrow/_azurefs.pyx` and the wheel
  build scripts. The Azure SDK for C++ at the tags `azure-identity_1.13.3`, `azure-core_1.16.4` and
  `azure-storage-files-datalake_12.17.0`.
- The four cp314 wheels from the lock, each checked against its hash: `win_amd64`, `manylinux_2_28_x86_64`,
  `macosx_12_0_arm64` and `macosx_12_0_x86_64`. Also pyarrow 23.0.1 and 24.0.0 for `win_amd64`, for the history.
- Azurite, blob service only, in the container `spike08-azurite` with `--memory 512m`. It peaked at about 75 MiB and was
  removed afterwards. Azurite has no HNS, so it shows the flat-namespace behaviour.
- Sign-in flows checked with fakes, against `azureopendatastorage`, a public Microsoft account:
  - for the CLI, a stand-in `az` on `PATH` that logs its arguments and prints a fake token;
  - for workload identity, made-up tenant and client ids with a fake service-account token file;
  - the account refuses the fake tokens with a 401, so reaching that 401 shows a token was fetched and sent over TLS;
  - `AZURE_LOG_LEVEL=verbose` makes the C++ SDK log which credential ran and which requests went out.

  The SDK appends `/usr/bin:/usr/local/bin` to `PATH` for the CLI credential. So one early run, made before `HOME` was
  pointed at an empty directory, fell through the default chain to this host's real `az` login. It sent that token to
  the public account, which refused it with a 401. Every run quoted below uses the stand-in.

## Answers

### 1. Sign-in in pyarrow 26.0.1

| Question | Answer | How known |
|---|---|---|
| `account_key` in the constructor | Yes. | verified here (Azurite) |
| `sas_token` in the constructor | Yes, but the token must start with `?`. Without it the SDK appends the token to the URL path, and Azure answers `400 OutOfRangeInput` ([arrow#49972](https://github.com/apache/arrow/issues/49972)). | verified here |
| `client_id` + `tenant_id` + `client_secret` | Yes, as `ClientSecretCredential`. | from source ([`_azurefs.pyx`](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/python/pyarrow/_azurefs.pyx)) |
| Managed identity | Yes: `client_id` alone gives `ManagedIdentityCredential(client_id)`. | from source |
| Workload identity, Azure CLI, in the constructor | **No.** The constructor takes `account_name, *, account_key, blob_storage_authority, blob_storage_scheme, client_id, client_secret, dfs_storage_authority, dfs_storage_scheme, sas_token, tenant_id` and nothing more. Both are reachable through `FileSystem.from_uri` with `credential_kind=workload_identity` or `credential_kind=cli`, which calls `ConfigureWorkloadIdentityCredential()` or `ConfigureCLICredential()`. | verified here (both made the requests in the next rows); from source ([`azurefs.h`](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/cpp/src/arrow/filesystem/azurefs.h#L150-L189), [`ExtractFromUriQuery`](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/cpp/src/arrow/filesystem/azurefs.cc#L104)) |
| A default chain when nothing is given | Yes, `DefaultAzureCredential`. | verified here |
| Does the default chain include workload identity? | Yes. In 1.13.3 the order is `EnvironmentCredential`, `WorkloadIdentityCredential`, `ManagedIdentityCredential`, `AzureCliCredential`, and the SDK logs: "Created with the following credentials: EnvironmentCredential, WorkloadIdentityCredential, ManagedIdentityCredential, AzureCliCredential." | verified here; from source ([`default_azure_credential.cpp`](https://github.com/Azure/azure-sdk-for-cpp/blob/azure-identity_1.13.3/sdk/identity/azure-identity/src/default_azure_credential.cpp#L88)) |
| Does it read `AZURE_FEDERATED_TOKEN_FILE`, `AZURE_CLIENT_ID` and `AZURE_TENANT_ID`? | Yes. With made-up ids and a fake token file, it posted to `https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`, which answered 401. It reads the token file again for each token request, so it follows the rotating projected token. It refuses a plain-http `AZURE_AUTHORITY_HOST`: "Authority host must be a TLS protected (https) endpoint." | verified here; the reread from source ([`workload_identity_credential.cpp`](https://github.com/Azure/azure-sdk-for-cpp/blob/azure-identity_1.13.3/sdk/identity/azure-identity/src/workload_identity_credential.cpp)) |
| Does the default chain include the Azure CLI? | Yes, last, unless `AZURE_TOKEN_CREDENTIALS=prod`. It runs `az account get-access-token --output json --scope https://storage.azure.com/.default`: through `/bin/sh` on Linux and `cmd /c` on Windows, with a 13 s timeout and `/usr/bin:/usr/local/bin` appended to `PATH`. | verified here with the stand-in `az`; the timeout and `PATH` from source ([`azure_cli_credential.cpp`](https://github.com/Azure/azure-sdk-for-cpp/blob/azure-identity_1.13.3/sdk/identity/azure-identity/src/azure_cli_credential.cpp#L135)) |
| `AZURE_TOKEN_CREDENTIALS` | Works in 26.0.1, though [arrow#50106](https://github.com/apache/arrow/issues/50106) doubts it. `AzureCliCredential` leaves that credential alone in the chain, and `prod` drops the CLI. | verified here |

The docs say: "By default DefaultAzureCredential is used for authentication. This means it will try several types of
authentication and go with the first one that works. If any authentication parameters are provided when initialising the
FileSystem, they will be used instead of the default credential."
([pyarrow.fs.AzureFileSystem](https://arrow.apache.org/docs/python/generated/pyarrow.fs.AzureFileSystem.html)). The C++
header lists the variables the chain may read: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`,
`AZURE_AUTHORITY_HOST`, `AZURE_CLIENT_CERTIFICATE_PATH` and `AZURE_FEDERATED_TOKEN_FILE`
([`azurefs.h`](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/cpp/src/arrow/filesystem/azurefs.h#L52)).

Also found, all verified here:

- `from_uri` takes no account key: it raises `ArrowInvalid: Unexpected query parameter in Azure Blob File System URI:
  'account_key'`. It does take a SAS token in the query, but that puts the secret in the URI.
- HTTP failures raise `OSError`. A credential that cannot get a token raises a bare `pyarrow.lib.ArrowException`:
  "Unknown error: … AuthenticationException …".
- The first call on a container runs the HNS check, so a sign-in failure reads "Check for Hierarchical Namespace support
  on '…' failed".
- The default chain asks IMDS (`169.254.169.254`) before it asks the CLI. Here, where nothing answers, that took 0.1 to
  1.0 s. On a desktop that is an Azure VM with an identity of its own, as a Citrix VDI can be, the chain signs in as the
  VM, not as the user.
- An image that has `az` and a logged-in profile falls through to the CLI without a word.
- `AzureFileSystem.__reduce__` carries the account key and the client secret in clear.
- The run store's docstring example is backwards. `storage.py` (line 32) offers `abfs://container@account/runs`, and
  pyarrow reads it as the account `container` with the path `account/runs`. The full form,
  `abfs://container@account.dfs.core.windows.net/runs`, which the settings and templates already use, reads correctly.
- The run store opens its URL with the default chain too, so everything here applies to it as well.

**Best guess.** The environment's settings name the sign-in as `key`, `sas`, `workload_identity` or `cli`, and basis
builds the filesystem from that setting. The bare default chain is never used:

```python
match auth:
    case "key":
        fs = pafs.AzureFileSystem(account, account_key=secret)
    case "sas":
        fs = pafs.AzureFileSystem(account, sas_token="?" + secret.removeprefix("?"))
    case "workload_identity" | "cli":
        fs, _ = pafs.FileSystem.from_uri(f"abfs://{container}@{account}.dfs.core.windows.net/?credential_kind={auth}")
```

A pod's deployment sets `workload_identity`, and a desktop's file sets `cli`. Messages name the account, container and
path, never the URI with its query. The run store's `Root.at` should share this builder, or document `?credential_kind=`
in `BASIS_SNAPSHOT_ROOT`.

**Fallback for the no.** `from_uri` with `credential_kind`, as above. The second choice is the default constructor with
`AZURE_TOKEN_CREDENTIALS=WorkloadIdentityCredential` or `AzureCliCredential` (verified), but that setting is
process-wide.

### 2. Platforms

| Question | Answer | How known |
|---|---|---|
| `win_amd64` | Yes: `pyarrow/_azurefs.cp314-win_amd64.pyd`. `arrow.dll` holds the workload-identity and CLI credentials (`AZURE_FEDERATED_TOKEN_FILE`, `AZURE_TOKEN_CREDENTIALS`, `cmd /c`) and the WinHTTP transport. | verified here (the lock's wheel, contents and strings) |
| `manylinux_2_28_x86_64` | Yes: `_azurefs.cpython-314-x86_64-linux-gnu.so`. Everything above ran on it. | verified here |
| macOS, `arm64` and `x86_64` (`macosx_12_0`) | Yes: `_azurefs.cpython-314-darwin.so` in both. | verified here (contents only) |
| Did early versions lack it? | Yes, on Windows. Windows wheels have no `_azurefs` through 23.0.1, and have it from 24.0.0 ([#49170](https://github.com/apache/arrow/pull/49170), GH-44655, merged 2026-02-12). The Python bindings arrived in 16.0.0 ([#40021](https://github.com/apache/arrow/pull/40021)), and the `client_id`/`client_secret` arguments in 21.0.0 ([#46837](https://github.com/apache/arrow/pull/46837)). | verified here: the 23.0.1 and 24.0.0 `cp313-win_amd64` wheels, and the PRs located by GitHub's compare against each tag |

All three platforms build with `ARROW_AZURE=ON`
([Windows](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/ci/scripts/python_wheel_windows_build.bat#L40),
[Linux](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/ci/scripts/python_wheel_xlinux_build.sh#L59),
[macOS](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/ci/scripts/python_wheel_macos_build.sh#L66)).

TLS from the manylinux wheel on Debian 13 works with the system store (verified here): a bogus SAS drew a 403 from the
service, and an anonymous read of a public container succeeded. The Azure client does not use pyarrow's Linux CA fix-up,
`_init_ca_paths`, which reaches only S3, since `azurefs.cc` never reads the global TLS options. It relies on OpenSSL's
compiled-in `/etc/ssl/cert.pem` and `/etc/ssl/certs`, so a pod image needs any corporate root in its system store.

**Best guess.** Keep `pyarrow>=26.0.1`, and never let the floor fall below 24.0.0, for Windows. basis needs no platform
branch.

### 3. Atomic rename

| Question | Answer | How known |
|---|---|---|
| With HNS, is a directory rename one atomic server-side operation? | Yes. Without real directories, "applications must process potentially millions of individual blobs to achieve directory-level tasks. By contrast, a hierarchical namespace processes these tasks by updating a single entry (the parent directory)." The docs' own example is Hive and Spark writing "to temporary locations and then rename the location at the conclusion of the job". | from docs ([hierarchical namespace](https://learn.microsoft.com/en-us/azure/storage/blobs/data-lake-storage-namespace)) |
| What does `move` do with HNS on? | One [`Path - Create`](https://learn.microsoft.com/en-us/rest/api/storageservices/datalakestoragegen2/path/create) with `x-ms-rename-source`, against the dfs endpoint. Arrow calls `RenameFile`, for directories too, and holds 15 s blob leases on the source and, if it exists, the destination. It refuses a non-empty destination directory (`PathAlreadyExists` becomes "not empty"), replaces an empty directory or a file, and refuses a move across containers. The SDK sends one request and follows no continuation token. | from source ([`MovePathWithDataLakeAPI`](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/cpp/src/arrow/filesystem/azurefs.cc#L3102), [`RenameFile`](https://github.com/Azure/azure-sdk-for-cpp/blob/azure-storage-files-datalake_12.17.0/sdk/storage/azure-storage-files-datalake/src/datalake_file_system_client.cpp#L349)); not runnable here, since no emulator has HNS |
| With HNS off: refuse, or copy and delete? | **Refuses**, for directories and files alike, and moves nothing: `pyarrow.lib.ArrowNotImplementedError: FileSystem::Move() is not implemented for Azure Storage accounts without Hierarchical Namespace support (see arrow/issues/40405).` The source stays where it was. `copy_file` still works, so a non-atomic move would have to be written on purpose. | verified here (Azurite); from source ([`MovePathUsingBlobsAPI`](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/cpp/src/arrow/filesystem/azurefs.cc#L3222)) |
| Can basis find out whether HNS is on? | Yes, three ways. (a) The refusal itself: `ArrowNotImplementedError` is a `NotImplementedError` (verified), with the message above. (b) pyarrow's own check, which runs once per container on first use: `getAccessControl` on the container's root, seen in the log as `HEAD https://ACCOUNT.dfs.core.windows.net/CONTAINER/?action=getAccessControl`. It is a data-plane call, but Python does not expose its result. (c) `az storage account show --query isHnsEnabled`, for a person with read access to the account. | (a) verified here; (b) seen in the SDK log, from source ([`CheckIfHierarchicalNamespaceIsEnabled`](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/cpp/src/arrow/filesystem/azurefs.cc#L1422)); (c) from docs |

`Get Account Information` also returns `x-ms-is-hns-enabled`. Under Entra, though, it needs
`Microsoft.Storage/storageAccounts/blobServices/getInfo/action`, which "is included in the Owner role", so it is not for
a job's identity ([Get Account
Information](https://learn.microsoft.com/en-us/rest/api/storageservices/get-account-information)).

**Best guess.** An interval's archive goes in four steps:

1. Write `TABLE/.tmp-RUN_ID/date=D/part-{i}.parquet`. Readers skip names that start with `.`: a dataset over `TABLE`
   counted only the visible partition while a `.tmp-*` directory sat beside it (verified).
2. Read every file back. Take the SHA-256 of its bytes and the rows in its footer, and check that the total equals the
   rows written.
3. `fs.move("TABLE/.tmp-RUN_ID/date=D", "TABLE/date=D")`. This is the one atomic step. Then delete the empty
   `.tmp-RUN_ID`.
4. Write the ledger row: the interval, the path, the rows and the hashes.

A `NotImplementedError` from the move deletes the temporary tree and stops the run with one `ERROR`: "ACCOUNT has no
hierarchical namespace, which the lake archive needs, so that a rename is atomic". `basis doctor --env` runs the same
create, rename and delete on a scratch directory under the archive root.

A retry checks the ledger before step 1. If the interval is there, the retry archives nothing. If the rename then
refuses because the partition exists (a crash between steps 3 and 4), basis reads that partition back. It records the
partition if the row count matches the interval's, and otherwise refuses, naming the path.

`--again` moves the old partition aside to `TABLE/.old-RUN_ID/`, moves the new one in, then deletes the old one. That is
two renames, so a reader can briefly find no partition, but never half of one.

### 4. The dataset path

| Question | Answer | How known |
|---|---|---|
| Do `pads.write_dataset` and `pq.write_to_dataset` write a hive-partitioned path through AzureFileSystem? | Yes, both. | verified here (flat namespace). With HNS, from source, the same calls create real directories. |
| Empty-directory markers | On a flat namespace, `create_dir`, which `write_dataset` calls, writes a zero-byte `dir/` blob with the metadata `is_directory=true` at each level. The markers stay after the files go; `delete_dir` removes them. With HNS, directories are real, made by `DataLakeDirectoryClient::CreateIfNotExists`, and there are no markers. | flat verified here (raw listing); HNS from source ([`azurefs.cc`](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/cpp/src/arrow/filesystem/azurefs.cc#L2357-L2430)) |
| `existing_data_behavior` | `error`, `write_dataset`'s default, refuses a non-empty base directory: "Could not write to … as the directory is not empty and existing_data_behavior is to error". `overwrite_or_ignore` replaces files of the same name, and `delete_matching` empties each partition before writing to it. `pq.write_to_dataset` names its files `<uuid>-0.parquet`, so a rerun adds a second copy unless it uses `delete_matching`. | verified here |
| Partition types | Without a schema, hive discovery reads `date=2026-11-07` as `string`. With `pads.partitioning(pa.schema([("date", pa.date32())]), flavor="hive")` it reads `date32`. | verified here |
| Large files and block sizes | A 3,000,000-row file of 49.7 MB, uncompressed, wrote and read back. Writes are buffered into 10 MiB blocks, and a single write over 10 MiB goes as one block of up to 4 GiB. The writes run in the background by default and are committed at close. Azure caps a block blob at 50,000 blocks, which at 10 MiB is about 488 GiB, far more than an interval holds. | verified here (size and rows); from source ([`kBlockUploadSizeBytes`](https://github.com/apache/arrow/blob/apache-arrow-26.0.1/cpp/src/arrow/filesystem/azurefs.cc#L1036)) |
| Is a file visible while it is written? | Yes. Opening a stream creates an empty blob at the path: partway through an 11 MiB write, the path read as a file of 0 bytes. That is why the partition goes to a temporary directory. | verified here |
| Known open issues | A user-delegation SAS cannot write datasets: `GetProperties` on the container returns 403 ([#46085](https://github.com/apache/arrow/issues/46085), [#50073](https://github.com/apache/arrow/issues/50073)). A SAS without `?` fails ([#49972](https://github.com/apache/arrow/issues/49972)). Parallel transfers cannot be tuned ([#40035](https://github.com/apache/arrow/issues/40035)). `allow_not_found` is mishandled in selectors ([#40014](https://github.com/apache/arrow/issues/40014)). None of them blocks a key, an account or service SAS, or Entra. | from GitHub, open on 2026-11-07 |

**Best guess.** Use `pads.write_dataset(table, temporary, format="parquet", partitioning=<the typed hive schema>,
basename_template="part-{i}.parquet", existing_data_behavior="error", filesystem=fs)`, with a fresh temporary directory
for each run. Never use `pq.write_to_dataset`. Readers always pass the same typed partitioning.

### 5. Fallback

| Question | Answer | How known |
|---|---|---|
| Is adlfs, wrapped in `PyFileSystem(FSSpecHandler(...))` with azure-identity, the alternative? | **No, not for this step.** adlfs speaks only the Blob API. `FSSpecHandler.move` calls fsspec's `mv(recursive=True)`, which copies and then deletes, even with HNS. On Azurite, moving a partition directory copied the file to the destination, raised `FileNotFoundError`, and left the source in place. A second run nested the copy as `final/date=…/date=…/part-0.parquet`. That is the silent non-atomic copy basis must refuse. | verified here; from source (fsspec `spec.py` `mv`, `asyn.py` `_mv_file`; pyarrow `fs.py` `FSSpecHandler.move`) |
| What would it cost? | 26 packages besides pyarrow, about 36 MB. 12 of them are new to basis's lock: adlfs, fsspec, azure-storage-blob, isodate, and aiohttp with aiohappyeyeballs, aiosignal, attrs, frozenlist, multidict, propcache and yarl. azure-identity 1.27.0 and msal are already locked, through mssql-python 1.17.0. | verified here (installed in a scratch venv and compared with `uv.lock`) |

**Fallback for the no.** If pyarrow's Azure client falls short at the firm, in sign-in or in the rename, keep pyarrow
for writing Parquet and make the storage calls with `azure-storage-file-datalake` and Python's `azure-identity`, which
is already locked. `DataLakeDirectoryClient.rename_directory` is the same `Path - Create` rename, and Python's
`WorkloadIdentityCredential` and `AzureCliCredential` match the C++ ones. It costs three new packages, about 8 MB:
azure-storage-file-datalake 12.26.1, azure-storage-blob 12.31.1 and isodate. This comes from the SDK's docs and a
scratch install; it was not run against an HNS account.

**Best guess.** Build on AzureFileSystem. fsspec and adlfs stay out of basis, since they cannot rename.

### 6. Azure-side permissions and network

| Question | Answer | How known |
|---|---|---|
| Which role does the job's identity need? | Storage Blob Data Contributor on the container: "Read, write, and delete access to Blob storage containers and blobs." Role assignments are evaluated before ACLs, and "if the assignment grants sufficient access permission, ACLs are ignored", so no ACLs are needed. That the role also covers leases and reading the root's ACL, which the rename and the HNS check use, is inferred; step 1 of `lake_check.py` confirms it. | from docs ([access control model](https://learn.microsoft.com/en-us/azure/storage/blobs/data-lake-storage-access-control-model)) |
| ACLs instead? | Only if the firm grants access per directory. Then the identity needs `--X` on `/` and each ancestor, `-WX` on a parent to create in it, `R-X` to list, `RWX` on a directory and everything inside it to delete it, and a default ACL on the table's directory so that new partitions inherit. Default ACLs apply only to items created after them. The docs' table has no rename row; going by its create and delete rows, a rename needs `-WX` on both parents. | from docs ([ACLs](https://learn.microsoft.com/en-us/azure/storage/blobs/data-lake-storage-access-control)); the rename inferred |
| Containers | pyarrow's recursive `create_dir` creates a missing container, which a container-scoped role cannot do. basis should require the container to exist. | from source |
| Key and SAS | They work only if the account allows Shared Key, which Microsoft recommends disallowing. | from docs |
| Private endpoints | Both sub-resources need one: "If you create a private endpoint for the Data Lake Storage storage resource, then you should also create one for the Blob Storage resource… if you add a private endpoint for Blob Storage only, and not for Data Lake Storage, some operations (such as Manage ACL, Create Directory, Delete Directory, etc.) will fail." AzureFileSystem uses both endpoints: blob for reads, writes and leases, and dfs for the HNS check, directories and the rename. Clients keep the public hostnames, and DNS (`privatelink.blob.core.windows.net`, `privatelink.dfs.core.windows.net`) must answer with the private addresses. | from docs ([private endpoints](https://learn.microsoft.com/en-us/azure/storage/common/storage-private-endpoints)); the use of both endpoints verified here (SDK log) and from source |
| Firewall | The storage firewall governs the public endpoint only. Traffic from a VNet with a private endpoint "is always allowed, even if public network access is disabled". | from docs |
| Pods | Pods need egress to `login.microsoftonline.com` for the token exchange, besides storage; the exchange posts there (verified here). On Linux, libcurl "respects the proxy environment variables", `https_proxy` and `no_proxy` among them. If the pod has a proxy set, both storage hostnames must be in `NO_PROXY`, so that they reach the private endpoint. | from docs ([CURLOPT_PROXY](https://curl.se/libcurl/c/CURLOPT_PROXY.html)) |
| Citrix desktops | pyarrow on Windows uses WinHTTP with no proxy at all: `EnableSystemDefaultProxy{false}`, and Arrow passes no transport options. So the session must reach both storage hostnames directly, either through the private endpoint over the corporate network or through the public endpoint opened to Citrix's egress. `az` fetches the token itself, through its own proxy settings, so pyarrow never calls Entra from a desktop. WinHTTP uses the Windows certificate store. | from source ([`win_http_transport.hpp`](https://github.com/Azure/azure-sdk-for-cpp/blob/azure-core_1.16.4/sdk/core/azure-core/inc/azure/core/http/win_http_transport.hpp#L65), [`transport_policy.cpp`](https://github.com/Azure/azure-sdk-for-cpp/blob/azure-core_1.16.4/sdk/core/azure-core/src/http/transport_policy.cpp)); not run on Windows |

**Best guess.** Build to this setup:

- A user-assigned managed identity for each job namespace, federated to the jobs' service account, with Storage Blob
  Data Contributor on the lake container.
- An Entra group for desktops: Contributor in dev and QA, and Reader in prod, matching step 5.6's read-only prod login.
- Private endpoints for both `blob` and `dfs`.
- `basis doctor --env` resolves both hostnames and says whether each answer is a private address. It then signs in,
  checks that the container exists, and runs the scratch create, rename and delete.

## Probe

`probe.py`, run as `python -I probe.py 127.0.0.1:PORT` against Azurite:

```python
"""Spike 0.8 probe: an interval's Parquet partition through pyarrow.fs.AzureFileSystem, against Azurite.

    python -I probe.py HOST:PORT

Azurite has no hierarchical namespace, so this shows the flat-namespace behaviour. azure-storage-blob is used only to
list the raw blobs and to make a SAS token, never to write the archive."""

import sys
import uuid
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.fs as pafs
import pyarrow.parquet as pq
from azure.storage.blob import AccountSasPermissions, BlobServiceClient, ResourceTypes, generate_account_sas

AUTHORITY = sys.argv[1]
ACCOUNT = "devstoreaccount1"
KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="  # Azurite's public key
CONTAINER = "archive"
PARTITIONING = pads.partitioning(pa.schema([("date", pa.date32())]), flavor="hive")


def step(label, action):
    try:
        print(f"{label}: ok {action()!r}"[:400])
    except Exception as error:  # noqa: BLE001 - the probe reports every outcome
        print(f"{label}: {type(error).__module__}.{type(error).__name__}: {str(error).splitlines()[0][:300]}")


def raw_blobs(prefix):
    service = BlobServiceClient(f"http://{AUTHORITY}/{ACCOUNT}", credential={"account_name": ACCOUNT, "account_key": KEY})
    container = service.get_container_client(CONTAINER)
    return [(blob.name, blob.size, dict(blob.metadata or {})) for blob in container.list_blobs(name_starts_with=prefix.removeprefix(f"{CONTAINER}/"), include=["metadata"])]


def table(rows, day):
    return pa.table({"id": pa.array(range(rows), pa.int64()), "amount": pa.array([i / 100 for i in range(rows)], pa.float64()), "date": pa.array([day] * rows, pa.date32())})


fs = pafs.AzureFileSystem(ACCOUNT, account_key=KEY, blob_storage_authority=AUTHORITY, dfs_storage_authority=AUTHORITY, blob_storage_scheme="http", dfs_storage_scheme="http")
day = datetime(2026, 11, 7, tzinfo=UTC).date()
final = f"{CONTAINER}/trades"
temporary = f"{CONTAINER}/trades/.tmp-{uuid.uuid4().hex}"

step("create the container by create_dir", lambda: fs.create_dir(CONTAINER))

# 1. Write an interval's partition to a temporary directory with pyarrow.dataset.write_dataset.
data = table(1_000, day)
step(
    "write_dataset to the temporary path",
    lambda: pads.write_dataset(data, temporary, format="parquet", partitioning=PARTITIONING, filesystem=fs, basename_template="part-{i}.parquet", existing_data_behavior="error"),
)
step("raw blobs after write_dataset", lambda: raw_blobs(temporary))

# 2. Read it back, with the partition column typed, and check the row count.
step("read back and count", lambda: pads.dataset(temporary, filesystem=fs, format="parquet", partitioning=PARTITIONING).count_rows())
step("partition column type", lambda: pads.dataset(temporary, filesystem=fs, format="parquet", partitioning=PARTITIONING).to_table().schema.field("date").type)
step("hive discovery without a schema", lambda: pads.dataset(temporary, filesystem=fs, format="parquet", partitioning="hive").schema.field("date").type)

# 3. existing_data_behavior on a second write of the same interval.
step("second write, existing_data_behavior='error'", lambda: pads.write_dataset(data, temporary, format="parquet", partitioning=PARTITIONING, filesystem=fs, basename_template="part-{i}.parquet"))
step(
    "second write, 'overwrite_or_ignore'",
    lambda: pads.write_dataset(data, temporary, format="parquet", partitioning=PARTITIONING, filesystem=fs, basename_template="part-{i}.parquet", existing_data_behavior="overwrite_or_ignore"),
)
step(
    "second write, 'delete_matching'",
    lambda: pads.write_dataset(data, temporary, format="parquet", partitioning=PARTITIONING, filesystem=fs, basename_template="part-{i}.parquet", existing_data_behavior="delete_matching"),
)
step("rows after the second writes", lambda: pads.dataset(temporary, filesystem=fs, format="parquet", partitioning=PARTITIONING).count_rows())

# 4. pyarrow.parquet.write_to_dataset, the older entry point.
legacy = f"{CONTAINER}/legacy/.tmp-{uuid.uuid4().hex}"
step("pq.write_to_dataset", lambda: pq.write_to_dataset(data, legacy, partition_cols=["date"], filesystem=fs))
step("pq.write_to_dataset files", lambda: [(name, size) for name, size, _ in raw_blobs(legacy)])

# 5. A file of several 10 MiB blocks, written through the stream and read back.
big = table(3_000_000, day)
big_path = f"{CONTAINER}/big/.tmp-{uuid.uuid4().hex}/date=2026-11-07/part-0.parquet"
step("write a multi-block file", lambda: pq.write_table(big, big_path, filesystem=fs, compression="none"))
step("its size in bytes", lambda: fs.get_file_info(big_path).size)
step("its rows read back", lambda: pq.read_metadata(big_path, filesystem=fs).num_rows)

# 6. The rename: the partition directory onto its final path, then a single file.
step("move a directory (temporary partition onto the final one)", lambda: fs.move(f"{temporary}/date=2026-11-07", f"{final}/date=2026-11-07"))
step("move a single file", lambda: fs.move(f"{temporary}/date=2026-11-07/part-0.parquet", f"{final}/part-0.parquet"))
step("final path after the moves", lambda: fs.get_file_info(f"{final}/date=2026-11-07").type)
step("temporary partition after the moves", lambda: fs.get_file_info(f"{temporary}/date=2026-11-07").type)
step("copy_file (server-side copy) still works", lambda: fs.copy_file(f"{temporary}/date=2026-11-07/part-0.parquet", f"{CONTAINER}/copy/part-0.parquet"))
step("ArrowNotImplementedError is a NotImplementedError", lambda: issubclass(pa.ArrowNotImplementedError, NotImplementedError))

# 7. Readers of the table skip the hidden temporary directory: one partition written in place, the other still hidden.
step(
    "write 2026-11-06 straight to the final path",
    lambda: pads.write_dataset(
        table(500, day - timedelta(days=1)), final, format="parquet", partitioning=PARTITIONING, filesystem=fs, basename_template="part-{i}.parquet", existing_data_behavior="overwrite_or_ignore"
    ),
)
step("rows a reader of the table sees (500 if .tmp-* is skipped)", lambda: pads.dataset(final, filesystem=fs, format="parquet", partitioning=PARTITIONING).count_rows())

# 8. Empty-directory markers: create_dir, then delete the directory's only file.
step("create_dir writes", lambda: (fs.create_dir(f"{CONTAINER}/markers/date=2026-11-07"), raw_blobs(f"{CONTAINER}/markers"))[1])
step("delete_dir of the temporary tree", lambda: fs.delete_dir(temporary))
step("raw blobs left under it", lambda: raw_blobs(temporary))

# 9. Sign-in through the other routes basis would take: a SAS token, with and without '?', and from_uri.
sas = generate_account_sas(
    ACCOUNT,
    KEY,
    ResourceTypes(service=True, container=True, object=True),
    AccountSasPermissions(read=True, write=True, list=True, delete=True, create=True, add=True),
    expiry=datetime.now(UTC) + timedelta(hours=1),
)
common = {"blob_storage_authority": AUTHORITY, "dfs_storage_authority": AUTHORITY, "blob_storage_scheme": "http", "dfs_storage_scheme": "http"}
step("constructor, sas_token with '?'", lambda: pafs.AzureFileSystem(ACCOUNT, sas_token="?" + sas, **common).get_file_info(final).type)
step("constructor, sas_token without '?'", lambda: pafs.AzureFileSystem(ACCOUNT, sas_token=sas, **common).get_file_info(final).type)
step("from_uri with a SAS in the query", lambda: pafs.FileSystem.from_uri(f"abfs://{ACCOUNT}@{AUTHORITY}/{final}?enable_tls=false&{sas}")[0].get_file_info(final).type)
step("from_uri path", lambda: pafs.FileSystem.from_uri(f"abfs://{ACCOUNT}@{AUTHORITY}/{final}?enable_tls=false&{sas}")[1])
step("from_uri with account_key in the query", lambda: pafs.FileSystem.from_uri(f"abfs://{ACCOUNT}@{AUTHORITY}/{final}?enable_tls=false&account_key=x"))
step("pickled AzureFileSystem carries the key", lambda: KEY in str(fs.__reduce__()))
```

Its output:

```text
create the container by create_dir: ok None
write_dataset to the temporary path: ok None
raw blobs after write_dataset: ok [('trades/.tmp-3122…/', 0, {'is_directory': 'true'}), ('trades/.tmp-3122…/date=2026-11-07/', 0, {'is_directory': 'true'}), ('trades/.tmp-3122…/date=2026-11-07/part-0.parquet', 11438, {})]
read back and count: ok 1000
partition column type: ok DataType(date32[day])
hive discovery without a schema: ok DataType(string)
second write, existing_data_behavior='error': pyarrow.lib.ArrowInvalid: Could not write to archive/trades/.tmp-3122… as the directory is not empty and existing_data_behavior is to error
second write, 'overwrite_or_ignore': ok None
second write, 'delete_matching': ok None
rows after the second writes: ok 1000
pq.write_to_dataset: ok None
pq.write_to_dataset files: ok [('legacy/.tmp-b001…/', 0), ('legacy/.tmp-b001…/date=2026-11-07/', 0), ('legacy/.tmp-b001…/date=2026-11-07/38724aad68ca4af6aad606a5a5f61479-0.parquet', 11438)]
write a multi-block file: ok None
its size in bytes: ok 49658261
its rows read back: ok 3000000
move a directory (temporary partition onto the final one): pyarrow.lib.ArrowNotImplementedError: FileSystem::Move() is not implemented for Azure Storage accounts without Hierarchical Namespace support (see arrow/issues/40405).
move a single file: pyarrow.lib.ArrowNotImplementedError: FileSystem::Move() is not implemented for Azure Storage accounts without Hierarchical Namespace support (see arrow/issues/40405).
final path after the moves: ok <FileType.NotFound: 0>
temporary partition after the moves: ok <FileType.Directory: 3>
copy_file (server-side copy) still works: ok None
ArrowNotImplementedError is a NotImplementedError: ok True
write 2026-11-06 straight to the final path: ok None
rows a reader of the table sees (500 if .tmp-* is skipped): ok 500
create_dir writes: ok [('markers/', 0, {'is_directory': 'true'}), ('markers/date=2026-11-07/', 0, {'is_directory': 'true'})]
delete_dir of the temporary tree: ok None
raw blobs left under it: ok []
constructor, sas_token with '?': ok <FileType.Directory: 3>
constructor, sas_token without '?': builtins.OSError: ListBlobsByHierarchy failed for prefix='trades'. GetFileInfo is unable to determine whether the path exists. Azure Error: [OutOfRangeInput] 400 The specified resource name length is not within the permissible limits.
from_uri with a SAS in the query: ok <FileType.Directory: 3>
from_uri path: ok 'archive/trades'
from_uri with account_key in the query: pyarrow.lib.ArrowInvalid: Unexpected query parameter in Azure Blob File System URI: 'account_key'
pickled AzureFileSystem carries the key: ok True
```

Also checked against Azurite: a file is visible while it is written. An open stream with 11 MiB written read as `File 0`
before `close()`, and as `File 11534336` after it.

`cred_probe.py`, run as `PATH=FAKEBIN HOME=EMPTY AZURE_LOG_LEVEL=verbose python -I cred_probe.py MODE [TOKEN_FILE]`.
`FAKEBIN/az` is a shell script that appends its arguments to a log and prints `{"accessToken": "fake-cli-token",
"expiresOn": "2099-01-01 00:00:00.000000", "expires_on": 4070908800, "tokenType": "Bearer", …}`:

```python
"""Which sign-in each credential kind attempts, with no real sign-in: a stand-in `az` and made-up workload-identity ids.

The target is a public Microsoft account, which refuses the fake tokens with a 401: reaching that 401 shows the token
was fetched and sent over TLS. HOME is empty so that a real `az` on /usr/bin, which the SDK appends to PATH, has no
login to fall back on."""

import os
import sys
import time

import pyarrow.fs as pafs

ACCOUNT = "azureopendatastorage"


def attempt(label, make):
    start = time.monotonic()
    try:
        print(f"{label}: ok {make().get_file_info('nyctlc/x')}")
    except Exception as error:  # noqa: BLE001 - the probe reports every outcome
        print(f"{label}: {type(error).__name__} after {time.monotonic() - start:.1f}s: {str(error).splitlines()[0][:300]}")


if sys.argv[1] == "cli":
    attempt("credential_kind=cli", lambda: pafs.FileSystem.from_uri(f"abfs://nyctlc@{ACCOUNT}.dfs.core.windows.net/x?credential_kind=cli")[0])
    attempt("default chain, desktop", lambda: pafs.AzureFileSystem(ACCOUNT))
else:
    os.environ |= {"AZURE_TENANT_ID": "11111111-1111-1111-1111-111111111111", "AZURE_CLIENT_ID": "22222222-2222-2222-2222-222222222222", "AZURE_FEDERATED_TOKEN_FILE": sys.argv[2]}
    attempt("credential_kind=workload_identity", lambda: pafs.FileSystem.from_uri(f"abfs://nyctlc@{ACCOUNT}.dfs.core.windows.net/x?credential_kind=workload_identity")[0])
    attempt("default chain, pod environment", lambda: pafs.AzureFileSystem(ACCOUNT))
```

The lines that matter, from its output and the SDK's log:

```text
credential_kind=cli: OSError after 0.2s: Check for Hierarchical Namespace support on 'https://azureopendatastorage.blob.core.windows.net/nyctlc' failed. Azure Error: [InvalidAuthenticationInfo] 401 …
Identity: DefaultAzureCredential: Created with the following credentials: EnvironmentCredential, WorkloadIdentityCredential, ManagedIdentityCredential, AzureCliCredential.
HTTP Request : GET http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=REDACTED
HTTP Request : HEAD https://azureopendatastorage.dfs.core.windows.net/nyctlc/?action=REDACTED
user-agent : azsdk-cpp-identity/1.13.3 (Linux 6.12.115+deb13.1-amd64 x86_64 …)
credential_kind=workload_identity: ArrowException after 0.1s: Unknown error: Check for Hierarchical Namespace support on '…' failed: N5Azure4Core11Credentials23AuthenticationExceptionE: GetToken(): error response: 401 Unauthorized
HTTP Request : POST https://login.microsoftonline.com/11111111-1111-1111-1111-111111111111/oauth2/v2.0/token
Identity: DefaultAzureCredential: Failed to get token from WorkloadIdentityCredential: GetToken(): error response: 401 Unauthorized
Identity: DefaultAzureCredential: Failed to get token from ManagedIdentityCredential: GetToken(): Fail to get a new connection for: http://169.254.169.254. Could not connect to server
Identity: DefaultAzureCredential: Successfully got token from AzureCliCredential. This credential will be reused for subsequent calls.
az calls: account get-access-token --output json --scope https://storage.azure.com/.default   (three, one per CLI sign-in)
```

`adlfs_probe.py`, run with adlfs 2026.8.0 against Azurite:

```python
blob = adlfs.AzureBlobFileSystem(connection_string=(f"DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey={KEY};BlobEndpoint=http://{AUTHORITY}/devstoreaccount1;"))
fs = pafs.PyFileSystem(pafs.FSSpecHandler(blob))
temporary, final = f"archive/viaadlfs/.tmp-{uuid.uuid4().hex}", "archive/viaadlfs/final"
data = pa.table({"id": range(1000), "date": pa.array(["2026-11-07"] * 1000)})
pads.write_dataset(data, temporary, format="parquet", partitioning=["date"], partitioning_flavor="hive", filesystem=fs, basename_template="part-{i}.parquet")
fs.move(f"{temporary}/date=2026-11-07", f"{final}/date=2026-11-07")  # in a try that prints the error
print(blob.find(final), blob.find(temporary))
```

```text
move: FileNotFoundError [Errno 2] No such file or directory: 'archive/viaadlfs/.tmp-4f7f…/date=2026-11-07'
after move, final: ['archive/viaadlfs/final/date=2026-11-07/date=2026-11-07/part-0.parquet', 'archive/viaadlfs/final/date=2026-11-07/part-0.parquet']
after move, temporary: ['archive/viaadlfs/.tmp-4f7f…/date=2026-11-07/part-0.parquet']
```

## (work): to run at the firm

A short checklist that confirms the assumptions at the port, rather than a blocker. `lake_check.py` is below, and was
rehearsed against Azurite, where step 3 refuses as expected.

1. **The account**, as anyone with read access to it:

   ```sh
   az storage account show -n ACCOUNT -g RG --query "{hns:isHnsEnabled, sharedKey:allowSharedKeyAccess, publicAccess:publicNetworkAccess, firewall:networkRuleSet.defaultAction, privateEndpoints:privateEndpointConnections[].privateEndpoint.id}" -o jsonc
   ```

   Expect `hns: true`. `sharedKey: false` rules out the key and SAS options.

2. **The role assignments** on the container:

   ```sh
   SCOPE="$(az storage account show -n ACCOUNT -g RG --query id -o tsv)/blobServices/default/containers/CONTAINER"
   az role assignment list --scope "$SCOPE" --include-inherited --query "[?starts_with(roleDefinitionName, 'Storage Blob Data')].{role:roleDefinitionName, who:principalName, type:principalType, scope:scope}" -o table
   ```

   Expect Storage Blob Data Contributor for the jobs' identity and for the desktop group, at the container or account
   scope.

3. **DNS**, from a desktop and from a pod: `nslookup ACCOUNT.blob.core.windows.net` and `nslookup
   ACCOUNT.dfs.core.windows.net`. Private addresses mean the private endpoints are in use.

4. **A desktop**, after `az login`, in PowerShell on Citrix and again in WSL:

   ```sh
   az account get-access-token --scope https://storage.azure.com/.default --query expiresOn -o tsv
   uv run --no-project --with pyarrow==26.0.1 python lake_check.py ACCOUNT CONTAINER cli
   ```

5. **A test pod with workload identity.** Use the jobs' existing service account if it is already federated. If not,
   create one and federate it with `az identity federated-credential create --name spike08 --identity-name IDENTITY -g
   RG --issuer "$(az aks show -n CLUSTER -g RG --query oidcIssuerProfile.issuerUrl -o tsv)" --subject
   system:serviceaccount:NAMESPACE:spike08 --audiences api://AzureADTokenExchange`. `IMAGE` is any image with Python
   3.14 and pyarrow 26.0.1, such as the pilot's job image.

   ```yaml
   apiVersion: v1
   kind: ServiceAccount
   metadata:
     name: spike08
     namespace: NAMESPACE
     annotations:
       azure.workload.identity/client-id: CLIENT_ID   # the identity that holds the role
   ---
   apiVersion: v1
   kind: Pod
   metadata:
     name: spike08
     namespace: NAMESPACE
     labels:
       azure.workload.identity/use: "true"
   spec:
     serviceAccountName: spike08
     restartPolicy: Never
     containers:
       - name: python
         image: IMAGE
         command: ["sleep", "3600"]
   ```

   ```sh
   kubectl apply -f spike08.yaml
   kubectl cp lake_check.py NAMESPACE/spike08:/tmp/lake_check.py
   kubectl exec -n NAMESPACE spike08 -- python /tmp/lake_check.py ACCOUNT CONTAINER workload_identity
   kubectl exec -n NAMESPACE spike08 -- sh -c 'env | grep -i proxy'
   kubectl delete -f spike08.yaml
   ```

6. **What to send back:**
   - the JSON from step 1 and the table from step 2;
   - the four `nslookup` answers;
   - the output of `lake_check.py` from Windows, WSL and the pod. Expect: 1 `Directory`; 2 ok; 3 ok; 4 `1000`; 5 ok; 6
     FAILED with an `OSError` saying the directory is not empty; 7 ok. Send any other FAILED line in full;
   - the pod's proxy variables.

`lake_check.py`:

```python
"""Spike 0.8 at the firm: sign in, write one partition under a hidden name, rename it into place, read it back.

    python lake_check.py ACCOUNT CONTAINER cli                  # a desktop, after `az login`
    python lake_check.py ACCOUNT CONTAINER workload_identity    # a pod labelled azure.workload.identity/use=true

Everything goes under CONTAINER/basis-spike08/, which the script deletes at the end."""

import os
import platform
import sys
import time
import uuid

import pyarrow as pa
import pyarrow.dataset as pads
import pyarrow.fs as pafs

account, container, kind = sys.argv[1:4]
if kind == "azurite":  # a local rehearsal: ACCOUNT is Azurite's HOST:PORT
    fs = pafs.AzureFileSystem(
        "devstoreaccount1",
        account_key="Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==",
        blob_storage_authority=account,
        dfs_storage_authority=account,
        blob_storage_scheme="http",
        dfs_storage_scheme="http",
    )
else:
    fs, _ = pafs.FileSystem.from_uri(f"abfs://{container}@{account}.dfs.core.windows.net/?credential_kind={kind}")
root = f"{container}/basis-spike08"
temporary = f"{root}/.tmp-{uuid.uuid4().hex}"
partition = "date=2026-11-07"
table = pa.table({"id": pa.array(range(1000), pa.int64()), "date": pa.array(["2026-11-07"] * 1000).cast(pa.date32())})
schema = pads.partitioning(pa.schema([("date", pa.date32())]), flavor="hive")

print(f"pyarrow {pa.__version__}, Python {platform.python_version()}, {platform.platform()}, credential_kind={kind}")
print("workload identity variables set:", {name: name in os.environ for name in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE", "AZURE_AUTHORITY_HOST")})


def step(label, action):
    start = time.monotonic()
    try:
        result = action()
        print(f"{label}: ok in {time.monotonic() - start:.2f}s {'' if result is None else result}")
    except Exception as error:  # noqa: BLE001 - every outcome is the answer
        print(f"{label}: FAILED in {time.monotonic() - start:.2f}s, {type(error).__name__}: {str(error).splitlines()[0][:400]}")


step("1 sign in and see the container", lambda: fs.get_file_info(container).type.name)
step("2 write the partition under a hidden name", lambda: pads.write_dataset(table, temporary, format="parquet", partitioning=schema, filesystem=fs, basename_template="part-{i}.parquet"))
step("3 rename it into place (atomic only with HNS)", lambda: fs.move(f"{temporary}/{partition}", f"{root}/{partition}"))
step("4 read it back", lambda: pads.dataset(root, format="parquet", partitioning=schema, filesystem=fs).count_rows())
step(
    "5 write the same interval again",
    lambda: pads.write_dataset(table, temporary, format="parquet", partitioning=schema, filesystem=fs, basename_template="part-{i}.parquet", existing_data_behavior="overwrite_or_ignore"),
)
step("6 rename onto the archived partition (should refuse)", lambda: fs.move(f"{temporary}/{partition}", f"{root}/{partition}"))
step("7 delete everything under basis-spike08", lambda: fs.delete_dir(root))
```
