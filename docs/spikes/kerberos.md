# Spike 0.7: Kerberos

basis can build step 5.6 now. SQL Server Kerberos works with mssql-python's `Trusted_Connection=yes` on all three
machines. On Windows it uses the logon, with no setup. In WSL and in pods it uses MIT GSSAPI, whose libraries the
managed image already installs. A pod needs no `kinit` and no sidecar for SQL Server: when `KRB5_CLIENT_KTNAME` names a
keytab, GSSAPI gets the ticket itself and refreshes it. Oracle Kerberos is Thick mode only in python-oracledb 26.1.0. So
it needs Oracle Instant Client, a `sqlnet.ora`, and a ticket from `kinit`. On Windows it is the weakest path.

basis passes the SQL Server connection string through unchanged. It adds external authentication to the Oracle settings.
`basis doctor --env` turns the drivers' Kerberos errors into one `ERROR` line with the fix, and it never runs `klist
-s`. Researched on 2026-11-07, with no KDC and no database server. The firm-specific parts are a checklist for the port.

"From docs" means the vendor's published docs, linked at each claim. "Verified here" means I checked it on this Debian
13 host, or in a throwaway `python:3.14-slim` container with the managed image's packages. Both were run against a fake
SQL Server endpoint on localhost and a realm whose KDC is a closed local port, so nothing real was contacted. The
endpoint is a 40-line Python listener. It answers SQL Server's PRELOGIN message with "encryption not supported", so the
driver goes on to build its login. That is the point where the driver asks GSSAPI for a Kerberos token. A password login
sent its LOGIN7 packet to the listener, which shows the listener works. Every Kerberos login stopped before LOGIN7. The
probe scripts are kept in
`/tmp/claude-1000/-home-jacob-dev/a0e5725e-460f-43fd-9986-1b0127454772/scratchpad/spike07-scripts/`. The scratch venv,
the clones, the Instant Client download and the pulled image are deleted.

## Versions

- **mssql-python 1.17.0** (uv.lock). It ships with two providers:
  - mssql-python-odbc 18.6.2.2, which is ODBC Driver 18.6 and the default provider;
  - mssql-python-rs 0.4.0, the preview Rust driver, `mssql-odbc`, chosen with `MSSQL_PYTHON_NATIVE_PROVIDER=mssql-odbc`.
    The wiki page describes 0.3.0.
- **oracledb 26.1.0** (uv.lock). Docs read at `v26.1.0`.
- **Oracle Instant Client 23.26.2.0.0 Basic Light**, Linux x64, from the `instantclient-basiclite-linuxx64.zip`
  permalink (2026-06-05).
- **MIT krb5 1.21.3.** These are Debian 13's `libkrb5-3`, `libgssapi-krb5-2` and `krb5-user`, `1.21.3-5+deb13u1`. MIT
  docs were read at 1.22.2, the latest release.
- **Python:** 3.14.7 on the host. The image test used `python:3.14-slim`, which is Python 3.14.10 on Debian 13.
- **basis:** `main` at 6d29ec6.

## Answers

| # | Question | Answer | How known |
|---|---|---|---|
| 1 | Does mssql-python do Kerberos on Linux, and so in WSL and pods? | Yes, with `Trusted_Connection=yes`, in both providers. | from docs; verified here up to the KDC |
| 1 | Which keywords? | `Trusted_Connection=yes`. Not `Authentication=ActiveDirectoryIntegrated`, which is Entra, for Azure SQL. Not `Integrated Security=SSPI`, which mssql-python refuses. | from docs; the refusal verified here |
| 1 | Does it need system packages? | Yes: `libkrb5-3` and `libgssapi-krb5-2`, for every login, passwords too. The managed image has them already. `krb5-user` (kinit, klist) is needed only to get a ticket by hand. | verified here (readelf); from docs |
| 1 | Does it need a ticket cache from `kinit`? | No. A cache from `kinit` works, and so does a client keytab named by `KRB5_CLIENT_KTNAME`, from which GSSAPI gets the ticket with no `kinit`. | from docs; the keytab path verified here up to the KDC |
| 1 | On Windows, does `Trusted_Connection=yes` use the logged-in user with no setup? | Yes, through SSPI. It is Kerberos when the server's SPN is registered, and NTLM, silently, when it isn't. | from docs |
| 2 | Is Oracle Kerberos Thick mode only in 26.1.0? | Yes. Thin mode has no Kerberos. | from docs; no Kerberos code in thin mode, verified here |
| 2 | Does Thick mode work in the pod image? | Yes, once `libaio1t64` and a `libaio.so.1` symlink are added. The zip is 76 MB, 171 MB unzipped. | verified here |
| 2 | Does Thick mode work on Windows? | Probably, with the Instant Client zip, the VC++ runtime and `sqlnet.ora` set to `OSMSFT://`. Credential Guard may block it. | from docs; not verified |
| 3 | Is `kinit` at a container's start enough for a short Airflow job? | Yes. SQL Server doesn't even need the `kinit`, only the keytab's environment variables. | from docs; verified here up to the KDC |
| 3 | Does a long-running API need a sidecar that renews the ticket? | Not for SQL Server: GSSAPI refreshes from the keytab. For Oracle, yes, unless the port shows its client reads the keytab too. | from docs; not verified (needs a KDC) |
| 4 | What is the smallest WSL recipe? | `krb5-user`, an `/etc/krb5.conf` that names the realm, and `kinit you@REALM` each workday. | from docs |
| 5 | Should basis check `klist -s`? | No. `doctor --env` already logs in; basis turns that login's Kerberos errors into an `ERROR` line with the fix. | verified here (the error texts) |
| 5 | Should the managed Dockerfile install krb5? | The libraries always, as it does today. `krb5-user` never by default; a repo that needs it lists it in `apt-packages`. | verified here |

