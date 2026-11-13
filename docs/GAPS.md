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

- [x] **P1** Inline editing in the grid (double-click a cell), multi-select rows (Ctrl/Shift+click), delete or copy several at once
- [ ] **P1** Exact row count and "go to page"; choose page size (50 / 200 / 1,000)
- [ ] **P2** SQL editor: run only the selection, cancel a running query, several query tabs, saved queries.
      (Highlighting and autocomplete: dropped for now — they would need GtkSourceView, an extra library to bundle
      on macOS and Windows.)
- [x] **P1** Export a whole table (all pages) to CSV / JSON; import CSV / JSON into a table (add or replace, all or nothing)
- [x] **P1** Backups: dump and restore a database (pg_dump / mysqldump / mongodump / Redis RDB), on demand and on a
      schedule, to a folder — the Backups page
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

- [x] **P1** Select several containers (or whole stacks) and start / stop / restart / remove them at once
- [x] **P1** Desktop notifications when a container stops unexpectedly, becomes unhealthy or runs out of memory
- [x] **P1** "Run" dialog with every option: networks, labels, healthcheck, memory / CPU limits, user, GPU,
      capabilities, env file, entrypoint, TTY, init, read-only, privileged, ports on this computer only
- [ ] **P2** Activity timeline per container (created, started, died, OOM, health changes)
- [ ] **P2** Health check history, and defining a health check
- [ ] **P2** Exec: several terminal tabs, choose the shell and user
- [ ] **P2** Download / save logs; follow several containers side by side
- [ ] **P2** What changed in the container's filesystem (`docker diff`)
- [ ] **P3** Drag and drop files into the Files tab

## Images

- [x] **P1** Build an image from a Dockerfile or folder (with live log; BuildKit through the CLI, the engine API without it)
- [x] **P1** "Update available" for images and the containers that use them; update and recreate in one click
- [x] **P2** Browse a private registry's repositories and tags; delete remote tags
- [x] **P2** Layer size breakdown (what makes an image big); files per layer still open
- [x] **P2** Safe updates: scan the new version first, keep the current one if it adds critical / high vulnerabilities
- [x] **P3** Scan history (last 10 scans per image, compared in the scan window)
- [x] **P3** Scheduled vulnerability scans (daily / weekly, images in use) and a Security column

## Volumes, networks

- [x] **P1** Volume backups on a schedule, and restore
- [ ] **P2** Connect / disconnect containers to a network from the network or the container page
- [ ] **P2** Create networks and volumes with options (subnet, driver, NFS)
- [x] **P3** A map of which containers share which networks, with a reachability check

## Compose and stacks

- [x] **P1** A Stacks view: every Compose project (also ones taken down) with up / down / pull / restart, check, edit, logs
- [x] **P1** Check a Compose file before running it (errors, unset variables, port clashes, names, build folders, env files)
- [x] **P2** Templates: one-click stacks (13 in berth/templates/) and Dockerize a folder
- [ ] **P2** Deploy a stack from a Git repository and redeploy on changes
- [x] **P3** Edit `.env` files with validation (Stacks → Environment and secrets)

## Sharing and Send files

- [ ] **P2** Protect a public link with a password or code
- [ ] **P2** Traffic for a public link or a local address (requests, last visit)
- [ ] **P3** Send files: history of past shares, and notifications when a phone finishes downloading or uploading

## Watching and alerts

- [x] **P2** Alerts on CPU / memory / disk thresholds (container failures and backup failures notify already)
- [ ] **P2** Keep metrics history while Berth is closed (today: only while a page is open, by design)
- [ ] **P2** An activity log of what Berth did (started, recreated, deleted, shared)

## Whole app

- [x] **P2** Windows: Berth talks to Docker Desktop's named pipe directly (no TCP setting)
- [x] **P1** Search everything (Ctrl+K / ⌘K, and a Search button in the sidebar): containers, stacks, images, volumes,
      networks, databases, pages and tasks
- [x] **P1** Remote engines over SSH (ssh://user@host — the system's OpenSSH and your keys)
- [x] **P1** Berth tells you when a new version is out (once a day, can be turned off)
- [ ] **P2** Accessible names on every icon button for screen readers (tooltips are done)
- [ ] **P2** Translations
- [ ] **P2** Podman support
- [ ] **P3** Export / import Berth's settings
- [ ] **P3** Kubernetes: port-forward, Helm releases, events

## Production readiness

- [x] **P1** The test suites run in CI on every change: integration, app paths, click-everything, UI audit, SSH,
      five Linux distributions through the installer, and a self-test of the macOS and Windows builds
- [ ] **P1** Tested with hundreds of containers and big databases for speed and memory
- [ ] **P2** A user guide (website) with a page per feature
