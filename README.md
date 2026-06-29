# Money Tracker

Self-hosted personal finance app — a Quicken replacement. Django + PostgreSQL,
daily account sync via SimpleFIN, hierarchical categories, split transactions,
envelope/"bucket" sub-accounts, property/net-worth tracking, reconciliation,
reports, and QIF import/export. Runs on a local Kubernetes cluster; LAN-only,
single shared login.

Design notes: see [`documentation/plan.html`](documentation/plan.html) and
[`documentation/backup-strategy.html`](documentation/backup-strategy.html).

## Features

- **Quicken-style register** — two-pane dashboard: accounts grouped by institution
  on the left, a sortable transaction register on the right with running balance,
  inline add/edit, cleared checkboxes, and a sticky New/Edit/Split/Delete toolbar.
- **Daily sync** via [SimpleFIN Bridge](https://www.simplefin.org/) — read-only,
  no bank passwords stored. New transactions are flagged until reviewed.
- **Reconciliation** — match imported bank transactions to manually-entered
  receipts; clear/lock against the bank balance.
- **Categories** — hierarchical (3 levels), searchable pickers, per-category
  transaction summaries with bulk re-categorize, and a category report
  (income vs spending, date ranges, drill-down).
- **Split transactions** — one transaction across multiple categories
  (e.g. a paycheck split across taxes/withholdings).
- **Buckets** (envelopes) — set money aside within an account (allowances,
  sinking funds). Moving money to/from a bucket posts a Transfer in the parent
  account and reduces its *available* balance; the *online* balance is shown
  separately so money is never double-counted.
- **Property & net worth** — track home/vehicle values with a change history;
  sidebar shows Portfolio (Fidelity + Wealthfront) and Net Worth totals.
- **Import / export** — QIF and Quicken-CSV import (dedup + splits); QIF export
  (per account or all) for Quicken or portable backup.
- **Backups** — tiered `pg_dump` to S3 (daily/monthly/yearly) plus a daily
  open-format QIF archive. See the backup-strategy doc.

## Stack

Python 3.12 · Django 6 · PostgreSQL 17 (CloudNativePG) · uv · gunicorn ·
WhiteNoise · Tom Select · Bootstrap 5 · Kubernetes (SealedSecrets, ingress-nginx,
MetalLB).

## Local development

```bash
uv sync
cp .env.example .env          # fill SECRET_KEY, FERNET_KEY, DATABASE_URL
uv run python manage.py migrate
uv run python manage.py seed_categories
uv run python manage.py createsuperuser
uv run python manage.py runserver
```
Generate a Fernet key (encrypts connection credentials at rest):
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Connecting SimpleFIN

```bash
uv run python manage.py claim_simplefin <SETUP_TOKEN>   # one-time, stores access URL encrypted
uv run python manage.py sync_accounts --since-days 30   # also runs daily via CronJob
```
First sync auto-discovers institutions/accounts; refine `type` /
`is_reconcilable` flags in `/admin/`.

## Import history

```bash
# Quicken Mac CSV register export:
uv run python manage.py import_csv export.csv --account-id <id> [--dry-run]
# QIF (also available in the UI: user menu -> Restore from QIF):
uv run python manage.py import_qif file.qif --institution PSECU [--dry-run]
```
Both are idempotent and dedup on (account, date, amount, payee).

## Export

```bash
uv run python manage.py export_qif --output all.qif   # or UI: Export all (QIF) / per-account Export QIF
```

## Management commands

`sync_accounts` · `claim_simplefin` · `import_csv` · `import_qif` ·
`export_qif` · `seed_categories`

## Deploy (k8s, LAN)

Prereq: create the `money_tracker` database + role on the PG cluster.
```bash
docker buildx build --platform linux/amd64 -t jaysuzi5/money-tracker:latest --push .
# secrets (fill, seal, apply):
kubeseal --format yaml < k8s/secret.example.yaml    > k8s/sealed-secret.yaml
kubeseal --format yaml < k8s/s3-secret.example.yaml > k8s/s3-sealed-secret.yaml
kubectl apply -f k8s/sealed-secret.yaml -f k8s/s3-sealed-secret.yaml
kubectl apply -f k8s/deployment.yaml -f k8s/cronjob-sync.yaml -f k8s/backup.yaml
kubectl rollout status deploy/money-tracker -n money-tracker
```
Served at `http://money-tracker.home.arpa` (ClusterIP + ingress-nginx via MetalLB;
Pi-Hole resolves the host). Migrations run via an initContainer.

## Layout

- `config/` — Django project (settings, urls, middleware, logging).
- `tracker/` — app: models, views, forms, admin; `connectors/` (SimpleFIN),
  `sync.py`, `qif.py` / `qif_export.py`, `crypto.py`, `management/commands/`.
- `k8s/` — manifests (deployment, ingress, cronjobs, sealed-secret templates).
- `documentation/` — design + backup-strategy docs.

## Security

LAN-only, single login. Connection credentials encrypted at rest (Fernet).
All k8s secrets are SealedSecrets. No bank passwords stored — SimpleFIN is
read-only. See `.gitignore`; never commit `.env`, raw exports, or unsealed secrets.
