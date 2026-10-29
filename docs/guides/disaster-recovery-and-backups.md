# Disaster Recovery and Database Backup Guide

This guide establishes the business continuity, backup automation, and disaster recovery procedures for HireEdge, satisfying launch audit recovery requirements with:
- **Recovery Point Objective (RPO):** $\le 24\text{ hours}$ (maximum acceptable data loss window)
- **Recovery Time Objective (RTO):** $\le 4\text{ hours}$ (maximum acceptable downtime to restore operations)

---

## 1. Automated Backup Architecture

HireEdge uses a multi-layered backup strategy:
1. **Logical SQL Dumps:** Portable, gzipped SQL dumps containing complete schema and table data, with automatic foreign-key disabling during restore.
2. **Online Engine Optimization:** In production MySQL/MariaDB environments with `mysqldump` available, `manage.py dbbackup` uses native `--single-transaction --quick` for zero-downtime, non-locking hot backups. If `mysqldump` is unavailable, a pure-Python batched streaming dump runs automatically. In SQLite development/staging, it utilizes the native SQLite Online Backup API.
3. **Integrity Checksums:** Every backup generates a companion `.sha256` checksum file to guarantee that data corruption during storage or transit is immediately caught before any restore attempt.
4. **Encryption (Optional / Recommended for Off-site):** Symmetrically encrypted using OpenSSL AES-256-CBC with PBKDF2 key derivation.
5. **Retention Pruning:** Automated rotation deletes backup sets older than 30 days (configurable via `BACKUP_RETENTION_DAYS`).

---

## 2. Backup Automation Scripts

Two production-ready wrapper scripts are located in `ops/`:
- **Linux / Docker:** [`ops/backup.sh`](file:///D:/Workshop/JobApp-Main/ops/backup.sh)
- **Windows:** [`ops/backup.ps1`](file:///D:/Workshop/JobApp-Main/ops/backup.ps1)

### Environment Configuration

Configure the following variables in your `.env` or system environment:

```bash
# Directory where backups are written (defaults to <project_dir>/backups)
BACKUP_DIR=/var/backups/hireedge

# Number of days to retain backup files before automated pruning (default: 30)
BACKUP_RETENTION_DAYS=30

# Optional: symmetric passphrase for AES-256 encryption.
# If omitted or empty, backups remain unencrypted gzipped SQL.
BACKUP_ENCRYPTION_PASSPHRASE="your-secure-backup-passphrase"
```

### Scheduling Backups

#### Linux Cron (Daily at 02:00 UTC)
```cron
0 2 * * * cd /opt/hireedge && /bin/bash ops/backup.sh >> /var/log/hireedge/backups.log 2>&1
```

#### Windows Task Scheduler
```powershell
schtasks /create /tn "HireEdge-Daily-Backup" /tr "powershell.exe -ExecutionPolicy Bypass -File D:\Workshop\JobApp-Main\ops\backup.ps1" /sc daily /st 02:00
```

---

## 3. Manual Backup Operations

You can trigger a backup at any time using the Django management command:

```bash
# Standard gzipped backup
python manage.py dbbackup

# Custom output destination
python manage.py dbbackup --output /path/to/snapshot_pre_deploy.sql.gz
```

---

## 4. Disaster Recovery & Restoration Runbook

In the event of database failure, catastrophic server loss, or test restoration drills, follow these steps:

### Step 1: Verify Checksum
Never restore an unverified dump. Confirm cryptographic integrity against the `.sha256` file:

```bash
# Linux
sha256sum -c hireedge_backup_20261022_230000.sql.gz.sha256

# Windows PowerShell
$computed = (Get-FileHash hireedge_backup_20261022_230000.sql.gz -Algorithm SHA256).Hash.ToLower()
$expected = (Get-Content hireedge_backup_20261022_230000.sql.gz.sha256).Trim().Split(" ")[0].ToLower()
if ($computed -ne $expected) { throw "Checksum mismatch! Corrupted backup." }
```

### Step 2: Decrypt (If Encrypted)
If the file ends with `.enc`, decrypt it using the passphrase:

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -pass pass:"$BACKUP_ENCRYPTION_PASSPHRASE" \
  -in hireedge_backup_20261022_230000.sql.gz.enc \
  -out hireedge_backup_20261022_230000.sql.gz
```

### Step 3: Execute Restoration
Run `dbrestore` with the `--verify` flag to automatically check row counts post-import:

```bash
python manage.py dbrestore hireedge_backup_20261022_230000.sql.gz --verify
```

*Note: For testing non-interactively or in CI, pass `--no-input`.*

---

## 5. Queue Health & Worker Recovery

HireEdge background workers run on Huey with SQLite or Redis task queues. If workers fail or crash unexpectedly:

1. **Check Queue Lag:**
   ```bash
   python manage.py huey_queue_status
   ```
2. **Purge Stale Tenant Locks:**
   Worker task execution uses tenant locks with TTLs (`redis_tenant_lock`). If a worker dies mid-task, tenant locks automatically expire within 10 minutes.
3. **Restart Workers:**
   ```bash
   # Systemd
   systemctl restart hireedge-huey
   # Docker
   docker-compose restart huey-worker
   ```

---

## 6. Restoration Drill Schedule

To maintain SOC2 and operational readiness:
- **Bi-annual Restore Drill:** Every 6 months, ops team must restore the latest production snapshot into an isolated staging environment and execute `python manage.py test resume_app.test_zero_data_dashboard` and automated smoke tests.
- **RTO Clock Verification:** Record total elapsed time from snapshot download to verification completion; must remain $\le 240\text{ minutes}$.
