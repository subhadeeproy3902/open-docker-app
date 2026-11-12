# What Berth still lacks

An honest list of what a user would expect from a desktop Docker app and a database client, and doesn't find in
Berth yet. Checked against Docker Desktop, Dockhand, Portainer and SQL clients (Neon's table editor, TablePlus,
Beekeeper Studio, DBeaver), and against every page, tab and dialog of Berth itself (`tools/ui_audit.py` photographs
all of them).

`[x]` = done in this round · `[ ]` = missing. **P1** = expected by most users, **P2** = expected by many, **P3** = nice.

## Done in this round

- [x] Database grid like a SQL client: row numbers, columns sized to their content, last column fills the width,
      horizontal scrolling, the card is only as tall as its rows
- [x] Clicking a row opens it in a side panel with every value in full (no 4,000-character cut), its type, key,
      NULL; JSON pretty-printed; binary as size + hex preview + **Save to a file**; ↑/↓ moves through rows
- [x] **Editing rows**: change values, set NULL, add a row (defaults explained in words), delete a row —
      PostgreSQL, MySQL/MariaDB; rows are picked out by their primary key; a stale row is reported, not overwritten
- [x] Redis: edit strings, hash fields (incl. rename), list items, set members, sorted-set scores, add stream
      entries, edit RedisJSON documents; new key, expiry, delete key
- [x] MongoDB, OpenSearch/Elasticsearch, Typesense, Meilisearch, Vespa: edit, add and delete documents as JSON
- [x] Empty / delete a table, collection or index (with confirmation)
- [x] Sort by clicking a column header and filter rows with a condition — on the server, over the whole table
- [x] Rows in primary-key order (they no longer jump around after an edit)
- [x] The first table opens by itself; the Database tab no longer repeats the container name
- [x] Dialogs size to their content (Ports, Share files, SQLite, Add address, Share publicly, Pull) — no empty
      space, nothing cut off; Pull suggests popular images instead of an empty box
- [x] Detail pages (container Inspect / Mounts, image, network, Disk usage, Settings) use two columns on large
      windows instead of one narrow strip
- [x] List pages: the table card hugs its rows
- [x] Volume files: an empty folder no longer says "this image has no shell"; unknown owners show their uid
- [x] MySQL: saving a value that is too long for its column is reported (it was silently ignored for values over
      100 KB)

## Databases

- [ ] **P1** Inline editing in the grid (double-click a cell), multi-select rows, delete several at once
- [ ] **P1** Exact row count and "go to page"; choose page size (50 / 200 / 1,000)
- [ ] **P1** SQL editor: syntax highlighting, autocomplete for tables and columns, run only the selection,
      cancel a running query, several query tabs, saved queries
- [ ] **P1** Export a whole table (all pages) to CSV / JSON; import CSV / JSON into a table
- [ ] **P1** Backups: dump and restore a database (pg_dump / mysqldump / mongodump / Redis RDB), on demand and on a
      schedule, to a folder
- [ ] **P2** Schema editing: create table, add / rename / drop column, indexes, foreign keys
- [ ] **P2** Follow a foreign key to the row it points to; show the referenced row in the panel
- [ ] **P2** Show / hide / reorder columns; type shown in the column header
- [ ] **P2** SQLite: editing (today it opens a read-only copy)
- [ ] **P2** Databases outside Docker (connection string: localhost, a cloud database)
- [ ] **P2** Create / delete databases; users, roles and grants
- [ ] **P2** Activity: running queries with "stop", table sizes, slow queries
- [ ] **P3** Undo the last change; a log of the changes made from Berth
- [ ] **P3** ER diagram
- [ ] **P3** Redis: live MONITOR / pub-sub view, memory by key pattern, bulk expiry
- [ ] **P3** MongoDB: aggregation builder, index management
- [ ] **P3** Search engines: edit index settings / mappings, reindex

## Containers

- [ ] **P1** Select several containers and start / stop / restart / remove them at once
- [ ] **P1** Desktop notifications when a container stops unexpectedly, becomes unhealthy or runs out of memory
- [ ] **P1** "Run" dialog with every option: networks, labels, healthcheck, memory / CPU limits, user, GPU,
      capabilities, env file (today: name, ports, env, volumes, command, restart policy)
- [ ] **P2** Activity timeline per container (created, started, died, OOM, health changes)
- [ ] **P2** Health check history, and defining a health check
- [ ] **P2** Exec: several terminal tabs, choose the shell and user
- [ ] **P2** Download / save logs; follow several containers side by side
- [ ] **P2** What changed in the container's filesystem (`docker diff`)
- [ ] **P3** Drag and drop files into the Files tab

## Images

- [ ] **P1** Build an image from a Dockerfile or folder (with live log) — today only past builds are listed
- [ ] **P1** "Update available" for images and the containers that use them; update and recreate in one click
- [ ] **P2** Browse a private registry's repositories and tags; delete remote tags
- [ ] **P2** Layer and file size breakdown (what makes an image big)
- [ ] **P3** Scheduled vulnerability scans and scan history

## Volumes, networks

- [ ] **P1** Volume backups on a schedule, and restore (export / import exist, by hand)
- [ ] **P2** Connect / disconnect containers to a network from the network or the container page
- [ ] **P2** Create networks and volumes with options (subnet, driver, NFS)
- [ ] **P3** A map of which containers share which networks

## Compose and stacks

- [ ] **P1** A Stacks view: every Compose project with up / down / pull / restart, its file and its env file
- [ ] **P1** Check a Compose file before running it (errors, unused or missing variables, port clashes)
- [ ] **P2** Templates: one-click stacks (Postgres + pgAdmin, WordPress, Redis, …)
- [ ] **P2** Deploy a stack from a Git repository and redeploy on changes
- [ ] **P3** Edit `.env` files with validation

## Sharing and Send files

- [ ] **P2** Protect a public link with a password or code
- [ ] **P2** Traffic for a public link or a local address (requests, last visit)
- [ ] **P3** Send files: history of past shares, and notifications when a phone finishes downloading or uploading

## Watching and alerts

- [ ] **P1** Alerts: CPU / memory / disk thresholds and container failures, as desktop notifications
- [ ] **P2** Keep metrics history while Berth is closed (today: only while a page is open, by design)
- [ ] **P2** An activity log of what Berth did (started, recreated, deleted, shared)

## Whole app

- [ ] **P1** Search everything (Ctrl+K): jump to any container, image, volume, table or page
- [ ] **P1** Remote engines over SSH (today: local socket or a TCP address)
- [ ] **P1** Berth tells you when a new version is out
- [ ] **P2** Accessible names on every icon button for screen readers (tooltips are done)
- [ ] **P2** Translations
- [ ] **P2** Podman support
- [ ] **P3** Export / import Berth's settings
- [ ] **P3** Kubernetes: port-forward, Helm releases, events

## Production readiness

- [ ] **P1** The test suites run in CI on every change (only the database & sharing suite does today)
- [ ] **P1** Tested with hundreds of containers and big databases for speed and memory
- [ ] **P2** A user guide (website) with a page per feature
