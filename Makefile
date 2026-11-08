# Berth — install with:  make install            (system, needs root: PREFIX=/usr/local)
#                        make install-user       (just you: ~/.local, no root)
PREFIX  ?= /usr/local
DESTDIR ?=
APPID    = io.github.subhadeeproy3902.Berth
VERSION ?= $(shell sed -n 's/^VERSION = "\(.*\)"/\1/p' berth/util.py)
ICON    ?= $(APPID)
DATADIR  = $(PREFIX)/share/berth
BINDIR   = $(PREFIX)/bin

.PHONY: install install-user uninstall uninstall-user run check dist

install:
	install -d $(DESTDIR)$(DATADIR)/berth $(DESTDIR)$(BINDIR)
	install -m644 berth/*.py berth/style.css $(DESTDIR)$(DATADIR)/berth/
	sed -e 's|@DATADIR@|$(DATADIR)|' -e 's|@ICON@|$(ICON)|' bin/berth.in > $(DESTDIR)$(BINDIR)/berth
	chmod 755 $(DESTDIR)$(BINDIR)/berth
	install -d $(DESTDIR)$(PREFIX)/share/applications
	sed -e 's|@BINDIR@|$(BINDIR)|' -e 's|@ICON@|$(ICON)|' data/$(APPID).desktop.in > $(DESTDIR)$(PREFIX)/share/applications/$(APPID).desktop
	install -Dm644 data/$(APPID).metainfo.xml $(DESTDIR)$(PREFIX)/share/metainfo/$(APPID).metainfo.xml
	install -Dm644 data/icons/$(APPID).svg $(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/$(APPID).svg
	install -Dm644 data/icons/$(APPID)-symbolic.svg $(DESTDIR)$(PREFIX)/share/icons/hicolor/symbolic/apps/$(APPID)-symbolic.svg
	-[ -z "$(DESTDIR)" ] && gtk-update-icon-cache -q -t $(PREFIX)/share/icons/hicolor 2>/dev/null; true
	-[ -z "$(DESTDIR)" ] && update-desktop-database -q $(PREFIX)/share/applications 2>/dev/null; true

install-user:
	$(MAKE) install PREFIX=$(HOME)/.local ICON=$(ICON)

uninstall:
	rm -rf $(DESTDIR)$(DATADIR)
	rm -f $(DESTDIR)$(BINDIR)/berth $(DESTDIR)$(PREFIX)/share/applications/$(APPID).desktop \
	      $(DESTDIR)$(PREFIX)/share/metainfo/$(APPID).metainfo.xml \
	      $(DESTDIR)$(PREFIX)/share/icons/hicolor/scalable/apps/$(APPID).svg \
	      $(DESTDIR)$(PREFIX)/share/icons/hicolor/symbolic/apps/$(APPID)-symbolic.svg

uninstall-user:
	$(MAKE) uninstall PREFIX=$(HOME)/.local

run:
	python3 -m berth

check:
	python3 -m py_compile berth/*.py && echo "syntax OK"

# Release tarball: dist/berth-linux.tar.gz (what scripts/install.sh and the PKGBUILD download)
dist:
	rm -rf dist/berth-$(VERSION) && mkdir -p dist/berth-$(VERSION)
	cp -r berth data bin scripts Makefile LICENSE README.md dist/berth-$(VERSION)/
	find dist/berth-$(VERSION) -name __pycache__ -prune -exec rm -rf {} +
	tar -C dist -czf dist/berth-linux.tar.gz berth-$(VERSION)
	@echo "dist/berth-linux.tar.gz"