## 1. mssql-python and Kerberos

**Linux, so WSL and pods.**

- The ODBC driver's Linux page says: "You can enable Kerberos integrated authentication by specifying
  **Trusted_Connection=yes**". It says the driver "works with ... GSSAPI and Kerberos v5 libraries", and that "If
  Kerberos authentication fails, the ODBC driver on Linux or macOS does not use NTLM authentication" ([Using Integrated
  Authentication](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/using-integrated-authentication),
  2026-08-26).
- mssql-python's preview Rust driver lists "Windows authentication (`Trusted_Connection=Yes`, using SSPI on Windows or
  Kerberos on Linux and macOS)" ([Using the mssql-odbc driver
  (Preview)](https://github.com/microsoft/mssql-python/wiki/Using-the-mssql%E2%80%90odbc-driver-(Preview))).
- mssql-python checks each keyword against an allow-list (`_ALLOWED_CONNECTION_STRING_PARAMS` in `constants.py`). The
  list includes `trusted_connection`, `authentication` and `serverspn` ([Connection to SQL
  Database](https://github.com/microsoft/mssql-python/wiki/Connection-to-SQL-Database)).

**Keywords.**

- Use `Trusted_Connection=yes`. Verified here: `Integrated Security=SSPI` fails before any network call, with
  `ConnectionStringParseError: Unknown keyword 'integrated security' is not recognized`. So does `Connect Timeout`.
- `Authentication=ActiveDirectoryIntegrated` is "Microsoft Entra integrated authentication" for a federated account
  ([keywords](https://learn.microsoft.com/en-us/sql/connect/odbc/dsn-connection-string-attribute); [Entra ID
  support](https://github.com/microsoft/mssql-python/wiki/Microsoft-Entra-ID-support)). It is not on-premises SQL Server
  Kerberos.
- `ServerSPN`: mssql-python accepts it, and the keyword table lists it on Linux. The Linux integrated-auth page says
  "**ServerSPN** and **FailoverPartnerSPN** are not supported". The docs contradict each other, so basis doesn't rely on
  it. Put the FQDN that the SPN names in `Server=`.

**Packages.**

- Verified here (readelf): ODBC Driver 18.6's `libmsodbcsql-18.6.so.2.1` has `NEEDED libkrb5.so.3` and
  `libgssapi_krb5.so.2`. So the default driver doesn't load without them, for any login.
- The Rust driver loads `libgssapi_krb5` only when it needs it, according to its strings.
- mssql-python's README installs `libltdl7 libkrb5-3 libgssapi-krb5-2` on Debian/Ubuntu
  ([README](https://github.com/microsoft/mssql-python/blob/main/README.md)). Its wiki adds `krb5-user` "for obtaining
  and managing Kerberos tickets" ([Installation](https://github.com/microsoft/mssql-python/wiki/Installation)).
- The managed Dockerfile installs the first three already.

**The ticket.**

- Microsoft says to "use `kinit` or PAM ... to obtain and cache the TGT", and "The ODBC driver does not renew
  credentials itself" ([Linux integrated
  auth](https://learn.microsoft.com/en-us/sql/connect/odbc/linux-mac/using-integrated-authentication)).
- MIT GSSAPI does more than that page says:
  - "If the default cache doesn't exist but the default client keytab does, the krb5 mechanism will try to acquire
    initial tickets for the first principal in the default client keytab". Those tickets "will be refreshed by future
    calls to gss_acquire_cred as they approach their expire time" ([MIT
    GSSAPI](https://web.mit.edu/kerberos/krb5-latest/doc/appdev/gssapi.html)).
  - The client keytab is named by `KRB5_CLIENT_KTNAME` ([MIT
    keytab](https://web.mit.edu/kerberos/krb5-latest/doc/basic/keytab_def.html)).
- Verified here: with only `KRB5_CONFIG`, `KRB5_CLIENT_KTNAME` and `KRB5CCNAME` set, and no `kinit`, both providers went
  to the KDC by themselves. The `KRB5_TRACE` log showed:
  - `Getting initial credentials for svc_basis@CORP.EXAMPLE.COM`;
  - `Found entries for svc_basis@CORP.EXAMPLE.COM in keytab: aes256-cts`;
  - `Sending request (197 bytes) to CORP.EXAMPLE.COM`.

  A `MEMORY:` cache behaved the same. Whether the KDC then grants the ticket, and the refresh near expiry, need a real
  KDC. They are on the checklist.
- A pooled connection outlives its ticket, since Kerberos is used only at login. mssql-python pools by default. A new
  login needs a valid ticket.

**Windows.**

- "When 'yes', instructs the ... ODBC driver to use Windows Authentication Mode for login validation"
  ([keywords](https://learn.microsoft.com/en-us/sql/relational-databases/native-client/applications/using-connection-string-keywords-with-sql-server-native-client)).
- That uses the logged-on user's SSPI credentials: no `kinit`, no `krb5.conf`, no packages.
- The catch: "In an Active Directory environment, Kerberos authentication is always attempted first", and "If a security
  token can't be obtained, authentication uses NTLM" ([Register an
  SPN](https://learn.microsoft.com/en-us/sql/database-engine/configure-windows/register-a-service-principal-name-for-kerberos-connections)).
- So a server with no SPN works from a Citrix desktop by NTLM, and fails from WSL and pods, which have no NTLM. `SELECT
  auth_scheme FROM sys.dm_exec_connections WHERE session_id = @@SPID` tells which one a login used.

**What a missing or broken ticket looks like (verified here).** Each of these is
`mssql_python.exceptions.OperationalError`.

| Situation | Default provider (msodbcsql18) message | Rust provider |
|---|---|---|
| No ticket, no keytab | `Driver Error: General error; DDBC Error: [Microsoft]SSPI Provider: No credentials were supplied, or the credentials were unavailable or inaccessible. No Kerberos credentials available (default cache: FILE:/tmp/krb5cc_10001)` | `... [Microsoft]Security error: GSSAPI error (major=0x00070000, minor=0x96C73A8D): No Kerberos credentials available. Run 'kinit' to obtain a ticket. ...` |
| Keytab set, KDC refuses | `... SSPI Provider: ... Cannot contact any KDC for realm 'CORP.EXAMPLE.COM'` | same MIT text, after `GSSAPI error (...)` |
| Keytab set, no KDC in DNS | `... SSPI Provider: ... Cannot find KDC for realm "CORP.EXAMPLE.COM"` | not tried |
| Keytab unreadable (root-owned `0400`, the process uid 10001) | the same as no ticket: no word about the keytab | not tried |
| Server unreachable | `Client unable to establish connection; DDBC Error: [Microsoft]TCP Provider: Error code 0x2749` (refused), `0x2AF9` (no such host) | not tried |

Without a server that answers, there is no Kerberos error at all, so the network check has to come first. The default
cache in the managed image, as uid 10001, is `FILE:/tmp/krb5cc_10001`. Both providers end with MIT's own message, so one
substring match covers both.

These are expected with a real KDC but not reproduced here: `Clock skew too great`, `Server not found in Kerberos
database` (no SPN for that name), `Client not found in Kerberos database`, `Preauthentication failed` (a wrong password
or a stale keytab) and `Ticket expired`. They are MIT's error-table texts.

**Best guess.** mssql-python needs no change and no code in basis. The docs and `.env.example` show the Kerberos form:

```text
BASIS_DATABASES__WAREHOUSE__CONNECTION_STRING=Server=tcp:sql01.corp.example.com,1433;Database=dw;Trusted_Connection=yes;Encrypt=yes
```

They say that with Kerberos a machine's read and write logins are one identity, the ticket's principal. So for a
Kerberos database, the read-only prod login for desktops is the user's own AD grants.

**Fallback.** For a server whose SPN is missing or wrong, the DBA registers `MSSQLSvc/<fqdn>:<port>`. Until then, a SQL
login with a password, as the firm's other logins have.

## 2. python-oracledb and Kerberos

**Thick only.**

- The v26.1.0 feature table lists "Kerberos and Radius authentication | No | Yes", for Thin and Thick ([Appendix A,
  v26.1.0](https://python-oracledb.readthedocs.io/en/v26.1.0/user_guide/appendix_a.html)).
- The authentication chapter says: "Connecting to Oracle Database using external authentication is only supported in
  python-oracledb Thick mode". The only exception is TLS-certificate external authentication ([Authentication methods,
  v26.1.0](https://python-oracledb.readthedocs.io/en/v26.1.0/user_guide/authentication_methods.html)).
- Verified here: the thin implementation has no Kerberos, krb5 or GSSAPI strings. `externalauth=True` in Thin mode does
  not fail fast. It tried to connect (`DPY-6005` against a closed port), so basis must refuse the combination itself.

**What a Kerberos login is.** `oracledb.connect(dsn=..., externalauth=True)`, with no user and no password (the docs'
example), after `oracledb.init_oracle_client()`. The client's `sqlnet.ora` ([Oracle 23
Kerberos](https://docs.oracle.com/en/database/oracle/oracle-database/23/dbseg/configuring-kerberos-authentication.html),
[sqlnet.ora
reference](https://docs.oracle.com/en/database/oracle/oracle-database/23/netrf/parameters-for-the-sqlnet.ora.html)):

```text
SQLNET.AUTHENTICATION_SERVICES = (KERBEROS5)
SQLNET.AUTHENTICATION_KERBEROS5_SERVICE = oracle
SQLNET.KERBEROS5_CONF = /etc/krb5.conf
SQLNET.KERBEROS5_CONF_MIT = TRUE
SQLNET.KERBEROS5_CC_NAME = /tmp/krb5cc_10001
```

- Oracle's own defaults don't match MIT's. Its `krb.conf` default is `/etc/krb.conf`, or `c:\krb5\krb.conf` on Windows,
  so `KERBEROS5_CONF` must be set.
- Its cache default is `/tmp/krb5cc_userid`, or `c:\tmp\krbcache` on Windows. So `KERBEROS5_CC_NAME` must match
  `KRB5CCNAME` wherever that isn't the default.
- The database's principal is `oracle/<db-host-fqdn>@REALM`.
- Thick mode finds `sqlnet.ora` in `config_dir`, in `TNS_ADMIN`, or in Instant Client's `network/admin`
  ([Initialization](https://python-oracledb.readthedocs.io/en/v26.1.0/user_guide/initialization.html)).

**The process is all Thin or all Thick.** "All connections in an application use the same mode", and Thick can't go
back. So one Oracle database with Kerberos makes every Oracle database in that process Thick.

- basis's settings have `mode` per database.
- `databases/oracle.py` calls `init_oracle_client()` at each Thick connect.
- A thin connection made before it fails with oracledb's "python-oracledb thick mode cannot be used because thin mode
  has already been enabled" (in `errors.py`, verified here).

**Cost in a pod image (verified here, in `python:3.14-slim`, Debian 13).**

- **Download:** Instant Client 23.26 Basic Light is a 75.8 MB zip, 171 MB unzipped. Basic is 135.6 MB zipped. Basic
  Light's `libclntsh.so.23.1` carries the Kerberos adapter: its strings include `KERBEROS5_CC_NAME` and
  `gss_init_sec_context`.
- **libaio:** `libclntsh.so` needs `libaio.so.1`. Debian 13 ships `libaio1t64`, which provides `libaio.so.1t64`. Without
  it and the symlink, `init_oracle_client()` fails with `DPI-1047`.

  ```text
  apt-get install --yes --no-install-recommends libaio1t64
  ln -s /usr/lib/x86_64-linux-gnu/libaio.so.1t64 /usr/lib/x86_64-linux-gnu/libaio.so.1
  echo /opt/oracle/instantclient_23_26 > /etc/ld.so.conf.d/oracle-instantclient.conf && ldconfig
  ```

  python-oracledb's install guide describes the symlink, and prefers `ldconfig` over `LD_LIBRARY_PATH`
  ([Installation](https://python-oracledb.readthedocs.io/en/v26.1.0/user_guide/installation.html)). After that,
  `init_oracle_client()` reported client `(23, 26, 2, 0, 0)`.
- **Ticket:** a `kinit` (`krb5-user`), since Oracle's Kerberos is built into `libclntsh` and reads a cache file. Its
  binary holds MIT's client-keytab strings, so `KRB5_CLIENT_KTNAME` might work without `kinit`. That is unverified and
  on the checklist.
- **Licence:** "Oracle Free Distribution, Hosting, and Use Terms" (`BASIC_LITE_LICENSE` in the zip), which allows
  shipping it in an image.

**Cost on Windows (from docs; not verified).**

- Unzip Basic or Basic Light into a user folder, with no admin rights. It needs the VC++ redistributable "for Visual
  Studio 2015, 2017, 2019, and 2022", which takes an admin to install but is often present already.
- Then call `init_oracle_client(lib_dir=r"C:\...\instantclient_23_26")`. basis's `client_lib_dir` does that today.
- `sqlnet.ora` sets `SQLNET.KERBEROS5_CC_NAME=OSMSFT://`, the logon's ticket, so "the user does not need to use
  `okinit`", and `KERBEROS5_CONF` names a `krb5.conf` file.
- The risk: reading the logon's TGT needs Windows to export its session key, and "With active Credential Guard in
  Windows 10 and later versions of Windows, you can't enable sharing the TGT session keys with applications anymore"
  ([Kerberos registry
  entries](https://learn.microsoft.com/en-us/troubleshoot/windows-server/windows-security/kerberos-protocol-registry-kdc-configuration-keys)).

**Best guess.**

1. Add `externalauth: bool = False` (python-oracledb's name) to `OracleSettings` and `OracleWrite`. When it is true:
   - `user` and `password` are not set;
   - `mode` must be `thick`;
   - `connect` passes `externalauth=True`.

   A settings error with the fix covers each mistake.
2. Refuse settings whose Oracle databases differ in `mode`, with one `ERROR` line: set every Oracle database's `MODE` to
   `thick`.
3. Add an opt-in `[tool.basis] oracle-client` that `basis sync` writes into the managed Dockerfile. It installs Instant
   Client Basic Light at a pinned version, checked against its SHA-256, with `libaio1t64`, the symlink and `ldconfig`.
   It is off by default, so other images stay as they are. Without it no repo can use Thick mode in a pod at all, since
   repos never edit that file. This covers native network encryption too.
4. `sqlnet.ora` comes from the deployment, through `TNS_ADMIN` and a mounted ConfigMap. basis doesn't write it.

**Fallbacks.**

- **Windows desktops (Credential Guard, or no VC++ runtime):** they use the database's password login, or run from WSL.
  basis's docs say so.
- **The build agent can't reach download.oracle.com:** the firm's feed hosts the zip.
- **Oracle won't read the client keytab:** `kinit` at start, as in section 3.

## 3. Pods

**The pieces.**

- **Keytab:** an AD service account per environment, such as `svc_basis_qa`, and its keytab, from the AD team
  (`ktpass`), or with `ktutil`: `addent -password -p svc_basis_qa@CORP.EXAMPLE.COM -k 1 -e aes256-cts-hmac-sha1-96 -f`,
  then `wkt client.keytab` (`-f`: "salt information will be fetched from the KDC",
  [ktutil](https://web.mit.edu/kerberos/krb5-latest/doc/admin/admin_commands/ktutil.html)). A keytab is as good as the
  password: it is a Kubernetes secret, and it goes stale when the account's password changes.
- **Mounting:** a secret volume, readable by uid 10001. Verified here: a keytab the process can't read fails silently as
  "No Kerberos credentials available", so the pod sets `fsGroup: 10001` and `defaultMode: 0440` ([Kubernetes security
  context](https://kubernetes.io/docs/tasks/configure-pod-container/security-context/)).
- **`krb5.conf`:** a ConfigMap, mounted anywhere and named by `KRB5_CONFIG`, which overrides `/etc/krb5.conf` ([MIT
  environment](https://web.mit.edu/kerberos/krb5-latest/doc/user/user_config/kerberos.html)).
- **`KRB5CCNAME`:** `FILE:/tmp/krb5cc_basis`. The image's `/tmp` is writable; with `readOnlyRootFilesystem`, mount an
  `emptyDir` there. A `kinit` sidecar and the app share that `emptyDir`.
- **`KRB5_CLIENT_KTNAME`:** the keytab's path. For SQL Server that is all: GSSAPI gets the ticket at the first login and
  refreshes it later (section 1).
- **DNS and SPN:**
  - The pod must resolve the AD domain's SRV records (`_kerberos._tcp.CORP.EXAMPLE.COM`), or `krb5.conf` lists `kdc =`
    lines.
  - It must reach the domain controllers on port 88, TCP and UDP.
  - `Server=` must be the FQDN of a registered SPN (`MSSQLSvc/<fqdn>:<port>`, or `oracle/<fqdn>` for Oracle), not an IP
    or an alias.
  - `rdns = false` stops a wrong PTR record from changing the name.
  - The clock is the node's, and AKS syncs it.
- **Long-running API with Oracle:** a native sidecar, which is an init container with `restartPolicy: Always`, on by
  default since Kubernetes 1.29 and stable in 1.33. It runs a `kinit -k -t` loop into the shared cache. "The sidecar
  container in each Pod does not prevent the Job from completing" ([Sidecar
  containers](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/)).

**A short Airflow job.** `kinit` at the start is enough when the job ends within the ticket's lifetime. AD's default is
10 hours ([Maximum lifetime for user
ticket](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/security/threat-protection/security-policy-settings/maximum-lifetime-for-user-ticket)).
For SQL Server it isn't needed at all.

**Best guess: the smallest recipe.** The same pod spec serves a Deployment and the pod a KubernetesPodOperator makes:

```yaml
spec:
  securityContext: {fsGroup: 10001}
  volumes:
    - name: krb5-keytab
      secret: {secretName: basis-krb5-qa, defaultMode: 0440}   # key: client.keytab
    - name: krb5-conf
      configMap: {name: basis-krb5-conf}                       # key: krb5.conf
  containers:
    - name: job
      image: REPO_IMAGE
      args: ["run", "JOB"]
      env:
        - {name: KRB5_CONFIG, value: /etc/basis/krb5/krb5.conf}
        - {name: KRB5_CLIENT_KTNAME, value: /etc/basis/keytab/client.keytab}
        - {name: KRB5CCNAME, value: FILE:/tmp/krb5cc_basis}
      volumeMounts:
        - {name: krb5-keytab, mountPath: /etc/basis/keytab, readOnly: true}
        - {name: krb5-conf, mountPath: /etc/basis/krb5, readOnly: true}
```

```text
[libdefaults]
    default_realm = CORP.EXAMPLE.COM
    dns_lookup_kdc = true
    dns_lookup_realm = false
    rdns = false
[domain_realm]
    .corp.example.com = CORP.EXAMPLE.COM
    corp.example.com = CORP.EXAMPLE.COM
```

An Oracle job adds three things:

- `krb5-user` in `apt-packages`;
- `TNS_ADMIN` pointing at a mounted `sqlnet.ora`;
- `command: ["tini", "--", "sh", "-c", "kinit -k -t \"$KRB5_CLIENT_KTNAME\" svc_basis_qa@CORP.EXAMPLE.COM && exec basis
  run JOB"]`.

An Oracle API puts that `kinit` in a sidecar loop instead. basis builds none of this; its deploy docs show it.

**Fallback.** If the pod can't reach the KDC (DNS, or port 88 blocked from the pod subnet), it uses a password login, as
the firm's other logins do.

## 4. WSL

The Windows logon's ticket lives in Windows' LSA, and WSL's MIT libraries can't read it. That is my inference, not a
documented statement. So a WSL user gets their own ticket.

**Best guess: the recipe.**

```bash
sudo DEBIAN_FRONTEND=noninteractive apt-get install --yes krb5-user libltdl7   # brings libkrb5-3, libgssapi-krb5-2
sudo tee /etc/krb5.conf >/dev/null <<'EOF'
[libdefaults]
    default_realm = CORP.EXAMPLE.COM
    dns_lookup_kdc = true
    dns_lookup_realm = false
    rdns = false
    renew_lifetime = 7d
[domain_realm]
    .corp.example.com = CORP.EXAMPLE.COM
    corp.example.com = CORP.EXAMPLE.COM
EOF
kinit jdoe@CORP.EXAMPLE.COM     # the realm in capitals; asks for the AD password
klist                           # valid until, renew until
```

- **Renewal:** AD's defaults are a 10-hour ticket, renewable for 7 days
  ([renewal](https://learn.microsoft.com/en-us/previous-versions/windows/it-pro/windows-10/security/threat-protection/security-policy-settings/maximum-lifetime-for-user-ticket-renewal)).
  `kinit -R` renews a live ticket without the password ("an expired ticket cannot be renewed",
  [kinit](https://web.mit.edu/kerberos/krb5-latest/doc/user/user_commands/kinit.html)). The simplest habit
  is `klist -s || kinit` in `~/.bashrc`. `klist -s` "will exit with status 1 if the credentials cache cannot be read or is expired"
  ([klist](https://web.mit.edu/kerberos/krb5-latest/doc/user/user_commands/klist.html)). A user keytab would spare the
  password, but it is a password on disk, so basis doesn't suggest it.
- **Clock skew:** MIT allows 300 seconds
  ([krb5.conf](https://web.mit.edu/kerberos/krb5-latest/doc/admin/conf_files/krb5_conf.html)), and Microsoft says the
  client's clock "must be close" to the KDC's. The error is `Clock skew too great`. Compare `date -u` with Windows. The
  fix I'd try first is `wsl --shutdown` from Windows, which restarts WSL on the host's clock; it isn't from docs.
- **DNS:**
  - `Cannot find KDC for realm "CORP.EXAMPLE.COM"` (verified here) means the SRV lookup failed. List `kdc =
    dc01.corp.example.com` under `[realms]`, or fix WSL's DNS.
  - On Windows 11 22H2 and later, `dnsTunneling` is on by default, and `networkingMode=mirrored` improves "compatibility
    for VPNs" ([WSL networking](https://learn.microsoft.com/en-us/windows/wsl/networking)).
- **Reverse lookup:** MIT's `rdns` defaults to true. A PTR record that names another host makes the client ask for the
  wrong SPN, so the recipe sets `rdns = false`.
- **SPN not found:** `Server not found in Kerberos database` means no SPN for that name. Use the FQDN in `Server=`.
  `kvno MSSQLSvc/sql01.corp.example.com:1433`, or `kvno oracle/<host>`, asks for the service ticket alone, so it tells
  whether the SPN exists before any driver runs.

**Fallback.** The database's password login.

## 5. Recommendation for basis

basis stays out of Kerberos's mechanics. It passes the SQL Server connection string through, takes Oracle's
`externalauth`, and explains failures. It never runs `kinit`, never reads a keytab and never writes `krb5.conf` or
`sqlnet.ora`.

**No `klist -s` check.**

- `klist` isn't in the pod image, and it is in WSL only with `krb5-user`.
- With a client keytab there is no cache until the first login. So `klist -s` exits 1 (verified here) where the login
  would succeed.
- A good cache can still fail on the SPN, the clock or the KDC.

The login check that step 5.6 adds to `basis doctor --env ENV` is the real test.

**Best guess: what 5.6's doctor does.**

- **Which logins are Kerberos.** A SQL Server database is Kerberos when its connection string has `Trusted_Connection`
  set to `yes` or `true`, in any case. An Oracle database is Kerberos when `externalauth` is set.
- **The `ok:` line.** For each Kerberos login it names the identity and the scheme. On SQL Server that is `SELECT
  SUSER_SNAME(), auth_scheme FROM sys.dm_exec_connections WHERE session_id = @@SPID`. On Oracle it is
  `SYS_CONTEXT('USERENV', 'AUTHENTICATION_METHOD')`.
- **NTLM on Windows.** When `auth_scheme` is `NTLM`, the line says that this login will fail in WSL and in pods, which
  have no NTLM, until the DBA registers the SPN.
- **A failure.** It matches MIT's message, which both providers include, and prints one `ERROR` line with its fix:

  | Message contains | Fix in the `ERROR` line |
  |---|---|
  | `No Kerberos credentials available` | run `kinit you@REALM`; in a pod, check `KRB5_CLIENT_KTNAME` names a keytab uid 10001 can read |
  | `Cannot find KDC for realm` | `krb5.conf` names no KDC and DNS has none: set `KRB5_CONFIG`, or add `kdc =` lines |
  | `Cannot contact any KDC` | the KDC is unreachable: port 88 to the domain controllers |
  | `Clock skew too great` | this machine's clock is off by more than 5 minutes |
  | `Server not found in Kerberos database` | no SPN for this server's name: use its FQDN in `Server=`, or ask the DBA for `MSSQLSvc/<fqdn>:<port>` |
  | `Ticket expired` | run `kinit` again, or `kinit -R` |

  The offline doctor, without `--env`, adds nothing for Kerberos.

**Best guess: the managed Dockerfile.**

- It keeps `libkrb5-3` and `libgssapi-krb5-2` always, as today, since ODBC Driver 18 needs them for every login. That is
  all a SQL Server Kerberos pod needs.
- `krb5-user` is never installed by default. A repo that needs `kinit` (Oracle, or a sidecar) lists it in `[tool.basis]
  apt-packages`, which exists today.
- Oracle Instant Client comes only through the opt-in `oracle-client` key in section 2.

**Best guess: settings and docs.**

- `OracleSettings` and `OracleWrite` get `externalauth`.
- Oracle databases with different `mode`s are refused.
- The reference and `.env.example` show the two Kerberos forms.
- The deploy docs carry the pod recipe from section 3. The desktop docs carry the WSL recipe from section 4.

## (work): to run at the firm

A checklist to confirm at the port; nothing above waits on it. Replace `CORP.EXAMPLE.COM`, `jdoe`, the hosts and the
service account with the firm's.

**A developer in WSL**, after section 4's recipe:

```bash
kinit jdoe@CORP.EXAMPLE.COM && klist
kvno MSSQLSvc/SQLHOST.corp.example.com:1433        # repeat for each SQL Server
kvno oracle/ORAHOST.corp.example.com               # repeat for each Oracle database
uv run --no-project --with mssql-python==1.17.0 python -c "import mssql_python as m; c = m.connect('Server=tcp:SQLHOST.corp.example.com,1433;Database=DB;Trusted_Connection=yes;Encrypt=yes'); print(c.cursor().execute('SELECT SUSER_SNAME(), auth_scheme FROM sys.dm_exec_connections WHERE session_id = @@SPID').fetchone())"
```

Add `TrustServerCertificate=yes` only if the server's certificate isn't trusted in WSL. The same Python line on the
Citrix desktop, in PowerShell, shows whether Windows uses `KERBEROS` or `NTLM`.

For Oracle in WSL, install Instant Client as in section 2, put section 2's `sqlnet.ora` in `TNS_ADMIN`, then:

```bash
uv run --no-project --with oracledb==26.1.0 python -c "import oracledb; oracledb.init_oracle_client(); c = oracledb.connect(dsn='ORAHOST.corp.example.com:1521/SERVICE', externalauth=True); print(c.cursor().execute(\"SELECT SYS_CONTEXT('USERENV','AUTHENTICATION_METHOD'), SYS_CONTEXT('USERENV','AUTHENTICATED_IDENTITY') FROM dual\").fetchone())"
```

**One test pod**, in the QA namespace, with any basis repo's image (it has mssql-python and the Kerberos libraries):

```bash
kubectl create secret generic basis-krb5-qa --from-file=client.keytab=svc_basis_qa.keytab
kubectl create configmap basis-krb5-conf --from-file=krb5.conf
# section 3's pod spec, with args replaced by the SQL Server line above, and no kinit
kubectl apply -f krb5-test.yaml && kubectl logs -f krb5-test
```

Then, in the same pod with `KRB5_TRACE=/dev/stderr`, check two things:

- run the login, delete the cache file, and run the login again;
- if an image with Instant Client exists, try the Oracle line, first with `KRB5_CLIENT_KTNAME` alone and then after
  `kinit -k -t`.

**What to send back:**

- **Each database that uses Kerberos:** which ones, by environment (dev, qa, prod) and kind (SQL Server, Oracle). For
  each, its FQDN and port, whether `kvno` found its SPN, and whether a password login also exists for it.
- **The auth scheme:** the `auth_scheme` from the desktop and from WSL for each SQL Server, and Oracle's
  `AUTHENTICATION_METHOD`.
- **Pod reach and keytabs:** whether the pod resolved the KDC by DNS and reached port 88. Whether the client keytab
  alone logged in to SQL Server, and logged in again after the cache was deleted. Whether Oracle needed `kinit`.
- **Who issues and rotates keytabs:** who issues service-account keytabs, and how they are rotated.
- **The ticket policy:** the realm's name, and its lifetime and renewal, from `klist`'s "expires" and "renew until".
- **Windows:** whether Citrix desktops run Credential Guard, but only if Oracle Kerberos is needed on Windows:
  `(Get-CimInstance -Namespace root\Microsoft\Windows\DeviceGuard -ClassName
  Win32_DeviceGuard).SecurityServicesRunning`, where 1 means it runs.
- **The build agent:** whether it can reach `download.oracle.com`.
- **Failures:** the exact `ERROR` text of any failure, to check section 5's table against.
